#!/usr/bin/env python3
"""lsyncd-webui, main-server role.

Keeps lsyncd running for mirrored folders and Unison for two-way folders, runs
full syncs, comparisons and counts on request or on a schedule, and serves a
small web interface.
Everything runs inside the container; the host only provides Docker and the
mounted folders, so the host operating system does not matter.
"""

import base64
import collections
import datetime
import hmac
import json
import os
import posixpath
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import count
import speedtest

def first_dir(env_name, candidates):
    """The environment variable if set, else the first candidate that exists."""
    value = os.environ.get(env_name)
    if value:
        return value
    return next((c for c in candidates if os.path.isdir(c)), candidates[-1])


APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
# With the host's /mnt mounted at /mnt, Unraid's relative share shortcuts
# (/mnt/user/appdata -> ../cache/appdata) resolve just like on the host.
# /data is the older layout that only had /mnt/user.
DATA_DIR = first_dir("DATA_DIR", ["/mnt/user", "/data"])
# Same shares, mounted read-write; only two-way folders are ever written through it
DATA_RW_DIR = first_dir("DATA_RW_DIR", ["/mnt-rw/user", "/data-rw"])
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
PORT = int(os.environ.get("WEBUI_PORT", "8484"))
PASSWORD = os.environ.get("WEBUI_PASSWORD", "")

SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")
SSH_DIR = os.path.join(CONFIG_DIR, "ssh")
KEY_FILE = os.path.join(SSH_DIR, "id_ed25519")
KNOWN_HOSTS = os.path.join(SSH_DIR, "known_hosts")
SSH_CONFIG = os.path.join(SSH_DIR, "config")
LSYNCD_CONF = os.path.join(CONFIG_DIR, "lsyncd.conf.lua")
UNISON_DIR = os.path.join(CONFIG_DIR, "unison")
STATUS_FILE = os.environ.get("LSYNCD_STATUS_FILE", "/tmp/lsyncd.status")
REMOTE_COUNT = os.environ.get("REMOTE_COUNT_SCRIPT", "/app/count.py")
SSH_ALIAS = "backup-target"
SSH_WRAPPER = os.path.join(APP_DIR, "bin", "ssh")

# app/bin/ssh runs the real ssh with "-F $LSYNCD_WEBUI_SSH_CONFIG", so lsyncd and
# rsync use our host, port and key without touching anyone's ~/.ssh.
os.environ["PATH"] = os.path.join(APP_DIR, "bin") + os.pathsep + os.environ.get("PATH", "")
os.environ["LSYNCD_WEBUI_SSH_CONFIG"] = SSH_CONFIG
os.environ["LC_ALL"] = "C.UTF-8"

DEFAULTS = {
    "host": "",
    "port": 2222,
    "user": "root",
    "target": "/backup",
    "folders": [],
    "twoway": [],
    "excludes": [".Recycle.Bin/"],
    "delay": 5,
    "delete": True,
    "compress": False,
    "bwlimit": 0,
    "nightly": "03:00",
}
CONNECTION_KEYS = ("host", "port", "user", "target")

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CIPHER_RE = re.compile(r"^[a-z0-9@.-]{1,64}$")
# rsync prints this line per item, and for a file only once it has been sent completely
# (%b makes rsync log at the end of each transfer). The number after @lsw@ is the lsyncd sync.
OUT_FORMAT = "--out-format=@lsw@{} %i %l %b %n"
FILE_RE = re.compile(r"^@lsw@(\d*) (\S)(\S)\S*\s+(\d+) \d+ (.*)$")
UNISON_BGN_RE = re.compile(r"^\[BGN\] (?:Copying|Updating file) (.+) from (\S+) to (\S+)$")
UNISON_END_RE = re.compile(r"^\[END\] (?:Copying|Updating file) (.+)$")
PROGRESS_RE = re.compile(
    r"^\s*([\d.,]+)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)"
    r"(?:\s+\((?:xfr#\d+,\s*)?(?:ir|to)-chk=(\d+)/(\d+)\))?"
)

# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

LOG = collections.deque(maxlen=1000)
LOG_LOCK = threading.Lock()
_log_seq = 0


def log(source, message):
    global _log_seq
    text = str(message).rstrip()
    if not text:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_LOCK:
        _log_seq += 1
        LOG.append({"seq": _log_seq, "time": stamp, "source": source, "text": text})
    print(f"{stamp} [{source}] {text}", flush=True)


# ---------------------------------------------------------------------------
# Settings and state
# ---------------------------------------------------------------------------

FILE_LOCK = threading.Lock()


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_state():
    with FILE_LOCK:
        return read_json(STATE_FILE, {})


def update_state(**changes):
    with FILE_LOCK:
        state = read_json(STATE_FILE, {})
        state.update(changes)
        write_json(STATE_FILE, state)


def clean_folder(path):
    """Turn user input into a path relative to DATA_DIR that cannot escape it."""
    path = posixpath.normpath("/" + str(path).strip()).lstrip("/")
    if not path or path == "." or "\n" in path or "\r" in path:
        return None
    return path


def int_in(value, low, high, default, label, errors):
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number.")
        return default
    if not low <= number <= high:
        errors.append(f"{label} must be between {low} and {high}.")
        return default
    return number


