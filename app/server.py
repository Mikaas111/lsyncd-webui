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
import queue
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
import plain
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
# Present only with Encryption set to None; app/bin/ssh then uses plain.py instead of ssh
PLAIN_CONFIG = os.path.join(SSH_DIR, "plain.json")
PLAIN_CLIENT = os.path.join(APP_DIR, "plain.py")
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
    "scheduled": [],
    "excludes": [".Recycle.Bin/"],
    "delay": 5,
    "delete": True,
    "compress": False,
    "whole_file": True,
    "bwlimit": 0,
    "nightly": "03:00",
    "encryption": "ssh",
    "plain_port": 2223,
    "schedule_hours": 6,
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
# rsync writes numbers with thousands separators ("1,000 files..."), depending on its version and
# locale, and newer versions put a B after byte counts ("1,811,614,517B")
SCAN_RE = re.compile(r"^\s*([\d.,]+) files\.\.\.$")
CONSIDER_RE = re.compile(r"^\s*([\d.,]+) files to consider$")
# rsync exits when nothing moves for this long; the folder is then tried again
RSYNC_TIMEOUT = 300
STALL_ATTEMPTS = 3
# Exit codes after which another attempt can help: 10/12 connection or protocol trouble,
# 30/35 timeout, 255 the connection command failed
RETRY_CODES = (10, 12, 30, 35, 255)
RETRY_DELAY = 10
# After its timeout rsync still waits for its connection command (ssh, or plain.py) to end. If that
# hangs too, the task ends the whole transfer itself once nothing has moved for this much longer.
GUARD_GRACE = 120
PROGRESS_RE = re.compile(
    r"^\s*([\d.,]+)B?\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)"
    r"(?:\s+\((?:xfr#\d+,\s*)?(?:ir|to)-chk=(\d+)/(\d+)\))?"
)


def plain_int(text):
    """'1,811,614,517' or '1.811.614.517' -> 1811614517"""
    return int(re.sub(r"\D", "", text) or 0)

# ---------------------------------------------------------------------------
# Log
# ---------------------------------------------------------------------------

LOG = collections.deque(maxlen=1000)
LOG_LOCK = threading.Lock()
_log_seq = 0
# Lines for the container log. A thread of its own writes them, so a slow Docker log never holds
# up the code that reads rsync's output (and with it rsync); when it falls far behind, lines are dropped.
CONSOLE = queue.Queue(maxsize=10000)


def log(source, message):
    global _log_seq
    text = str(message).rstrip()
    if not text:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_LOCK:
        _log_seq += 1
        LOG.append({"seq": _log_seq, "time": stamp, "source": source, "text": text})
    try:
        CONSOLE.put_nowait(f"{stamp} [{source}] {text}")
    except queue.Full:
        pass


def console_writer():
    while True:
        print(CONSOLE.get(), flush=True)


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
    s["plain_port"] = int_in(raw.get("plain_port"), 1, 65535, s["plain_port"], "Port without encryption", errors)
    s["encryption"] = "none" if raw.get("encryption") == "none" else "ssh"
    s["delay"] = int_in(raw.get("delay"), 1, 3600, s["delay"], "Delay", errors)
    s["schedule_hours"] = int_in(raw.get("schedule_hours"), 1, 168, s["schedule_hours"],
                                 "Hours between scheduled syncs", errors)
    s["bwlimit"] = int_in(raw.get("bwlimit"), 0, 10_000_000, 0, "Bandwidth limit", errors)

    target = str(raw.get("target", s["target"])).strip()
    if target.startswith("/") and "\n" not in target and "\r" not in target:
        s["target"] = posixpath.normpath(target)
    else:
        errors.append("The target folder must be an absolute path, such as /backup.")

    s["delete"] = bool(raw.get("delete", True))
    s["compress"] = bool(raw.get("compress", False))
    s["whole_file"] = bool(raw.get("whole_file", True))

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
    scheduled = {clean_folder(item) for item in raw.get("scheduled", []) or []}
    s["scheduled"] = sorted(f for f in scheduled if f in s["folders"] and f not in s["twoway"])
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
    """Folders lsyncd mirrors live."""
    return [f for f in active_folders(s) if f not in s["twoway"] and f not in s["scheduled"]]


def scheduled_folders(s):
    """Folders mirrored every few hours instead of live, such as appdata that changes all the time."""
    return [f for f in active_folders(s) if f in s["scheduled"]]


