#!/usr/bin/env python3
"""Transfers without SSH encryption, for servers connected through a VPN.

SSH encrypts everything a second time on top of the VPN, which costs a lot of
CPU on a slow server. With Encryption set to None, the main server's "ssh"
wrapper runs this client instead of ssh. It connects to the backup container's
PLAIN_PORT, proves it holds the main server's SSH key by signing a fresh
challenge with ssh-keygen, and then runs the command there, passing input,
output and the exit code through like ssh does. Only keys in AUTHORIZED_KEY
get in, just like with SSH; the data itself is left to the VPN to encrypt.

The login uses frames of 1 byte type, 4 bytes length, data:
  backup H (challenge), main L (command + signature), backup K (ok) or N (refused).
After that the input goes from main to backup as is, and the command on the
backup reads it straight from the connection, so the slow side does not copy
every byte; the main server half-closes the connection at the end of input.
Output comes back in frames: O (output), R (error output), X (exit code).

  python3 plain.py client CONFIG.json [ssh options] HOST COMMAND...
"""

import json
import os
import pwd
import secrets
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

NAMESPACE = "lsyncd-webui"
CHUNK = 256 * 1024
MAX_FRAME = 1 << 20
# Once a command has ended, its last output and exit code get this long to reach the main server
FLUSH_TIMEOUT = 60
HEADER = struct.Struct(">cI")
# ssh options that take a value, so the parser can find where the command starts
WITH_VALUE = set("BbcDEeFIiJLlmOoPpQRSWw")


class PlainError(Exception):
    pass


def send_frame(sock, kind, data=b""):
    sock.sendall(HEADER.pack(kind, len(data)) + data)


def send_frame_waiting(sock, kind, data, give_up):
    """Send a frame, waiting for room as long as give_up() says to keep trying.

    Does not rely on the socket being blocking: the command on the backup reads straight from
    this connection, and rsync makes its input non-blocking, which changes it for us as well.
    Without this, a main server that reads slowly for a moment looked like a lost connection.
    """
    view = memoryview(HEADER.pack(kind, len(data)) + data)
    while view:
        try:
            view = view[sock.send(view, socket.MSG_DONTWAIT):]
        except BlockingIOError:
            if give_up():
                raise TimeoutError("the main server stopped reading")
            select.select([], [sock], [], 1)


def recv_exact(sock, size):
    buf = bytearray(size)
    view = memoryview(buf)
    got = 0
    while got < size:
        n = sock.recv_into(view[got:], size - got)
        if not n:
            raise EOFError
        got += n
    return view


def recv_frame(sock, limit=MAX_FRAME):
    kind, size = HEADER.unpack(recv_exact(sock, HEADER.size))
    if size > limit:
        raise PlainError("The other side sent a frame that is too large.")
    return kind, bytes(recv_exact(sock, size))


def write_all(fd, data):
    view = memoryview(data)
    while view:
        try:
            view = view[os.write(fd, view):]
        except BlockingIOError:  # rsync hands ssh non-blocking pipes
            select.select([], [fd], [])


def read_some(fd, size):
    """os.read that waits on a non-blocking pipe instead of failing."""
    while True:
        try:
            return os.read(fd, size)
        except BlockingIOError:
            select.select([fd], [], [])


def challenge_message(nonce, command):
    return f"lsyncd-webui plain v1\n{nonce}\n{command}".encode()


