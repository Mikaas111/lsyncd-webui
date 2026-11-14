#!/bin/sh
set -e
case "${ROLE:-main}" in
  backup) exec python3 -u /app/receiver.py ;;
  main) exec python3 -u /app/server.py ;;
  *) echo "ROLE must be 'main' or 'backup' (got '${ROLE}')." >&2; exit 1 ;;
esac
