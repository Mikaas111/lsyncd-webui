#!/usr/bin/env python3
"""Docker health check: the web UI answers (main) or sshd accepts connections (backup)."""

import os
import socket
import sys
import urllib.request

try:
    if os.environ.get("ROLE", "main") == "backup":
        socket.create_connection(("127.0.0.1", int(os.environ.get("SSH_PORT", "2222"))), timeout=5).close()
    else:
        port = os.environ.get("WEBUI_PORT", "8484")
        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5).read()
except Exception:
    sys.exit(1)
