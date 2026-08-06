#!/bin/bash
# Generate droplet user_data for a Flotilla SERVER host (v2: runs games on-box).
# Secrets injected at generation time from env — NEVER commit the generated file.
#   READ_ACCESS_KEY / READ_SECRET_KEY : read-only bucket key (app + library pulls)
#   INFERENCE_KEY                     : serverless-inference key for LLM admirals
#   CADDY_HASH                        : bcrypt hash for basic auth
#   DOMAIN, BUCKET, S3_ENDPOINT       : site + storage config
#   EXTRA_SSH_KEYS                    : newline-separated pubkeys for root (optional)
set -euo pipefail
: "${READ_ACCESS_KEY:?}" "${READ_SECRET_KEY:?}" "${INFERENCE_KEY:?}" "${CADDY_HASH:?}"
: "${DOMAIN:?}" "${BUCKET:?}" "${S3_ENDPOINT:?}"
cat <<UD
#!/bin/bash
set -e
exec > /var/log/flotilla-setup.log 2>&1
mkdir -p /root/.ssh
cat >> /root/.ssh/authorized_keys <<'KEYS'
${EXTRA_SSH_KEYS:-}
KEYS
apt-get update
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl rclone python3
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update && apt-get install -y caddy
mkdir -p /opt/flotilla/app /opt/flotilla/library /root/.config/rclone /etc/flotilla
cat > /root/.config/rclone/rclone.conf <<'RC'
[flotilla]
type = s3
provider = DigitalOcean
access_key_id = ${READ_ACCESS_KEY}
secret_access_key = ${READ_SECRET_KEY}
endpoint = ${S3_ENDPOINT}
RC
chmod 600 /root/.config/rclone/rclone.conf
cat > /etc/flotilla/env <<'ENV'
DO_INFERENCE_KEY=${INFERENCE_KEY}
FLOTILLA_LIBRARY=/opt/flotilla/library
FLOTILLA_BIND=127.0.0.1:8080
ENV
chmod 600 /etc/flotilla/env
cat > /usr/local/bin/flotilla-update <<'UPD'
#!/bin/bash
set -e
cur=\$(cat /opt/flotilla/app/VERSION 2>/dev/null || echo none)
new=\$(rclone cat flotilla:${BUCKET}/app/VERSION 2>/dev/null || echo none)
if [ "\$new" != "none" ] && [ "\$new" != "\$cur" ]; then
  rclone copyto flotilla:${BUCKET}/app/flotilla-app.tar.gz /tmp/app.tar.gz
  rm -rf /opt/flotilla/app.new && mkdir -p /opt/flotilla/app.new
  tar xzf /tmp/app.tar.gz -C /opt/flotilla/app.new
  rm -rf /opt/flotilla/app.old && mv /opt/flotilla/app /opt/flotilla/app.old || true
  mv /opt/flotilla/app.new /opt/flotilla/app
  systemctl restart flotilla-server 2>/dev/null || true
  echo "updated to \$new"
fi
UPD
chmod +x /usr/local/bin/flotilla-update
if [ ! -f /opt/flotilla/library/index.json ]; then
  rclone copyto flotilla:${BUCKET}/app/library-seed.tar.gz /tmp/seed.tar.gz && \
    tar xzf /tmp/seed.tar.gz -C /opt/flotilla/library || true
fi
cat > /etc/systemd/system/flotilla-server.service <<'SVC'
[Unit]
Description=Flotilla server
After=network-online.target
[Service]
EnvironmentFile=/etc/flotilla/env
WorkingDirectory=/opt/flotilla/app
ExecStart=/usr/bin/python3 /opt/flotilla/app/server.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
SVC
cat > /etc/systemd/system/flotilla-update.service <<'SVU'
[Unit]
Description=Flotilla app update check
[Service]
Type=oneshot
ExecStart=/usr/local/bin/flotilla-update
SVU
cat > /etc/systemd/system/flotilla-update.timer <<'TMR'
[Unit]
Description=Flotilla update poll
[Timer]
OnCalendar=*:0/2
Persistent=true
[Install]
WantedBy=timers.target
TMR
cat > /etc/caddy/Caddyfile <<'CF'
${DOMAIN} {
    basic_auth {
        ${AUTH_USER:-admin} ${CADDY_HASH}
    }
    reverse_proxy 127.0.0.1:8080
}
CF
systemctl daemon-reload
/usr/local/bin/flotilla-update
systemctl enable --now flotilla-server flotilla-update.timer
systemctl restart caddy
echo "== flotilla server host ready =="
UD
