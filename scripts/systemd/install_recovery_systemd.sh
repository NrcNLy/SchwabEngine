#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "==> Installing SOXL Autonomous Recovery Systemd Units from $SCRIPT_DIR..."

# 1. Install units
cp "$SCRIPT_DIR/schwab-notify@.service" /etc/systemd/system/
cp "$SCRIPT_DIR/schwab-recovery-arm.service" /etc/systemd/system/
cp "$SCRIPT_DIR/schwab-recovery-arm.timer" /etc/systemd/system/
cp "$SCRIPT_DIR/schwab-recovery-watchdog.service" /etc/systemd/system/

mkdir -p /etc/systemd/system/schwab-trader.service.d
cp "$SCRIPT_DIR/schwab-trader.service.d/live.conf" /etc/systemd/system/schwab-trader.service.d/

chmod 644 /etc/systemd/system/schwab-notify@.service
chmod 644 /etc/systemd/system/schwab-recovery-arm.service
chmod 644 /etc/systemd/system/schwab-recovery-arm.timer
chmod 644 /etc/systemd/system/schwab-recovery-watchdog.service
chmod 644 /etc/systemd/system/schwab-trader.service.d/live.conf

# 2. Reload daemon
echo "==> Running systemctl daemon-reload..."
systemctl daemon-reload

# 3. Enable and start timer and watchdog
echo "==> Enabling schwab-recovery-arm.timer..."
systemctl enable --now schwab-recovery-arm.timer

echo "==> Enabling and starting schwab-recovery-watchdog.service..."
systemctl enable --now schwab-recovery-watchdog.service

echo "==> Systemd installation COMPLETE."
echo "Active Timers:"
systemctl list-timers schwab* --no-pager
