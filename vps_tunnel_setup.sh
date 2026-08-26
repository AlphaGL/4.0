#!/usr/bin/env bash
#
# Watch2D VPS setup for a server behind a BLOCKED-INBOUND network, using a
# Cloudflare Tunnel (server reaches OUT to Cloudflare; no inbound ports needed).
#
# Run on the server (in the VNC console) AFTER cloning the repo to /opt/watch2d:
#   bash /opt/watch2d/vps_tunnel_setup.sh
#
# It installs Python + gunicorn (app on 127.0.0.1:8000) and cloudflared. Static
# files are served by WhiteNoise, so no nginx is needed.
set -e
APP_DIR=/opt/watch2d

echo ">>> [1/4] Packages..."
export DEBIAN_FRONTEND=noninteractive
apt update
apt install -y python3 python3-venv python3-pip python3-dev build-essential \
    libpq-dev git curl

echo ">>> [2/4] Python venv + dependencies (a few minutes)..."
cd "$APP_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
./venv/bin/pip install gunicorn

echo ">>> [3/4] gunicorn service (127.0.0.1:8000)..."
cat > /etc/systemd/system/watch2d.service <<'UNIT'
[Unit]
Description=Watch2D gunicorn
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/watch2d
ExecStart=/opt/watch2d/venv/bin/gunicorn master.wsgi:application --workers 2 --threads 2 --timeout 120 --bind 127.0.0.1:8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

echo ">>> [4/4] cloudflared (Cloudflare Tunnel client)..."
curl -L -o /usr/local/bin/cloudflared \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x /usr/local/bin/cloudflared
cloudflared --version || true

# handy future-updates script
cat > "$APP_DIR/deploy.sh" <<'DEP'
#!/usr/bin/env bash
set -e
cd /opt/watch2d
git pull
./venv/bin/pip install -r requirements.txt
systemctl restart watch2d
echo "Redeployed."
DEP
chmod +x "$APP_DIR/deploy.sh"

echo ""
echo "==================================================================="
echo " Base install done. Next steps:"
echo ""
echo " 1) Create the env file:   nano /opt/watch2d/.env"
echo "      (paste your env vars; DEBUG=False; DATABASE_URL = web DB)"
echo ""
echo " 2) Start the app:"
echo "      chown -R www-data:www-data /opt/watch2d"
echo "      systemctl daemon-reload && systemctl enable --now watch2d"
echo "      curl -sI http://127.0.0.1:8000/ | head -1   # expect 200/301/302"
echo ""
echo " 3) Connect the Cloudflare Tunnel (I'll give you the token command)."
echo "==================================================================="
