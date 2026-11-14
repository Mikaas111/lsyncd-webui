#!/usr/bin/env python3
"""Speed test between the main and the backup server.

Measures each link of the chain separately, so you can see which one limits
the sync speed: the network without SSH, SSH with each fast cipher, and
writing to disk on the backup server.

The main server sends this file itself to the backup over SSH and runs it
there with "python3 - sink|disk ...", so the backup container does not need
to be updated first. Only the standard library is used.
"""

import json
import os
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time

CIPHERS = ["aes128-gcm@openssh.com", "chacha20-poly1305@openssh.com"]
CHUNK = bytes(1 << 20)
SOURCE = os.path.abspath(__file__)


def cpu_info():
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    flags = next((l for l in lines if l.startswith(("flags", "Features"))), "")
    model = next((l.split(":", 1)[1].strip() for l in lines if l.startswith(("model name", "Model"))), "")
    return {"model": model, "aes": " aes " in flags.split(":", 1)[-1] + " ", "cores": os.cpu_count()}


# ---------------------------------------------------------------------------
# Runs on the backup server
# ---------------------------------------------------------------------------

def sink(token):
    """Accept one connection that starts with the token, count what it sends, report back."""
    if socket.has_dualstack_ipv6():
        server = socket.create_server(("", 0), family=socket.AF_INET6, dualstack_ipv6=True)
    else:
        server = socket.create_server(("", 0))
    server.settimeout(30)
    print(server.getsockname()[1], flush=True)
    token = token.encode()
    while True:
        conn, _ = server.accept()
        conn.settimeout(30)
        given = b""
        while len(given) < len(token):
            part = conn.recv(len(token) - len(given))
            if not part:
                break
            given += part
        if given != token:  # someone else knocked on the port
            conn.close()
            continue
        total = 0
        while True:
            part = conn.recv(1 << 20)
            if not part:
                break
            total += len(part)
        conn.sendall(str(total).encode())
        conn.close()
        return


def disk(target, size_mb=1024):
    """Write a test file in the target folder, including the time to flush it to disk."""
    out = {"cpu": cpu_info()}
    path = os.path.join(target, ".lsyncd-webui-speedtest")
    try:
        block = os.urandom(4 << 20)  # random, so file system compression cannot cheat
        size = int(size_mb) << 20
        start = time.monotonic()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        written = 0
        try:
            while written < size and time.monotonic() - start < 60:
                written += os.write(fd, block[:size - written])
            os.fsync(fd)
            out["bytes"], out["seconds"] = written, time.monotonic() - start
        finally:
            os.close(fd)
            os.remove(path)
    except OSError as err:
        out["error"] = f"{err.strerror or err} ({target})"
    print(json.dumps(out))


# ---------------------------------------------------------------------------
# Runs on the main server
# ---------------------------------------------------------------------------

def _source():
    with open(SOURCE, encoding="utf-8") as f:
        return f.read()


def _kill_later(proc, seconds):
    timer = threading.Timer(seconds, lambda: proc.poll() is None and proc.kill())
    timer.daemon = True
    timer.start()
    return timer


def _finish(proc, timer):
    """Stop the process if it still runs, close its pipes and return what it wrote to stderr."""
    timer.cancel()
    if proc.poll() is None:
        proc.kill()
    err = proc.stderr.read()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    proc.wait()
    return err if isinstance(err, str) else err.decode("utf-8", "replace")


def _last_line(text):
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def network(remote, host, seconds=8, stop=lambda: False):
    """Plain TCP from here to a temporary listener on the backup: the network without SSH."""
    token = secrets.token_hex(16)
    proc = subprocess.Popen(remote("python3 - sink " + token), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    timer = _kill_later(proc, seconds + 60)
    result = _send_to_sink(proc, host, token, seconds, stop)
    err = _finish(proc, timer)
    if result is None:
        return {"error": _last_line(err) or "The backup server could not start the test."}
    return result


def _send_to_sink(proc, host, token, seconds, stop):
    try:
        proc.stdin.write(_source())
        proc.stdin.close()
        port = proc.stdout.readline().strip()
        if not port.isdigit():
            proc.wait(10)
            return None
        try:
            conn = socket.create_connection((host, int(port)), timeout=10)
        except OSError as err:
            return {"error": f"Could not open a direct connection to port {port} on {host} "
                             f"({err.strerror or err}). The backup container needs host networking for this test."}
        with conn:
            conn.sendall(token.encode())
            start = time.monotonic()
            while time.monotonic() - start < seconds and not stop():
                conn.sendall(CHUNK)
            conn.shutdown(socket.SHUT_WR)
            reply = b""
            while True:
                part = conn.recv(64)
                if not part:
                    break
                reply += part
            elapsed = time.monotonic() - start
        received = int(reply or 0)
        return {"bps": received / elapsed if elapsed > 0 else 0}
    except (OSError, ValueError, subprocess.TimeoutExpired) as err:
        return {"error": f"The test connection failed: {err}"}


def ssh_pipe(remote, cipher, seconds=8, stop=lambda: False):
    """Data through SSH with one cipher (network plus encryption on both servers),
    or through any other remote command runner that ignores the options."""
    proc = subprocess.Popen(remote("cat > /dev/null", ["-c", cipher, "-o", "Compression=no"]),
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
    timer = _kill_later(proc, seconds + 60)
    sent, start = 0, None
    try:
        proc.stdin.write(CHUNK)  # returns once the connection is up and data flows
        start = time.monotonic()
        while time.monotonic() - start < seconds and not stop():
            proc.stdin.write(CHUNK)
            sent += len(CHUNK)
        proc.stdin.close()
        proc.wait(60)
    except (OSError, subprocess.TimeoutExpired):
        pass
    error = _last_line(_finish(proc, timer))
    if proc.returncode != 0 or start is None:
        return {"error": error or f"ssh stopped with code {proc.returncode}."}
    elapsed = time.monotonic() - start
    return {"bps": sent / elapsed if elapsed > 0 else 0}


def remote_disk(remote, target, size_mb=1024):
    try:
        result = subprocess.run(remote(f"python3 - disk {shlex.quote(target)} {int(size_mb)}"), input=_source(),
                                capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {"error": "Writing on the backup server took more than 10 minutes."}
    try:
        out = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"error": _last_line(result.stderr) or f"The disk test failed (code {result.returncode})."}
    if out.get("seconds"):
        out["bps"] = out["bytes"] / out["seconds"]
    return out


def local_ciphers():
    try:
        result = subprocess.run(["ssh", "-Q", "cipher"], capture_output=True, text=True, timeout=10,
                                env={**os.environ, "LSYNCD_WEBUI_TRANSPORT": "ssh"})
    except (OSError, subprocess.TimeoutExpired):
        return list(CIPHERS)
    known = set(result.stdout.split())
    return [c for c in CIPHERS if c in known] or list(CIPHERS)


def verdict(network_bps, link_bps, disk_bps, encrypted=True):
    """Which link limits the sync: 'disk', 'encryption', 'transport' or 'network'.

    link_bps is what the transport in use reached: SSH, or the plain connection.
    """
    link = link_bps or network_bps
    if disk_bps and link and disk_bps < link * 0.9:
        return "disk"
    if network_bps and link_bps and link_bps < network_bps * 0.75:
        return "encryption" if encrypted else "transport"
    if link:
        return "network"
    return None


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "sink":
        sink(sys.argv[2])
    elif len(sys.argv) >= 3 and sys.argv[1] == "disk":
        disk(sys.argv[2], *sys.argv[3:4])
    else:
        sys.exit("usage: speedtest.py sink TOKEN | disk FOLDER [SIZE_MB]")
