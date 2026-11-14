#!/usr/bin/env python3
"""lsyncd-webui, backup-server role.

Runs an SSH server inside the container that only lets in the main server's key.
rsync, mkdir and the counting script then run here, so the backup host itself
needs nothing but Docker. Next to it, PLAIN_PORT accepts the same key for
transfers without SSH encryption (plain.py), for servers connected by a VPN.
"""

import os
import shutil
import signal
import subprocess
import sys
import threading
import time

import plain

CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")
PORT = os.environ.get("SSH_PORT", "2222").strip()
PLAIN_PORT = os.environ.get("PLAIN_PORT", "2223").strip().lower()
HOST_KEY_DIR = os.path.join(CONFIG_DIR, "hostkeys")
HOST_KEY = os.path.join(HOST_KEY_DIR, "ssh_host_ed25519_key")
EXTRA_KEYS = os.path.join(CONFIG_DIR, "authorized_keys")
AUTHORIZED_KEYS = "/etc/ssh/lsyncd_authorized_keys"
ALLOWED_SIGNERS = "/etc/ssh/lsyncd_allowed_signers"
UNISON_DIR = os.path.join(CONFIG_DIR, "unison")
UNISON_HOME = "/root/.unison"
SSHD_CONFIG = "/etc/ssh/sshd_config_lsyncd"


def log(message):
    print(time.strftime("%Y-%m-%d %H:%M:%S") + " [backup] " + message, flush=True)


def allowed_keys():
    keys = [line.strip() for line in os.environ.get("AUTHORIZED_KEY", "").splitlines() if line.strip()]
    try:
        with open(EXTRA_KEYS, encoding="utf-8") as f:
            keys += [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except OSError:
        pass
    return keys


def main():
    if not PORT.isdigit():
        sys.exit("SSH_PORT must be a number.")
    plain_on = PLAIN_PORT not in ("", "0", "off", "no", "false")
    if plain_on and not PLAIN_PORT.isdigit():
        sys.exit("PLAIN_PORT must be a number, or 0 to turn it off.")
    os.makedirs(HOST_KEY_DIR, mode=0o700, exist_ok=True)
    if not os.path.exists(HOST_KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", HOST_KEY], check=True)
        log("Created a new host key.")
    os.chmod(HOST_KEY, 0o600)

    # Unison keeps its record of the last sync in ~/.unison; keep it in /config so it survives updates
    os.makedirs(UNISON_DIR, exist_ok=True)
    if not os.path.islink(UNISON_HOME):
        if os.path.isdir(UNISON_HOME):
            shutil.rmtree(UNISON_HOME)
        os.symlink(UNISON_DIR, UNISON_HOME)

    keys = allowed_keys()
    with open(AUTHORIZED_KEYS, "w", encoding="utf-8") as f:
        f.write("\n".join(keys) + "\n")
    os.chmod(AUTHORIZED_KEYS, 0o600)
    with open(ALLOWED_SIGNERS, "w", encoding="utf-8") as f:
        f.write(plain.allowed_signers(keys))
    os.chmod(ALLOWED_SIGNERS, 0o600)
    if keys:
        log(f"{len(keys)} key(s) allowed. Listening for the main server on port {PORT}.")
    else:
        log("No key set yet. Paste the main server's public key into AUTHORIZED_KEY and restart this container.")

    with open(SSHD_CONFIG, "w", encoding="utf-8") as f:
        f.write(
            f"Port {PORT}\n"
            f"HostKey {HOST_KEY}\n"
            f"AuthorizedKeysFile {AUTHORIZED_KEYS}\n"
            "AllowUsers root\n"
            "PermitRootLogin prohibit-password\n"
            "PubkeyAuthentication yes\n"
            "PasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\n"
            "AllowTcpForwarding no\n"
            "AllowAgentForwarding no\n"
            "X11Forwarding no\n"
            "PermitTunnel no\n"
            "PrintMotd no\n"
            "ClientAliveInterval 30\n"
        )
    sshd = subprocess.Popen(["/usr/sbin/sshd", "-D", "-e", "-f", SSHD_CONFIG])

    def stop(*_):
        sshd.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if plain_on:
        threading.Thread(target=plain.serve, args=(int(PLAIN_PORT), ALLOWED_SIGNERS, log), daemon=True).start()
    sys.exit(sshd.wait())


if __name__ == "__main__":
    main()
