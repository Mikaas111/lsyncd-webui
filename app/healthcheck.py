#!/usr/bin/env python3
"""Docker health check: the web UI answers (main) or sshd listens (backup)."""

import os
import sys
import urllib.request


def listening(port):
    """Whether something listens on this TCP port. Read from /proc rather than by connecting,
    because every test connection would show up in sshd's log."""
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as f:
                next(f)
                for line in f:
                    fields = line.split()
                    if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:  # 0A: LISTEN
                        return True
        except (OSError, StopIteration, IndexError, ValueError):
            continue
    return False


try:
    if os.environ.get("ROLE", "main") == "backup":
        if not listening(int(os.environ.get("SSH_PORT", "2222"))):
            sys.exit(1)
    else:
        port = os.environ.get("WEBUI_PORT", "8484")
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5).read()
except Exception:
    sys.exit(1)
