#!/bin/bash
# ============================================
# SISTRIX LED Ticker — Setup
# ============================================
# Run on the Pi: bash setup.sh

set -e

echo "=================================="
echo "  SISTRIX LED Ticker — Setup"
echo "=================================="

# Install dependencies
echo "[1/3] Installing dependencies..."
sudo apt update -qq
sudo apt install -y python3-pip python3-pillow python3-requests
pip3 install flask --break-system-packages

# Create cache directory
mkdir -p cache

echo ""
echo "[2/3] Creating systemd services..."

# Web panel service
sudo tee /etc/systemd/system/sistrix-web.service > /dev/null << 'EOF'
[Unit]
Description=SISTRIX LED Ticker Web Panel
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=natzir
WorkingDirectory=/home/natzir/sistrix-led
ExecStart=/usr/bin/python3 /home/natzir/sistrix-led/web_panel.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Display service (only enable when you have the LED panel)
sudo tee /etc/systemd/system/sistrix-display.service > /dev/null << 'EOF'
[Unit]
Description=SISTRIX LED Ticker Display
After=network-online.target sistrix-web.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/home/natzir/sistrix-led
ExecStart=/usr/bin/python3 /home/natzir/sistrix-led/display.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

# Enable web panel now
sudo systemctl enable sistrix-web
sudo systemctl start sistrix-web

echo ""
echo "[3/3] Checking..."
sleep 2
if systemctl is-active --quiet sistrix-web; then
    echo "✓ Web panel active at http://raspberrypi.local:5001"
else
    echo "✗ Error starting web panel"
    sudo journalctl -u sistrix-web -n 20
fi

echo ""
echo "=================================="
echo "  Setup complete!"
echo "=================================="
echo ""
echo "  Open in your browser:"
echo "  → http://raspberrypi.local:5001"
echo ""
echo "  1. Configure your SISTRIX API key"
echo "  2. Add domains"
echo "  3. When you have the LED panel, enable the display:"
echo "     sudo systemctl enable sistrix-display"
echo "     sudo systemctl start sistrix-display"
echo ""
