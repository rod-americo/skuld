#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COLLECTOR_SRC="$SCRIPT_DIR/skuld_journal_stats_collector.py"

REGISTRY_PATH="${HOME}/.local/share/skuld/services.json"
OUTPUT_PATH="/var/lib/skuld/journal_stats.json"
INSTALL_COLLECTOR="/usr/local/lib/skuld/skuld_journal_stats_collector.py"
SERVICE_FILE="/etc/systemd/system/skuld-journal-stats.service"
TIMER_FILE="/etc/systemd/system/skuld-journal-stats.timer"

usage() {
  cat <<EOF
Usage: $0 [--registry PATH] [--output PATH]

Installs a systemd service+timer that collects restart/execution counters
for managed Skuld services every minute.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --registry)
      REGISTRY_PATH="$2"
      shift 2
      ;;
    --output)
      OUTPUT_PATH="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -f "$COLLECTOR_SRC" ]]; then
  echo "Collector script not found: $COLLECTOR_SRC" >&2
  exit 1
fi

echo "[1/6] Installing collector script to $INSTALL_COLLECTOR"
sudo install -d -m 0755 /usr/local/lib/skuld
sudo install -m 0755 "$COLLECTOR_SRC" "$INSTALL_COLLECTOR"

echo "[2/6] Writing systemd service: $SERVICE_FILE"
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=Collect Skuld restart/execution stats since last boot
After=systemd-journald.service
Wants=systemd-journald.service

[Service]
Type=oneshot
ExecStart=/usr/bin/env python3 $INSTALL_COLLECTOR --registry $REGISTRY_PATH --output $OUTPUT_PATH
EOF

echo "[3/6] Writing systemd timer: $TIMER_FILE"
sudo tee "$TIMER_FILE" >/dev/null <<EOF
[Unit]
Description=Refresh Skuld journal stats every minute

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min
AccuracySec=15s
Persistent=true
Unit=skuld-journal-stats.service

[Install]
WantedBy=timers.target
EOF

echo "[4/6] Reloading systemd"
sudo systemctl daemon-reload

echo "[5/6] Enabling and starting timer"
sudo systemctl enable --now skuld-journal-stats.timer

echo "[6/6] Running first collection now"
sudo systemctl start skuld-journal-stats.service

echo
echo "Installed successfully."
echo "Timer status:"
sudo systemctl status skuld-journal-stats.timer --no-pager
