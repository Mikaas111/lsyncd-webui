FROM alpine:3.22

# One image, two roles: ROLE=main (web UI + lsyncd + Unison) or ROLE=backup (SSH receiver)
RUN apk add --no-cache lsyncd rsync unison openssh-client openssh-keygen openssh-server python3 tini tzdata \
 && sed -i 's/^root:[^:]*:/root:*:/' /etc/shadow \
 && for f in /usr/bin/unison-fsmonitor-*; do \
      [ -e "$f" ] && [ ! -e /usr/bin/unison-fsmonitor ] && ln -s "$f" /usr/bin/unison-fsmonitor; \
    done; \
    command -v unison-fsmonitor

COPY app /app
RUN chmod +x /app/entrypoint.sh /app/bin/ssh

ENV ROLE=main \
    WEBUI_PORT=8484 \
    SSH_PORT=2222 \
    PLAIN_PORT=2223 \
    CONFIG_DIR=/config \
    LANG=C.UTF-8

EXPOSE 8484 2222 2223
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s CMD ["python3", "/app/healthcheck.py"]
ENTRYPOINT ["/sbin/tini", "--", "/app/entrypoint.sh"]