def validate(raw, check_folders=True):
    s = dict(DEFAULTS)
    errors = []
    if not isinstance(raw, dict):
        return s, ["Invalid settings."]

    host = str(raw.get("host", "")).strip()
    if host and not HOST_RE.match(host):
        errors.append("The backup server address contains invalid characters.")
    else:
        s["host"] = host

    user = str(raw.get("user", s["user"])).strip() or "root"
    if USER_RE.match(user):
        s["user"] = user
    else:
        errors.append("Invalid user name.")

    s["port"] = int_in(raw.get("port"), 1, 65535, s["port"], "SSH port", errors)
    s["delay"] = int_in(raw.get("delay"), 1, 3600, s["delay"], "Delay", errors)
    s["bwlimit"] = int_in(raw.get("bwlimit"), 0, 10_000_000, 0, "Bandwidth limit", errors)

    target = str(raw.get("target", s["target"])).strip()
    if target.startswith("/") and "\n" not in target and "\r" not in target:
        s["target"] = posixpath.normpath(target)
    else:
        errors.append("The target folder must be an absolute path, such as /backup.")

    s["delete"] = bool(raw.get("delete", True))
    s["compress"] = bool(raw.get("compress", False))

    nightly = str(raw.get("nightly", s["nightly"]) or "").strip()
    if nightly and not TIME_RE.match(nightly):
        errors.append("Use a time like 03:00 for the nightly full sync.")
    else:
        s["nightly"] = nightly

    excludes = []
    for item in raw.get("excludes", s["excludes"]) or []:
        item = str(item).strip()
        if item and "\n" not in item and item not in excludes:
            excludes.append(item)
    s["excludes"] = excludes[:500]

    folders = set()
    for item in raw.get("folders", []) or []:
        folder = clean_folder(item)
        if folder is None:
            continue
        if check_folders and not os.path.isdir(source_dir(folder)):
            errors.append(broken_message(folder))
            continue
        folders.add(folder)
    # A folder inside another selected folder is already covered by its parent
    s["folders"] = sorted(f for f in folders if not any(f.startswith(o + "/") for o in folders))

    twoway = {clean_folder(item) for item in raw.get("twoway", []) or []}
    s["twoway"] = sorted(f for f in twoway if f in s["folders"])
    if check_folders:
        for folder in s["twoway"]:
            if not os.path.isdir(rw_dir(folder)) or not os.access(rw_dir(folder), os.W_OK):
                errors.append(
                    f"{folder} is set to two-way, but the container cannot write to it. "
                    "Map the host's /mnt to /mnt-rw (read-write) in the container settings."
                )
    return s, errors


def load_settings():
    settings, _ = validate(read_json(SETTINGS_FILE, {}), check_folders=False)
    return settings


def source_dir(folder):
    return os.path.join(DATA_DIR, folder)


def remote_dir(s, folder):
    return posixpath.join(s["target"], folder)


def rw_dir(folder):
    return os.path.join(DATA_RW_DIR, folder)


def link_target(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def broken_message(folder):
    target = link_target(source_dir(folder))
    if target is None:
        return f"The folder {folder} does not exist."
    return (
        f"{folder} is a shortcut to {target}, which the container cannot reach. "
        "Map the host's /mnt to /mnt (read only) in the container settings."
    )


def active_folders(s):
    return [f for f in s["folders"] if os.path.isdir(source_dir(f))]


def mirror_folders(s):
    return [f for f in active_folders(s) if f not in s["twoway"]]


def twoway_folders(s):
    return [f for f in s["twoway"] if os.path.isdir(rw_dir(f))]


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# SSH
# ---------------------------------------------------------------------------

def ensure_key():
    os.makedirs(SSH_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(KEY_FILE):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "lsyncd-webui", "-f", KEY_FILE],
            check=True,
        )
        log("app", "Created a new SSH key.")


