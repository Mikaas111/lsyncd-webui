FROM alpine:3.22

# One image, two roles: ROLE=main (web UI + lsyncd) or ROLE=backup (SSH receiver)
RUN apk add --no-cache lsyncd rsync openssh-client openssh-keygen openssh-server python3 tini tzdata \
 && sed -i 's/^root:[^:]*:/root:*:/' /etc/shadow

COPY app /app
RUN chmod +x /app/entrypoint.sh /app/bin/ssh

ENV ROLE=main \
    WEBUI_PORT=8484 \
    SSH_PORT=2222 \
    DATA_DIR=/data \
    CONFIG_DIR=/config \
    LANG=C.UTF-8

EXPOSE 8484 2222
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s CMD ["python3", "/app/healthcheck.py"]
ENTRYPOINT ["/sbin/tini", "--", "/app/entrypoint.sh"]