def folder_mode(s, folder):
    if folder in s["twoway"]:
        return "twoway"
    return "scheduled" if folder in s["scheduled"] else "mirror"


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
    if s["encryption"] == "none" and s["host"]:
        write_json(PLAIN_CONFIG, plain_settings(s))
    else:
        try:
            os.remove(PLAIN_CONFIG)
        except OSError:
            pass


def plain_settings(s):
    return {"host": s["host"], "port": s["plain_port"], "key": KEY_FILE}


def check_plain(s):
    """Before switching encryption off: can we log in on the backup without SSH? Returns an error or ''."""
    try:
        code, out, err = plain.call(s["host"], s["plain_port"], KEY_FILE, "echo connected", timeout=20)
    except plain.PlainError as error:
        return f"Encryption was not turned off. {error}"
    if code != 0 or "connected" not in out:
        return f"Encryption was not turned off. The backup server answered: {(err or out).strip() or code}"
    return ""


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
    cmd = ["rsync", "-a", f"--timeout={RSYNC_TIMEOUT}"]
    if s["whole_file"]:
        cmd.append("--whole-file")  # the backup does not read the old version to work out a difference
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
        # One transfer at a time across all folders: parallel writes make a parity array crawl
        "    maxProcesses = 1,",
        "}",
        "",
    ]
    for index, folder in enumerate(folders):
        extra = [OUT_FORMAT.format(index), f"--timeout={RSYNC_TIMEOUT}"] + (["--whole-file"] if s["whole_file"] else [])
        extra += [f"--bwlimit={s['bwlimit']}"] if s["bwlimit"] else []
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
        # React to changes at once, and look over everything once a day for what the watcher missed
        "-repeat", "watch+86400",
        # One file at a time (the default is 20 or more), so the backup's disks write in one go
        "-maxthreads", "1",
        "-prefer", "newer", "-copyonconflict",
        "-times", "-owner", "-group", "-numericids",
        "-log=false",
        "-sshcmd", SSH_WRAPPER,
        "-servercmd", "unison",
    ]
    if s["whole_file"]:
        cmd += ["-rsync", "false"]
    for pattern in unison_ignores(s["excludes"], folders):
        cmd += ["-ignore", pattern]
    return cmd


class Transfer:
    """Live transfer speed and the last file that reached the other server completely.

    The speed is what this container's ssh processes (or plain.py clients, with
    encryption off) write: to the network, and what they receive from it.
    Sampled every second. A transfer that starts
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
        counters = transport_counters()
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


def transport_counters():
    """Bytes written so far by each running ssh process or plain.py client."""
    out = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            comm = stat[stat.find("(") + 1:stat.rfind(")")]
            if comm.startswith("python"):
                with open(f"/proc/{entry}/cmdline", "rb") as f:
                    if b"plain.py\0client\0" not in f.read():
                        continue
            elif comm != "ssh":
                continue
            started = stat[stat.rfind(")") + 2:].split()[19]
            with open(f"/proc/{entry}/io", encoding="utf-8") as f:
                wchar = next(int(l.split()[1]) for l in f if l.startswith("wchar:"))
        except (OSError, ValueError, IndexError, StopIteration):
            continue
        out[(entry, started)] = wchar
    return out


def rsync_file_line(line, folder_for):
    """Record a file rsync reports as sent completely.

    Returns the item's change type ("<" sent, "." unchanged, "*" deleted, ...)
    if the line was one of these reports, else "".
    """
    match = FILE_RE.match(line)
    if not match:
        return ""
    index, kind, ftype, size, name = match.groups()
    if kind in "<>" and ftype == "f":
        folder = folder_for(index)
        transfer.file_done(f"{folder}/{name}" if folder else name, int(size))
    return kind


def end_group(proc):
    """End a process started in a session of its own, with everything it started."""
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def new_progress():
    return {"active": True, "seen": time.monotonic(), "scanned": 0, "items": 0, "checked": 0,
            "bytes": 0, "sent_percent": 0, "speed": "", "eta": ""}


def sync_progress(p):
    """Progress of the folder rsync is on, from the counts _on_progress keeps."""
    items = p["items"]
    # Items compared moves when little changed; bytes sent moves during a first upload of big files
    compared = p["checked"] / items if items else 0.0
    fraction = min(1.0, max(compared, p["sent_percent"] / 100))
    return {
        "phase": "Comparing and sending" if items else "Scanning",
        "scanned": p["scanned"], "items": items, "checked": min(p["checked"], items) if items else 0,
        "bytes": p["bytes"], "speed": p["speed"], "eta": p["eta"], "percent": int(fraction * 100),
        "quiet": int(time.monotonic() - p["seen"]),
    }


def wait_kind(wchan):
    """'network', 'disk' or '' (running, or nothing to tell), from the kernel function a process sleeps in."""
    if not wchan or wchan == "0" or "nanosleep" in wchan:
        return ""
    if "select" in wchan or "poll" in wchan:
        return "network"  # rsync waits for its connection with select()
    return "disk"


def waiting_on(wchan):
    """What rsync on this server waits for, in words."""
    return {"network": "the backup server", "disk": "the disk or file system"}.get(wait_kind(wchan), "")


# Runs on the backup server during a stall: what its rsync processes wait for and which file they have open
BACKUP_PROBE = """
import json, os
out = []
for pid in os.listdir("/proc"):
    try:
        if not pid.isdigit() or open(f"/proc/{pid}/comm").read().strip() != "rsync":
            continue
        paths = []
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                paths.append(os.readlink(f"/proc/{pid}/fd/{fd}"))
            except OSError:
                pass
        paths = [p for p in paths if p.startswith("/") and not p.startswith(("/dev/", "/proc/"))]
        out.append({"wchan": open(f"/proc/{pid}/wchan").read().strip(), "file": max(paths, key=len) if paths else ""})
    except OSError:
        pass