def keep_alive(sock):
    """Notice a dead connection within about 2 minutes, also in the middle of a file."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    # Keepalive probes cover an idle connection; the user timeout covers data that is never acknowledged
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4),
                        ("TCP_USER_TIMEOUT", 120_000)):
        if hasattr(socket, name):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)


# ---------------------------------------------------------------------------
# Backup side
# ---------------------------------------------------------------------------

def allowed_signers(keys):
    """Turn authorized_keys lines into an ssh-keygen allowed_signers file."""
    lines = []
    for key in keys:
        parts = key.split()
        for i, part in enumerate(parts[:-1]):
            if part.startswith(("ssh-", "ecdsa-", "sk-")):
                lines.append(f'{NAMESPACE} namespaces="{NAMESPACE}" {part} {parts[i + 1]}')
                break
    return "\n".join(lines) + "\n"


def verify(signers_file, message, signature):
    with tempfile.NamedTemporaryFile("w", suffix=".sig", delete=False) as f:
        f.write(signature)
    try:
        result = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", signers_file, "-I", NAMESPACE, "-n", NAMESPACE, "-s", f.name],
            input=message, capture_output=True, timeout=30,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        os.remove(f.name)


class Server:
    """Listens for the main server and runs its commands, like sshd does for its key."""

    def __init__(self, port, signers_file, log=print):
        self.signers_file = signers_file
        self.log = log
        if socket.has_dualstack_ipv6():
            self.sock = socket.create_server(("", port), family=socket.AF_INET6, dualstack_ipv6=True)
        else:
            self.sock = socket.create_server(("", port))
        self.port = self.sock.getsockname()[1]
        home = pwd.getpwuid(os.getuid()).pw_dir
        self.home = home if os.path.isdir(home) else "/"
        self.env = {
            "HOME": self.home, "USER": pwd.getpwuid(os.getuid()).pw_name, "SHELL": "/bin/sh",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8",
        }

    def serve_forever(self):
        while True:
            conn, addr = self.sock.accept()
            threading.Thread(target=self.handle, args=(conn, addr[0]), daemon=True).start()

    def handle(self, conn, peer):
        with conn:
            peer = peer.removeprefix("::ffff:")
            try:
                conn.settimeout(30)
                nonce = secrets.token_hex(32)
                send_frame(conn, b"H", nonce.encode())
                kind, data = recv_frame(conn)
                login = json.loads(data) if kind == b"L" else {}
                command, signature = str(login.get("command", "")), str(login.get("signature", ""))
                if not command or not verify(self.signers_file, challenge_message(nonce, command), signature):
                    self.log(f"Refused an unencrypted connection from {peer}: its key is not in AUTHORIZED_KEY.")
                    send_frame(conn, b"N", b"The backup server does not accept this key. "
                                           b"Paste the main server's key into AUTHORIZED_KEY of the backup container.")
                    return
                conn.settimeout(None)
                keep_alive(conn)
                send_frame(conn, b"K")
                self.run(conn, command, peer)
            except (OSError, EOFError, ValueError, PlainError):
                pass

    def run(self, conn, command, peer=""):
        # The command reads its input straight from the connection
        proc = subprocess.Popen(["/bin/sh", "-c", command], stdin=conn.fileno(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0, cwd=self.home, env=self.env,
                                start_new_session=True)
        lock = threading.Lock()
        lost = threading.Event()
        ended = []  # when the command ended
        errors = bytearray()  # the end of what it wrote to stderr, for the log

        def give_up():
            # While the command runs, a main server that reads slowly only means waiting, as with
            # ssh. Once the command has ended, its last output gets FLUSH_TIMEOUT to get through.
            return lost.is_set() or (bool(ended) and time.monotonic() - ended[0] > FLUSH_TIMEOUT)

        def stop_command():
            lost.set()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass

        def pump(stream, kind):
            fd = stream.fileno()
            while True:
                data = os.read(fd, CHUNK)
                if not data:
                    break
                if kind == b"R":
                    errors[:] = (bytes(errors) + data)[-2000:]
                if lost.is_set():
                    continue  # keep draining so the command is not stuck on a full pipe
                try:
                    with lock:
                        send_frame_waiting(conn, kind, data, give_up)
                except OSError:  # the connection is gone, or the main server stopped reading after the end
                    stop_command()
            stream.close()

        pumps = [threading.Thread(target=pump, args=(proc.stdout, b"O"), daemon=True),
                 threading.Thread(target=pump, args=(proc.stderr, b"R"), daemon=True)]
        for thread in pumps:
            thread.start()
        code = proc.wait()
        ended.append(time.monotonic())
        for thread in pumps:
            thread.join(FLUSH_TIMEOUT + 5)
        code = code if code >= 0 else 128 - code
        if code != 0:
            last = errors.decode("utf-8", "replace").strip().splitlines()[-1:]
            self.log(f"A command from {peer} ended with code {code}: {command[:70]}"
                     + (f" ({last[0][:200]})" if last else ""))
        if not lost.is_set():
            try:
                with lock:
                    send_frame_waiting(conn, b"X", struct.pack(">i", code), give_up)
            except OSError:
                pass
        try:
            conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def serve(port, signers_file, log=print):
    try:
        server = Server(port, signers_file, log)
    except OSError as err:
        log(f"Could not open port {port} for unencrypted transfers: {err.strerror or err}")
        return
    log(f"Accepting unencrypted transfers on port {server.port} (only use these over a VPN).")
    server.serve_forever()


# ---------------------------------------------------------------------------
# Main side
# ---------------------------------------------------------------------------

def sign(key_file, message):
    try:
        result = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key_file, "-n", NAMESPACE],
                                input=message, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise PlainError(f"Could not sign the login: {err}")
    if result.returncode != 0:
        raise PlainError("Could not sign the login: " + result.stderr.decode("utf-8", "replace").strip())
    return result.stdout.decode()


def connect(host, port, key_file, command, timeout=15):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as err:
        raise PlainError(f"No connection to port {port} on {host} ({err.strerror or err}). "
                         "Is the backup container up to date and running?")
    try:
        kind, nonce = recv_frame(sock, 1024)
        if kind != b"H":
            raise PlainError(f"Port {port} on {host} is not the backup container's port for unencrypted transfers.")
        signature = sign(key_file, challenge_message(nonce.decode(), command))
        send_frame(sock, b"L", json.dumps({"command": command, "signature": signature}).encode())
        kind, data = recv_frame(sock, 4096)
        if kind != b"K":
            raise PlainError(data.decode("utf-8", "replace") or "The backup server refused the connection.")
    except (OSError, EOFError, ValueError) as err:
        sock.close()
        raise PlainError(f"Port {port} on {host} did not answer like a backup container ({err}).")
    except PlainError:
        sock.close()
        raise
    sock.settimeout(None)
    keep_alive(sock)
    return sock


def call(host, port, key_file, command, timeout=30):
    """Run a command without input. Returns (exit code, output, error output)."""
    sock = connect(host, port, key_file, command, timeout=timeout)
    out, err = bytearray(), bytearray()
    with sock:
        sock.settimeout(timeout)
        sock.shutdown(socket.SHUT_WR)  # no input
        try:
            while True:
                kind, data = recv_frame(sock)
                if kind == b"O":
                    out += data
                elif kind == b"R":
                    err += data
                elif kind == b"X":
                    return struct.unpack(">i", data)[0], out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        except (OSError, EOFError) as error:
            raise PlainError(f"The connection to the backup server was lost ({error}).")


def command_from_ssh_args(args):
    """What ssh would run remotely: skip the options and the host, like OpenSSH does."""
    host, i = None, 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            i += 1
            if host is None and i < len(args):
                host, i = args[i], i + 1
            break
        if arg.startswith("-") and len(arg) > 1:
            for j in range(1, len(arg)):
                if arg[j] in WITH_VALUE:
                    if j == len(arg) - 1:
                        i += 1  # the value is the next argument
                    break
            i += 1
            continue
        if host is not None:
            break
        host, i = arg, i + 1
    return " ".join(args[i:])


def client(config_file, args):
    """Stand in for ssh: run the command on the backup, pass through input, output and exit code."""
    command = command_from_ssh_args(args)
    if not command:
        sys.stderr.write("lsyncd-webui: no command given.\n")
        os._exit(255)
    try:
        with open(config_file, encoding="utf-8") as f:
            conf = json.load(f)
        sock = connect(conf["host"], int(conf["port"]), conf["key"], command)
    except (OSError, ValueError, KeyError, PlainError) as err:
        sys.stderr.write(f"lsyncd-webui: {err}\n")
        os._exit(255)

    def forward_input():
        # write() rather than send(): the web UI's transfer monitor counts what this process writes
        out = sock.fileno()
        try:
            while True:
                try:
                    data = read_some(0, CHUNK)
                except OSError:  # no input at all, such as a closed stdin
                    data = b""
                if not data:
                    sock.shutdown(socket.SHUT_WR)  # end of input
                    return
                write_all(out, data)
        except OSError as err:
            # The connection broke. Give the reading side a moment to pick up the backup's exit
            # code and last messages, then end, so rsync notices instead of waiting for ever.
            time.sleep(5)
            sys.stderr.write(f"lsyncd-webui: the connection to the backup server was lost ({err}).\n")
            os._exit(255)

    threading.Thread(target=forward_input, daemon=True).start()
    try:
        while True:
            kind, data = recv_frame(sock)
            if kind == b"O":
                write_all(1, data)
            elif kind == b"R":
                write_all(2, data)
            elif kind == b"X":
                os._exit(struct.unpack(">i", data)[0])
    except BrokenPipeError:
        os._exit(255)
    except (OSError, EOFError, PlainError) as err:
        sys.stderr.write(f"lsyncd-webui: the connection to the backup server was lost ({err}).\n")
        os._exit(255)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "client":
        client(sys.argv[2], sys.argv[3:])
    else:
        sys.exit("usage: plain.py client CONFIG.json [ssh options] HOST COMMAND...")