def public_key():
    try:
        with open(KEY_FILE + ".pub", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_ssh_config(s):
    lines = [
        f"Host {SSH_ALIAS}",
        f"    HostName {s['host'] or '127.0.0.1'}",
        f"    Port {s['port']}",
        f"    User {s['user']}",
        f'    IdentityFile "{KEY_FILE}"',
        "    IdentitiesOnly yes",
        f'    UserKnownHostsFile "{KNOWN_HOSTS}"',
        "    StrictHostKeyChecking accept-new",
        "    BatchMode yes",
        "    ConnectTimeout 15",
        "    ServerAliveInterval 30",
        "    ServerAliveCountMax 4",
    ]
    cipher = (load_state().get("speedtest") or {}).get("cipher") or ""
    if CIPHER_RE.match(cipher):
        # Tried first; the default list stays behind it in case the server does not offer it
        lines.append(f"    Ciphers ^{cipher}")
    lines.append("")
    with open(SSH_CONFIG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.chmod(SSH_CONFIG, 0o600)


def ssh(command, timeout=60):
    return subprocess.run(
        ["ssh", SSH_ALIAS, command], capture_output=True, text=True, errors="replace", timeout=timeout
    )


def explain_ssh_error(text):
    checks = [
        ("Permission denied", "The backup server refused the key. Is the public key set in AUTHORIZED_KEY on the backup container?"),
        ("Connection refused", "Nothing is listening on that address and port. Is the backup container running?"),
        ("timed out", "No connection. Is the VPN between the servers up?"),
        ("No route to host", "No route to the backup server. Is the VPN between the servers up?"),
        ("Could not resolve", "The backup server address could not be resolved."),
        ("REMOTE HOST IDENTIFICATION HAS CHANGED", "The backup server has a different host key than before. If that is expected, click 'Forget backup host key'."),
        ("Host key verification failed", "The backup server has a different host key than before. If that is expected, click 'Forget backup host key'."),
    ]
    for needle, message in checks:
        if needle in text:
            return message
    return ""


def ensure_remote_dirs(s, folders):
    if not folders:
        return True
    command = "mkdir -p -- " + " ".join(shlex.quote(remote_dir(s, f)) for f in folders)
    try:
        result = ssh(command, timeout=60)
    except subprocess.TimeoutExpired:
        log("ssh", "The backup server did not respond (timeout).")
        return False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        log("ssh", explain_ssh_error(detail) or detail or f"mkdir failed (code {result.returncode})")
        return False
    return True


def rsync_base(s):
    cmd = ["rsync", "-a"]
    if s["compress"]:
        cmd.append("--compress")
    if s["bwlimit"]:
        cmd.append(f"--bwlimit={s['bwlimit']}")
    for pattern in s["excludes"]:
        cmd += ["--exclude", pattern]
    return cmd


def remote_count(s, folders):
    """Count on the backup server. Returns ({path: counts}, error)."""
    paths = [remote_dir(s, f) for f in folders]
    args = [f"--exclude={shlex.quote(e)}" for e in s["excludes"]] + ["--"] + [shlex.quote(p) for p in paths]
    try:
        result = ssh(f"python3 {shlex.quote(REMOTE_COUNT)} " + " ".join(args), timeout=6 * 3600)
    except subprocess.TimeoutExpired:
        return None, "Counting on the backup server took too long."
    if result.returncode == 0:
        try:
            return json.loads(result.stdout), None
        except ValueError:
            pass
    err = result.stderr.strip()
    if result.returncode in (126, 127) or "No such file" in err or "not found" in err:
        return remote_count_with_find(paths)
    return None, explain_ssh_error(err) or err or f"Counting failed (code {result.returncode})."


def remote_count_with_find(paths):
    """Fallback for a backup target that is not the lsyncd-webui backup container."""
    script = (
        "for p in " + " ".join(shlex.quote(p) for p in paths) + "; do "
        'if [ -d "$p" ]; then printf "%s\\t%s\\t%s\\n" "$p" '
        '"$(find "$p" -mindepth 1 -type d | wc -l)" "$(find "$p" -mindepth 1 ! -type d | wc -l)"; '
        'else printf "%s\\tmissing\\n" "$p"; fi; done'
    )
    try:
        result = ssh(script, timeout=6 * 3600)
    except subprocess.TimeoutExpired:
        return None, "Counting on the backup server took too long."
    if result.returncode != 0:
        return None, result.stderr.strip() or "Counting failed."
    counts = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[1] == "missing":
            counts[parts[0]] = {"missing": True}
        elif len(parts) == 3:
            counts[parts[0]] = {"dirs": int(parts[1]), "files": int(parts[2]), "bytes": None}
    return counts, None


# ---------------------------------------------------------------------------
# lsyncd
# ---------------------------------------------------------------------------

def lua(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def build_lsyncd_conf(s, folders):
    out = [
        "-- Generated by lsyncd-webui. Changes here are overwritten.",
        "settings {",
        f"    statusFile = {lua(STATUS_FILE)},",
        "    statusInterval = 5,",
        "}",
        "",
    ]
    for index, folder in enumerate(folders):
        extra = [OUT_FORMAT.format(index)] + ([f"--bwlimit={s['bwlimit']}"] if s["bwlimit"] else [])
        out += [
            "sync {",
            "    default.rsyncssh,",
            f"    source = {lua(source_dir(folder) + '/')},",
            f"    host = {lua(SSH_ALIAS)},",
            f"    targetdir = {lua(remote_dir(s, folder) + '/')},",
            f"    delay = {s['delay']},",
            f"    delete = {'true' if s['delete'] else 'false'},",
            "    init = false,",
            "    exclude = { " + ", ".join(lua(e) for e in s["excludes"]) + " },",
            # lsyncd runs ssh itself for moves and deletes, by absolute path, so point it at the wrapper
            f"    ssh = {{ binary = {lua(SSH_WRAPPER)} }},",
            "    rsync = {",
            "        archive = true,",
            f"        compress = {'true' if s['compress'] else 'false'},",
        ]
        out.append("        _extra = { " + ", ".join(lua(e) for e in extra) + " },")
        out += ["    },", "}", ""]
    return "\n".join(out)


def lsyncd_status_file():
    """Queue length and watched directory count from lsyncd's status file."""
    try:
        with open(STATUS_FILE, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return None, None
    queue = sum(int(n) for n in re.findall(r"There (?:are|is) (\d+) delays?", text))
    watching = [int(n) for n in re.findall(r"watching (\d+) director", text)]
    return queue, (sum(watching) if watching else None)


def child_count(parent_pid, name):
    if not parent_pid:
        return 0
    total = 0
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except OSError:
            continue
        comm = data[data.find("(") + 1:data.rfind(")")]
        fields = data[data.rfind(")") + 2:].split()
        if comm == name and len(fields) > 1 and fields[1] == str(parent_pid):
            total += 1
    return total


def unison_ignores(excludes, folders):
    """Translate rsync-style excludes into Unison ignore patterns."""
    out = []
    for raw in excludes:
        pattern = raw.strip().rstrip("/")
        if not pattern or pattern.startswith("#"):
            continue
        if "/" in pattern:
            # Anchored to the selected folder; Unison paths start at the root, which holds the folders
            out += [f"Path {folder}/{pattern.lstrip('/')}" for folder in folders]
        else:
            out.append(f"Name {pattern}")
    return out


def build_unison_cmd(s, folders):
    cmd = ["unison", DATA_RW_DIR, f"ssh://{SSH_ALIAS}/{s['target']}"]
    for folder in folders:
        cmd += ["-path", folder]
        if os.path.islink(rw_dir(folder)):
            # Sync what the shortcut points to, not the shortcut itself
            cmd += ["-follow", f"Path {folder}"]
    cmd += [
        "-batch", "-ui", "text",
        "-repeat", "watch",
        "-prefer", "newer", "-copyonconflict",
        "-times", "-owner", "-group", "-numericids",
        "-log=false",
        "-sshcmd", SSH_WRAPPER,
        "-servercmd", "unison",
    ]
    for pattern in unison_ignores(s["excludes"], folders):
        cmd += ["-ignore", pattern]
    return cmd


class Transfer:
    """Live transfer speed and the last file that reached the other server completely.

    The speed is what this container's ssh processes write (to the network, and
    what they receive from it), sampled every second. A transfer that starts
    and ends between two samples is missed, so it is an indication.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = None  # (pid, start time) -> bytes written so far
        self.total = 0
        self.samples = collections.deque(maxlen=4)
        self.last_file = None
        self.files = 0
        self.started = time.time()

    def file_done(self, path, size, direction="to backup"):
        with self.lock:
            self.last_file = {"path": path, "size": size, "time": time.time(), "direction": direction}
            self.files += 1

    def sample(self):
        counters = ssh_counters()
        with self.lock:
            if self.seen is not None:
                self.total += sum(max(0, n - self.seen.get(key, 0)) for key, n in counters.items())
            self.seen = counters
            self.samples.append((time.monotonic(), self.total))

    def speed(self):
        with self.lock:
            if len(self.samples) < 2:
                return 0
            (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0

    def snapshot(self):
        speed = self.speed()
        with self.lock:
            return {"speed": speed, "last_file": self.last_file, "files": self.files, "since": self.started}

    def run(self):
        while True:
            try:
                self.sample()
            except Exception as err:  # never let the sampler die
                log("app", f"Transfer monitor: {err}")
            time.sleep(1)


def ssh_counters():
    """Bytes written so far by each running ssh process."""
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            if stat[stat.find("(") + 1:stat.rfind(")")] != "ssh":
                continue
            started = stat[stat.rfind(")") + 2:].split()[19]
            with open(f"/proc/{entry}/io", encoding="utf-8") as f:
                wchar = next(int(l.split()[1]) for l in f if l.startswith("wchar:"))
        except (OSError, ValueError, IndexError, StopIteration):
            continue
        out[(entry, started)] = wchar
    return out


def rsync_file_line(line, folder_for):
    """Record a file rsync reports as sent completely. True if the line was such a report."""
    match = FILE_RE.match(line)
    if not match:
        return False
    index, kind, ftype, size, name = match.groups()
    if kind in "<>" and ftype == "f":
        folder = folder_for(index)
        transfer.file_done(f"{folder}/{name}" if folder else name, int(size))
    return True


class Daemon:
    """A long-running sync process that is restarted when it exits."""

    name = ""

    def __init__(self):
        self.lock = threading.RLock()
        self.proc = None
        self.wanted = False
        self.started_at = None
        self.last_error = ""
        self.last_activity = None
        self.retry_at = None

    def running(self):
        proc = self.proc
        return proc is not None and proc.poll() is None

    def pid(self):
        proc = self.proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def folders(self, s):
        raise NotImplementedError

    def command(self, s, folders):
        raise NotImplementedError

    def on_line(self, line):
        """Handle a line of output. Return True to keep it out of the log."""
        raise NotImplementedError

    def prepare(self, s, folders):
        pass

    def env(self):
        return None

    def start(self, s):
        with self.lock:
            self._stop()
            folders = self.folders(s)
            if not s["host"] or not folders:
                self.wanted = False
                self.retry_at = None
                return
            self.wanted = True
            self.retry_at = None
            self.prepare(s, folders)
            ensure_remote_dirs(s, folders)
            try:
                self.proc = subprocess.Popen(
                    self.command(s, folders),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    bufsize=1,
                    env=self.env(),
                )
            except OSError as err:
                self.last_error = f"{self.name} could not start: {err}"
                log(self.name, self.last_error)
                self.retry_at = time.time() + 60
                return
            self.started_at = time.time()
            self.last_error = ""
            log(self.name, f"Started for {len(folders)} folder(s).")
            threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if line and not self.on_line(line):
                log(self.name, line)

    def _stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def stop(self):
        with self.lock:
            self.wanted = False
            self._stop()

    def watchdog(self):
        while True:
            time.sleep(5)
            with self.lock:
                if not self.wanted:
                    continue
                proc = self.proc
                if proc is not None and proc.poll() is None:
                    continue
                if proc is not None:
                    self.proc = None
                    self.retry_at = time.time() + 30
                    if not self.last_error:
                        self.last_error = f"{self.name} stopped unexpectedly (code {proc.returncode})."
                    log(self.name, f"Exited with code {proc.returncode}; retrying in 30 seconds.")
                    continue
                if self.retry_at and time.time() >= self.retry_at:
                    self.start(load_settings())


class Lsyncd(Daemon):
    """One-way mirror of the folders that are not two-way."""

    name = "lsyncd"

    def __init__(self):
        super().__init__()
        self.synced = []

    def folders(self, s):
        return mirror_folders(s)

    def prepare(self, s, folders):
        self.synced = list(folders)
        with open(LSYNCD_CONF, "w", encoding="utf-8") as f:
            f.write(build_lsyncd_conf(s, folders))
        try:
            os.remove(STATUS_FILE)
        except OSError:
            pass

    def command(self, s, folders):
        return ["lsyncd", "-nodaemon", "-insist", LSYNCD_CONF]

    def on_line(self, line):
        if rsync_file_line(line, self.folder_for):
            return True
        if re.search(r"\bError\b", line):
            self.last_error = line
        elif "Normal:" in line and "Startup" not in line:
            self.last_activity = time.time()
        return False

    def folder_for(self, index):
        synced = self.synced
        return synced[int(index)] if index.isdigit() and int(index) < len(synced) else ""


class Unison(Daemon):
    """Two-way sync of the folders marked two-way, watching both servers for changes."""

    name = "unison"

    def __init__(self):
        super().__init__()
        self.last_result = ""
        self.pending = {}  # path -> direction, from [BGN] until [END]

    def folders(self, s):
        return twoway_folders(s)

    def prepare(self, s, folders):
        os.makedirs(UNISON_DIR, exist_ok=True)

    def env(self):
        return {**os.environ, "UNISON": UNISON_DIR}

    def command(self, s, folders):
        return build_unison_cmd(s, folders)

    def on_line(self, line):
        begin = UNISON_BGN_RE.match(line)
        if begin:
            if len(self.pending) > 1000:
                self.pending.clear()
            self.pending[begin.group(1)] = "to main" if begin.group(2).startswith("//") else "to backup"
        end = UNISON_END_RE.match(line)
        if end:
            path = end.group(1)
            try:
                size = os.path.getsize(rw_dir(path)) if os.path.isfile(rw_dir(path)) else None
            except OSError:
                size = None
            transfer.file_done(path, size, self.pending.pop(path, "to backup"))
        if line.startswith("Synchronization complete") or line.startswith("Nothing to do"):
            self.last_activity = time.time()
            self.last_result = line
            self.last_error = ""
        elif line.startswith("Fatal error") or line.startswith("Error") or "failed:" in line:
            self.last_error = line
        return False


# ---------------------------------------------------------------------------
# Jobs: full sync, compare, count, speed test
# ---------------------------------------------------------------------------

JOB_NAMES = {"full": "Full sync", "compare": "Compare", "count": "Count", "speed": "Speed test"}


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.proc = None
        self.stop_requested = False
        self.info = None

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self):
        with self.lock:
            return dict(self.info) if self.info else None

    def set(self, **changes):
        with self.lock:
            if self.info is not None:
                self.info.update(changes)

    def start(self, kind, folders=None):
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False, "Another task is already running."
            s = load_settings()
            if not s["host"]:
                return False, "Set the backup server address first."
            wanted = folders if folders is not None else s["folders"]
            todo = [f for f in wanted if f in s["folders"] and os.path.isdir(source_dir(f))]
            if kind == "speed":
                todo = []  # tests the connection, not folders
            elif not todo:
                return False, "Select one or more folders first."
            self.stop_requested = False
            self.info = {
                "kind": kind, "name": JOB_NAMES[kind], "state": "running", "phase": "",
                "started": time.time(), "finished": None, "message": "",
                "folder": "", "index": 0, "total": len(todo),
                "percent": 0, "bytes": 0, "speed": "", "eta": "", "checked": 0, "files": 0,
            }
            self.thread = threading.Thread(target=self._run, args=(kind, s, todo), daemon=True)
            self.thread.start()
            return True, ""

    def stop(self):
        self.stop_requested = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _run(self, kind, s, folders):
        log("task", f"{JOB_NAMES[kind]} started" + (f" for: {', '.join(folders)}" if folders else "."))
        try:
            if kind == "full":
                ok, message = self._full(s, folders)
                if ok and not self.stop_requested:
                    self._count(s, active_folders(s))
            elif kind == "compare":
                ok, message = self._compare(s, folders)
            elif kind == "speed":
                ok, message = self._speed(s)
            else:
                ok, message = self._count(s, folders)
        except Exception as err:  # keep the web UI alive whatever happens
            ok, message = False, f"Unexpected error: {err}"
        state = "stopped" if self.stop_requested else ("done" if ok else "failed")
        if self.stop_requested:
            message = "Stopped."
        self.set(state=state, finished=time.time(), message=message, phase="")
        log("task", f"{JOB_NAMES[kind]}: {message}")

    def _run_rsync(self, cmd, on_line):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as err:
            log("rsync", f"Could not start rsync: {err}")
            return -1
        self.proc = proc
        errors = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        errors.start()
        buffer = b""
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = re.split(rb"[\r\n]", buffer)
            for raw in lines:
                if raw.strip():
                    on_line(raw.decode("utf-8", "replace"))
        if buffer.strip():
            on_line(buffer.decode("utf-8", "replace"))
        proc.wait()
        errors.join(5)
        self.proc = None
        return proc.returncode

    @staticmethod
    def _drain_stderr(proc):
        for raw in proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            log("rsync", explain_ssh_error(text) or text)

    def _on_progress(self, line):
        if rsync_file_line(line, lambda _: self.info and self.info["folder"]):
            return
        match = PROGRESS_RE.match(line)
        if not match:
            log("rsync", line)
            return
        changes = {
            "phase": "Transferring",
            "bytes": int(re.sub(r"\D", "", match.group(1)) or 0),
            "percent": int(match.group(2)),
            "speed": match.group(3),
            "eta": match.group(4),
        }
        if match.group(5):
            left, total = int(match.group(5)), int(match.group(6))
            changes.update(checked=total - left, files=total)
        self.set(**changes)

    def _full(self, s, folders):
        twoway = [f for f in folders if f in s["twoway"]]
        if twoway:
            # Never rsync --delete a two-way folder: it would remove files that are new on the backup.
            # Restarting Unison makes it compare both sides in full instead.
            log("task", f"Two-way folders are reconciled by Unison: {', '.join(twoway)}")
            unison.start(s)
        folders = [f for f in folders if f not in s["twoway"]]
        ensure_remote_dirs(s, folders)
        problems = []
        for index, folder in enumerate(folders, 1):
            if self.stop_requested:
                break
            self.set(folder=folder, index=index, phase="Scanning", percent=0, bytes=0,
                     speed="", eta="", checked=0, files=0)
            src = source_dir(folder)
            with os.scandir(src) as entries:
                empty = next(entries, None) is None
            if empty and s["delete"]:
                log("task", f"{folder} is empty on the main server. Skipped so the backup copy is not wiped by accident.")
                problems.append(f"{folder} (empty, skipped)")
                continue
            cmd = rsync_base(s) + ["--info=progress2", "--no-inc-recursive", OUT_FORMAT.format("")]
            if s["delete"]:
                cmd.append("--delete")
            cmd += [src + "/", f"{SSH_ALIAS}:{remote_dir(s, folder)}/"]
            code = self._run_rsync(cmd, self._on_progress)
            if code in (0, 24):  # 24: some files vanished during the transfer
                self.set(percent=100)
            elif self.stop_requested:
                break
            else:
                problems.append(f"{folder} (rsync code {code})")
        if self.stop_requested:
            ok, message = False, "Stopped."
        elif problems:
            ok, message = False, "Not completed: " + ", ".join(problems)
        else:
            ok, message = True, f"Done: {len(folders)} mirrored folder(s) in sync."
            if twoway:
                message += f" Unison is comparing {len(twoway)} two-way folder(s)."
        update_state(last_full={"time": time.time(), "ok": ok, "message": message})
        return ok, message

    def _compare(self, s, folders):
        lines, total, problems = [], 0, []
        for index, folder in enumerate(folders, 1):
            if self.stop_requested:
                break
            self.set(folder=folder, index=index, phase="Comparing",
                     percent=int((index - 1) / len(folders) * 100))
            cmd = rsync_base(s) + ["--omit-dir-times", "--dry-run", "--itemize-changes"]
            if s["delete"] or folder in s["twoway"]:  # dry run only: lists files that exist only on the backup
                cmd.append("--delete")
            cmd += [source_dir(folder) + "/", f"{SSH_ALIAS}:{remote_dir(s, folder)}/"]
            found = []
            code = self._run_rsync(cmd, found.append)
            if code not in (0, 24):
                if self.stop_requested:
                    break
                problems.append(f"{folder} (rsync code {code})")
                continue
            total += len(found)
            room = max(0, 2000 - len(lines))
            lines += [{"folder": folder, "line": line} for line in found[:room]]
        self.set(percent=100)
        ok = not problems and not self.stop_requested
        update_state(last_compare={
            "time": time.time(), "ok": ok, "differences": total,
            "problems": problems, "folders": folders, "lines": lines,
        })
        if problems:
            return False, "Could not compare: " + ", ".join(problems)
        if total == 0:
            return ok, "Identical: no differences found."
        return ok, f"{total} difference(s) found."

    def _count(self, s, folders):
        main = {}
        for index, folder in enumerate(folders, 1):
            if self.stop_requested:
                return False, "Stopped."
            self.set(phase="Counting on the main server", folder=folder, index=index,
                     percent=int((index - 1) / len(folders) * 50))
            main[folder] = count.count_tree(source_dir(folder), s["excludes"])
        self.set(phase="Counting on the backup server", folder="", percent=50)
        backup, error = remote_count(s, folders)
        backup = backup or {}
        per_folder = {f: {"main": main.get(f), "backup": backup.get(remote_dir(s, f))} for f in folders}
        disk_main = next((c.get("disk") for c in main.values() if c and c.get("disk")), None)
        disk_backup = next((c.get("disk") for c in backup.values() if c and c.get("disk")), None)
        update_state(counts={
            "time": time.time(), "folders": per_folder, "error": error,
            "disk_main": disk_main, "disk_backup": disk_backup,
        })
        self.set(percent=100)
        if error:
            log("task", f"Count: {error}")
            return False, error
        return True, "Counted both servers."

    def _speed(self, s):
        def remote(command, options=()):
            return ["ssh", *options, SSH_ALIAS, command]

        def stop():
            return self.stop_requested

        ciphers = speedtest.local_ciphers()
        steps = len(ciphers) + 2
        result = {"time": time.time(), "busy": transfer.speed() > 1_000_000, "ciphers": []}
        self.set(phase="Network without SSH", index=1, total=steps)
        result["network"] = speedtest.network(remote, s["host"], stop=stop)
        for index, cipher in enumerate(ciphers, 2):
            if self.stop_requested:
                return False, "Stopped."
            self.set(phase=f"SSH with {cipher}", index=index, percent=int((index - 1) / steps * 100))
            measured = speedtest.ssh_pipe(remote, cipher, stop=stop)
            if measured.get("error"):
                measured["error"] = explain_ssh_error(measured["error"]) or measured["error"]
            result["ciphers"].append({"name": cipher, **measured})
        if self.stop_requested:
            return False, "Stopped."
        working = [c for c in result["ciphers"] if c.get("bps")]
        if not working:
            update_state(speedtest=result)
            return False, "SSH to the backup server failed: " + (result["ciphers"][0].get("error") or "no answer.")
        self.set(phase="Writing a 1 GB test file on the backup server", index=steps,
                 percent=int((steps - 1) / steps * 100))
        result["disk"] = speedtest.remote_disk(remote, s["target"])
        result["cpu"] = {"main": speedtest.cpu_info(), "backup": result["disk"].pop("cpu", None)}
        best = max(working, key=lambda c: c["bps"])
        result["cipher"] = best["name"]
        result["bottleneck"] = speedtest.verdict(
            result["network"].get("bps"), best["bps"], result["disk"].get("bps"))
        update_state(speedtest=result)
        write_ssh_config(s)  # syncs from now on use the fastest cipher
        self.set(percent=100)
        limit = min(b for b in (best["bps"], result["disk"].get("bps")) if b)
        where = {"disk": "writing on the backup server", "encryption": "SSH encryption",
                 "network": "the network"}[result["bottleneck"]]
        return True, f"Done. At most {limit / 1e6:.0f} MB/s ({limit * 8 / 1e6:.0f} Mbit/s), limited by {where}."


transfer = Transfer()
lsyncd = Lsyncd()
unison = Unison()
jobs = Jobs()


def scheduler():
    while True:
        time.sleep(20)
        s = load_settings()
        if not s["nightly"]:
            continue
        now = datetime.datetime.now()
        if now.strftime("%H:%M") != s["nightly"]:
            continue
        today = now.strftime("%Y-%m-%d")
        if load_state().get("last_nightly") == today:
            continue
        update_state(last_nightly=today)
        ok, why = jobs.start("full")
        log("schedule", "Nightly full sync started." if ok else f"Nightly full sync skipped: {why}")


def apply_settings(new):
    old = load_settings()
    write_json(SETTINGS_FILE, new)
    write_ssh_config(new)
    lsyncd.start(new)
    unison.start(new)
    message = "Saved."
    connection_changed = any(old[k] != new[k] for k in CONNECTION_KEYS)
    added = [f for f in new["folders"] if f not in old["folders"]]
    # Unison already compares two-way folders in full when it (re)starts
    run = mirror_folders(new) if connection_changed else [f for f in added if f in mirror_folders(new)]
    if new["host"] and run:
        ok, _ = jobs.start("full", run)
        message += " A full sync has started." if ok else " Start a full sync once the current task is done."
    return message


def browse(rel):
    rel = clean_folder(rel) if rel else ""
    base = source_dir(rel) if rel else DATA_DIR
    dirs = []
    try:
        with os.scandir(base) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    is_dir = entry.is_dir()
                    is_link = entry.is_symlink()
                except OSError:
                    continue
                broken = is_link and not os.path.exists(entry.path)
                if not is_dir and not broken:
                    continue
                path = f"{rel}/{entry.name}" if rel else entry.name
                item = {"name": entry.name, "path": path, "children": is_dir and has_subdirs(entry.path)}
                if is_link:
                    item["link"] = link_target(entry.path)
                    item["broken"] = broken
                dirs.append(item)
    except OSError as err:
        return {"path": rel, "dirs": [], "error": err.strerror or str(err)}
    dirs.sort(key=lambda d: d["name"].lower())
    return {"path": rel, "dirs": dirs}


def has_subdirs(path, limit=2000):
    try:
        with os.scandir(path) as entries:
            for seen, entry in enumerate(entries):
                if seen >= limit:
                    return True
                try:
                    if entry.is_dir() and not entry.name.startswith("."):
                        return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def status_payload():
    s = load_settings()
    state = load_state()
    queue, watching = lsyncd_status_file()
    pid = lsyncd.pid()
    compare = state.get("last_compare")
    if compare:
        compare = {k: v for k, v in compare.items() if k != "lines"}
    return {
        "time": time.time(),
        "configured": bool(s["host"] and s["folders"]),
        "settings": {"host": s["host"], "delay": s["delay"], "nightly": s["nightly"], "delete": s["delete"]},
        "unison": {
            "folders": s["twoway"],
            "running": unison.running(),
            "wanted": unison.wanted,
            "since": unison.started_at,
            "last_activity": unison.last_activity,
            "last_result": unison.last_result,
            "last_error": unison.last_error,
        },
        "lsyncd": {
            "running": lsyncd.running(),
            "wanted": lsyncd.wanted,
            "since": lsyncd.started_at,
            "queue": queue,
            "watching": watching,
            "busy": child_count(pid, "rsync") + child_count(pid, "ssh") if pid else 0,
            "last_activity": lsyncd.last_activity,
            "last_error": lsyncd.last_error,
        },
        "inotify_max": read_int("/proc/sys/fs/inotify/max_user_watches"),
        "job": jobs.snapshot(),
        "last_full": state.get("last_full"),
        "last_compare": compare,
        "counts": state.get("counts"),
        "transfer": transfer.snapshot(),
        "speedtest": state.get("speedtest"),
        "folders": [
            {"path": f, "exists": os.path.isdir(source_dir(f)), "target": remote_dir(s, f),
             "mode": "twoway" if f in s["twoway"] else "mirror"}
            for f in s["folders"]
        ],
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "lsyncd-webui"

    def log_message(self, *args):
        pass

    def _send(self, code, body, content_type="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        if not PASSWORD or self.path.startswith("/api/health"):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except ValueError:
                given = ""
            if hmac.compare_digest(given.encode(), PASSWORD.encode()):
                return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="lsyncd-webui"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authorized():
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path in ("/", "/index.html"):
            with open(os.path.join(STATIC_DIR, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif url.path == "/api/health":
            self._send(200, {"ok": True})
        elif url.path == "/api/status":
            self._send(200, status_payload())
        elif url.path == "/api/config":
            self._send(200, {
                "settings": load_settings(), "public_key": public_key(), "data_dir": DATA_DIR,
                "rw_available": os.path.isdir(DATA_RW_DIR) and os.access(DATA_RW_DIR, os.W_OK),
            })
        elif url.path == "/api/browse":
            self._send(200, browse(query.get("path", [""])[0]))
        elif url.path == "/api/compare":
            self._send(200, load_state().get("last_compare") or {})
        elif url.path == "/api/log":
            since = int(query.get("since", ["0"])[0] or 0)
            with LOG_LOCK:
                lines = [entry for entry in LOG if entry["seq"] > since]
            self._send(200, {"lines": lines})
        else:
            self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._authorized():
            return
        # Requiring a JSON body blocks plain cross-site form posts
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            self._send(415, {"error": "Send JSON."})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self._send(413, {"error": "Request too large."})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "Invalid JSON."})
            return
        path = urlparse(self.path).path

        if path == "/api/config":
            settings, errors = validate(body)
            if errors:
                self._send(400, {"error": " ".join(errors)})
                return
            message = apply_settings(settings)
            self._send(200, {"settings": settings, "message": message})
        elif path in ("/api/jobs/full", "/api/jobs/compare", "/api/jobs/count", "/api/jobs/speed"):
            ok, why = jobs.start(path.rsplit("/", 1)[1])
            self._send(200 if ok else 409, {"ok": ok, "error": why})
        elif path == "/api/jobs/stop":
            jobs.stop()
            self._send(200, {"ok": True})
        elif path == "/api/ssh/test":
            self._send(200, test_connection())
        elif path == "/api/ssh/forget-host":
            try:
                os.remove(KNOWN_HOSTS)
            except OSError:
                pass
            log("ssh", "Forgot the backup server host key; it will be learned on the next connection.")
            self._send(200, {"ok": True})
        elif path == "/api/ssh/new-key":
            for path_ in (KEY_FILE, KEY_FILE + ".pub"):
                try:
                    os.remove(path_)
                except OSError:
                    pass
            ensure_key()
            lsyncd.start(load_settings())
            unison.start(load_settings())
            self._send(200, {"ok": True, "public_key": public_key()})
        else:
            self._send(404, {"error": "Not found"})


def test_connection():
    s = load_settings()
    if not s["host"]:
        return {"ok": False, "message": "Set the backup server address first."}
    write_ssh_config(s)
    try:
        result = ssh("echo connected; rsync --version | head -n 1", timeout=30)
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "No answer within 30 seconds. Is the VPN between the servers up?"}
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0 and "connected" in result.stdout:
        return {"ok": True, "message": "Connected to the backup server.", "output": output}
    return {"ok": False, "message": explain_ssh_error(output) or "Connection failed.", "output": output}


def startup(settings):
    lsyncd.start(settings)
    unison.start(settings)
    if settings["host"] and mirror_folders(settings):
        jobs.start("full")


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    ensure_key()
    settings = load_settings()
    if not os.path.exists(SETTINGS_FILE):
        write_json(SETTINGS_FILE, settings)
    write_ssh_config(settings)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    threading.Thread(target=lsyncd.watchdog, daemon=True).start()
    threading.Thread(target=unison.watchdog, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=transfer.run, daemon=True).start()
    threading.Thread(target=startup, args=(settings,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log("app", f"Web interface listening on port {PORT}.")
    try:
        server.serve_forever()
    finally:
        jobs.stop()
        lsyncd.stop()
        unison.stop()


if __name__ == "__main__":
    main()