print(json.dumps(out))
"""


def backup_state(s):
    """What rsync on the backup server is doing, as a sentence ('' if that cannot be told)."""
    try:
        result = ssh("python3 -c " + shlex.quote(BACKUP_PROBE), timeout=20)
    except subprocess.TimeoutExpired:
        return "The backup server does not answer a separate quick check within 20 seconds either: it is overloaded."
    try:
        found = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ""
    return describe_backup(found, s["target"])


def describe_backup(found, target):
    target = target.rstrip("/") + "/"
    kinds = [wait_kind(r["wchan"]) for r in found]
    if "disk" in kinds:
        where = max((r["file"] for r, k in zip(found, kinds) if k == "disk"), key=len)
        where = where[len(target):] if where.startswith(target) else where
        return "On the backup server, rsync waits for its disk" + (f" ({where})" if where else "") + "."
    if found and all(k == "network" for k in kinds):
        return ("On the backup server, rsync waits for data from this server: "
                "the connection between them is stuck, not the disks.")
    if found:
        return "On the backup server, rsync is busy working (CPU)."
    return "No rsync is running on the backup server."


def rsync_activity(pid):
    """Where a running rsync is (the deepest folder or file it has open) and what it waits for."""
    out = {}
    paths = []
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("/") and not target.startswith(("/dev/", "/proc/")):
                paths.append(target)
    except OSError:
        return out
    if paths:
        where = max(paths, key=len)
        out["now_at"] = where[len(DATA_DIR) + 1:] if where.startswith(DATA_DIR + "/") else where
    try:
        with open(f"/proc/{pid}/wchan", encoding="utf-8") as f:
            out["waiting"] = waiting_on(f.read().strip())
    except OSError:
        pass
    return out


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
        self.paused = False

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
        self.paused = False
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGCONT)  # a paused process would not act on SIGTERM
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

    def start(self, s):
        super().start(s)
        if jobs.busy():  # (re)started during a task, such as after saving settings
            self.pause()

    def pause(self):
        """Hold Unison while a task runs, so the backup server does one heavy thing at a time."""
        with self.lock:
            proc = self.proc
            if proc is not None and proc.poll() is None and not self.paused:
                proc.send_signal(signal.SIGSTOP)
                self.paused = True
                log(self.name, "Paused while a task runs; it continues where it was afterwards.")

    def resume(self):
        with self.lock:
            proc = self.proc
            if self.paused and proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGCONT)
                log(self.name, "Continuing.")
            self.paused = False

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

JOB_NAMES = {"full": "Full sync", "scheduled": "Scheduled sync", "compare": "Compare", "count": "Count",
             "speed": "Speed test"}


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.proc = None
        self.stop_requested = False
        self.info = None
        self.progress = {}  # counts of the folder rsync is on, while it runs

    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def snapshot(self):
        with self.lock:
            info = dict(self.info) if self.info else None
        p = self.progress
        if info and info["state"] == "running" and p.get("active"):
            # Computed when asked, so the page always shows rsync's latest counts
            info.update(sync_progress(p))
            proc = self.proc
            if proc is not None and proc.poll() is None:
                info.update(rsync_activity(proc.pid))
            if info["quiet"] >= 60 and info.get("waiting") == "the backup server":
                self._check_backup(p)
            if p.get("backup"):
                info["backup"] = p["backup"]
        return info

    @staticmethod
    def _check_backup(p):
        """Once a minute during a stall, ask the backup server what its rsync waits for."""
        now = time.monotonic()
        if p.get("checking") or now - p.get("checked_backup", 0) < 60:
            return
        p["checking"] = True

        def check():
            try:
                p["backup"] = backup_state(load_settings())
            finally:
                p["checked_backup"] = time.monotonic()
                p["checking"] = False

        threading.Thread(target=check, daemon=True).start()

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
            threading.Thread(target=end_group, args=(proc,), daemon=True).start()

    def _run(self, kind, s, folders):
        log("task", f"{JOB_NAMES[kind]} started" + (f" for: {', '.join(folders)}" if folders else "."))
        unison.pause()
        try:
            if kind in ("full", "scheduled"):
                ok, message = self._full(s, folders, kind)
            elif kind == "compare":
                ok, message = self._compare(s, folders)
            elif kind == "speed":
                ok, message = self._speed(s)
            else:
                ok, message = self._count(s, folders)
        except Exception as err:  # keep the web UI alive whatever happens
            ok, message = False, f"Unexpected error: {err}"
        finally:
            unison.resume()
        state = "stopped" if self.stop_requested else ("done" if ok else "failed")
        if self.stop_requested:
            message = "Stopped."
        self.set(state=state, finished=time.time(), message=message, phase="")
        log("task", f"{JOB_NAMES[kind]}: {message}")

    def _run_rsync(self, cmd, on_line, guard=False):
        """Run rsync and feed its output lines to on_line. With guard, a transfer that stops
        moving is ended (and reported as rsync's timeout code, 30), however it is stuck."""
        try:
            # A session of its own, so ending it also ends its connection command (ssh or plain.py)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except OSError as err:
            log("rsync", f"Could not start rsync: {err}")
            return -1
        self.proc = proc
        errors = threading.Thread(target=self._drain_stderr, args=(proc,), daemon=True)
        errors.start()
        last = [time.monotonic()]
        ended = []
        if guard:
            threading.Thread(target=self._guard, args=(proc, last, ended), daemon=True).start()
        buffer = b""
        while True:
            chunk = proc.stdout.read1(65536)
            if not chunk:
                break
            last[0] = time.monotonic()
            buffer += chunk
            *lines, buffer = re.split(rb"[\r\n]", buffer)
            for raw in lines:
                if raw.strip():
                    on_line(raw.decode("utf-8", "replace"))
        if buffer.strip():
            on_line(buffer.decode("utf-8", "replace"))
        proc.wait()
        errors.join(5)
        for stream in (proc.stdout, proc.stderr):
            stream.close()
        self.proc = None
        return 30 if ended else proc.returncode

    @staticmethod
    def _guard(proc, last, ended):
        limit = RSYNC_TIMEOUT + GUARD_GRACE
        while proc.poll() is None:
            time.sleep(1)
            if time.monotonic() - last[0] > limit:
                ended.append(True)
                log("rsync", f"Nothing moved for {limit // 60} minutes; ending this transfer so it can start again.")
                end_group(proc)
                return

    @staticmethod
    def _drain_stderr(proc):
        for raw in proc.stderr:
            text = raw.decode("utf-8", "replace").rstrip()
            log("rsync", explain_ssh_error(text) or text)

    def _on_progress(self, line):
        """Follow rsync through a folder: scanning the main server, then comparing and sending.

        Only counts here (rsync can report a million lines); snapshot() turns them into progress.
        """
        p = self.progress
        p["seen"] = time.monotonic()
        kind = rsync_file_line(line, lambda _: self.info and self.info["folder"])
        if kind:
            if kind != "*":  # deletions are not in the list of items to check
                p["checked"] += 1
            return
        match = SCAN_RE.match(line)
        if match:
            p["scanned"] = plain_int(match.group(1))
            return
        match = CONSIDER_RE.match(line)
        if match:
            p["items"] = plain_int(match.group(1))
            return
        if line.strip() == "building file list ...":
            return
        match = PROGRESS_RE.match(line)
        if not match:
            log("rsync", line)
            return
        p.update(bytes=plain_int(match.group(1)), sent_percent=int(match.group(2)), speed=match.group(3))
        if match.group(5) is None:  # a line in the middle of a file ends in the time left; one after a file, in the time taken
            p["eta"] = match.group(4)

    @staticmethod
    def _expected_items(folder):
        """Items in a folder at the last count, to say how far the scan is."""
        counted = ((load_state().get("counts") or {}).get("folders") or {}).get(folder) or {}
        main = counted.get("main") or {}
        return main.get("dirs", 0) + main["files"] + 1 if main.get("files") is not None else 0

    def _full(self, s, folders, kind="full"):
        twoway = [f for f in folders if f in s["twoway"]]
        if twoway:
            # Never rsync --delete a two-way folder: it would remove files that are new on the backup.
            # Unison keeps them in sync, and looks over everything once a day itself.
            log("task", f"Two-way folders are left to Unison: {', '.join(twoway)}")
        folders = [f for f in folders if f not in s["twoway"]]
        ensure_remote_dirs(s, folders)
        problems = []
        self.set(total=len(folders))
        for index, folder in enumerate(folders, 1):
            if self.stop_requested:
                break
            self.set(folder=folder, index=index, expected=self._expected_items(folder), percent=0,
                     phase="Scanning", scanned=0, items=0, checked=0, bytes=0, speed="")
            src = source_dir(folder)
            with os.scandir(src) as entries:
                empty = next(entries, None) is None
            if empty and s["delete"]:
                log("task", f"{folder} is empty on the main server. Skipped so the backup copy is not wiped by accident.")
                problems.append(f"{folder} (empty, skipped)")
                continue
            # flist2 reports the scan, name2 every item compared, so the progress bar keeps moving
            cmd = rsync_base(s) + ["--info=progress2,flist2,name2", "--no-inc-recursive", OUT_FORMAT.format("")]
            if s["delete"]:
                cmd.append("--delete")
            cmd += [src + "/", f"{SSH_ALIAS}:{remote_dir(s, folder)}/"]
            for attempt in range(1, STALL_ATTEMPTS + 1):
                self.set(attempt=attempt, attempts=STALL_ATTEMPTS)
                self.progress = new_progress()
                code = self._run_rsync(cmd, self._on_progress, guard=True)
                self.progress = {}
                if code not in RETRY_CODES or self.stop_requested or attempt == STALL_ATTEMPTS:
                    break
                # What was copied stays copied; the next attempt continues with the rest
                log("task", f"{folder}: the transfer stalled or broke off (rsync code {code}). "
                            f"Trying again, attempt {attempt + 1} of {STALL_ATTEMPTS}.")
                time.sleep(RETRY_DELAY)
            self.set(attempt=0)
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
                message += f" {len(twoway)} two-way folder(s) are kept in sync by Unison."
        result = {"time": time.time(), "ok": ok, "message": message}
        if kind == "scheduled":
            update_state(last_scheduled=result)
        else:
            update_state(last_full=result)
            if ok and set(scheduled_folders(s)) <= set(folders):
                update_state(last_scheduled=result)  # this run covered the scheduled folders too
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
        self.set(total=len(folders), items=0, scanned=0, checked=0, bytes=0)
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
        def remote(command, options=()):  # whichever transport is set
            return ["ssh", *options, SSH_ALIAS, command]

        def remote_ssh(command, options=()):
            return ["env", "LSYNCD_WEBUI_TRANSPORT=ssh", "ssh", *options, SSH_ALIAS, command]

        def remote_plain(command, options=()):
            return [sys.executable, PLAIN_CLIENT, "client", plain_test, SSH_ALIAS, command]

        def stop():
            return self.stop_requested

        ciphers = speedtest.local_ciphers()
        steps = len(ciphers) + 3
        encrypted = s["encryption"] != "none"
        result = {"time": time.time(), "busy": transfer.speed() > 1_000_000, "ciphers": [], "encrypted": encrypted}
        self.set(phase="Network without SSH", index=1, total=steps)
        result["network"] = speedtest.network(remote, s["host"], stop=stop)
        if self.stop_requested:
            return False, "Stopped."
        self.set(phase="Without SSH encryption", index=2, percent=int(1 / steps * 100))
        plain_test = os.path.join(CONFIG_DIR, "plain-speedtest.json")
        write_json(plain_test, plain_settings(s))
        try:
            result["plain"] = speedtest.ssh_pipe(remote_plain, "", stop=stop)
        finally:
            os.remove(plain_test)
        for index, cipher in enumerate(ciphers, 3):
            if self.stop_requested:
                return False, "Stopped."
            self.set(phase=f"SSH with {cipher}", index=index, percent=int((index - 1) / steps * 100))
            measured = speedtest.ssh_pipe(remote_ssh, cipher, stop=stop)
            if measured.get("error"):
                measured["error"] = explain_ssh_error(measured["error"]) or measured["error"]
            result["ciphers"].append({"name": cipher, **measured})
        if self.stop_requested:
            return False, "Stopped."
        working = [c for c in result["ciphers"] if c.get("bps")]
        link = result["plain"].get("bps") if not encrypted else max((c["bps"] for c in working), default=None)
        if not link:
            update_state(speedtest=result)
            failed = result["plain"] if not encrypted else (result["ciphers"] or [{}])[0]
            return False, "Could not send data to the backup server: " + (failed.get("error") or "no answer.")
        self.set(phase="Writing a 1 GB test file on the backup server", index=steps,
                 percent=int((steps - 1) / steps * 100))
        result["disk"] = speedtest.remote_disk(remote, s["target"])
        result["cpu"] = {"main": speedtest.cpu_info(), "backup": result["disk"].pop("cpu", None)}
        if working:
            best = max(working, key=lambda c: c["bps"])
            result["cipher"] = best["name"]
            # Worth switching off encryption when that is clearly faster
            result["suggest_plain"] = bool(encrypted and (result["plain"].get("bps") or 0) > best["bps"] * 1.2)
        result["bottleneck"] = speedtest.verdict(
            result["network"].get("bps"), link, result["disk"].get("bps"), encrypted)
        update_state(speedtest=result)
        write_ssh_config(s)  # SSH syncs from now on use the fastest cipher
        self.set(percent=100)
        limit = min(b for b in (link, result["disk"].get("bps")) if b)
        where = {"disk": "writing on the backup server", "encryption": "SSH encryption",
                 "transport": "the CPU passing data through the containers", "network": "the network"}[result["bottleneck"]]
        return True, f"Done. At most {limit / 1e6:.0f} MB/s ({limit * 8 / 1e6:.0f} Mbit/s), limited by {where}."


transfer = Transfer()
lsyncd = Lsyncd()
unison = Unison()
jobs = Jobs()


def scheduled_due(s, state, now):
    """When the scheduled folders are next due: after the interval, or 30 minutes after a failed run."""
    last = state.get("last_scheduled")
    if not last:
        return now
    wait = s["schedule_hours"] * 3600 if last.get("ok") else 1800
    return last["time"] + wait


def scheduler():
    while True:
        time.sleep(20)
        s = load_settings()
        folders = scheduled_folders(s)
        now = time.time()
        if s["host"] and folders and not jobs.busy() and now >= scheduled_due(s, load_state(), now):
            ok, why = jobs.start("scheduled", folders)
            if not ok:
                log("schedule", f"Scheduled sync skipped: {why}")
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
    copied = mirror_folders(new) + scheduled_folders(new)
    run = copied if connection_changed else [f for f in added if f in copied]
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
        "settings": {"host": s["host"], "delay": s["delay"], "nightly": s["nightly"], "delete": s["delete"],
                     "encryption": s["encryption"]},
        "unison": {
            "folders": s["twoway"],
            "running": unison.running(),
            "paused": unison.paused,
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
        "schedule": {
            "folders": s["scheduled"], "hours": s["schedule_hours"], "last": state.get("last_scheduled"),
            "next": scheduled_due(s, state, time.time()) if s["scheduled"] else None,
        },
        "last_compare": compare,
        "counts": state.get("counts"),
        "transfer": transfer.snapshot(),
        "speedtest": state.get("speedtest"),
        "folders": [
            {"path": f, "exists": os.path.isdir(source_dir(f)), "target": remote_dir(s, f),
             "mode": folder_mode(s, f)}
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
            old = load_settings()
            changed = any(old[k] != settings[k] for k in ("encryption", "host", "plain_port"))
            if settings["encryption"] == "none" and settings["host"] and changed:
                error = check_plain(settings)
                if error:
                    self._send(400, {"error": error})
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
        how = "without SSH encryption" if s["encryption"] == "none" else "over SSH"
        return {"ok": True, "message": f"Connected to the backup server {how}.", "output": output}
    return {"ok": False, "message": explain_ssh_error(output) or "Connection failed.", "output": output}


def startup(settings):
    lsyncd.start(settings)
    unison.start(settings)
    # Scheduled folders wait for their schedule, so a restart does not go through appdata again
    if settings["host"] and mirror_folders(settings):
        jobs.start("full", mirror_folders(settings))


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
    threading.Thread(target=console_writer, daemon=True).start()
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
