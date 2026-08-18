#!/bin/bash
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.
#
# One-boot installer for the blesentry collector service (P3-1, #32).
# Run via the systemd.run cmdline hook (flash-notes.md pattern).
#
# OPERATIONAL INVARIANTS (maintainer directive, field-learned):
#   - the collector starts on EVERY boot with NO network dependency
#   - it restarts on failure indefinitely
#   - deploys always restart it (scoped sudoers) so the latest
#     deployed code is always the running code
#   - journals persist across reboots (bounded for SD longevity)
# Transient systemd-run units are BANNED for the collector: they die
# on reboot and cost a silent multi-hour collection gap in the field.
set +e
LOG=/boot/firmware/install-service.log
exec >>"$LOG" 2>&1
echo "=== blesentry service install $(date) ==="

cat > /etc/systemd/system/blesentry.service <<'UNIT'
[Unit]
Description=blesentry BLE presence collector
# Needs the Bluetooth stack; deliberately does NOT need the network.
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
User=ryanspeed
Environment=HOME=/home/ryanspeed
WorkingDirectory=/home/ryanspeed/blesentry
ExecStart=/home/ryanspeed/.local/bin/uv run blesentry run \
  --db /home/ryanspeed/blesentry-data/blesentry.db \
  --site-id home --window 10 --pause 5
Restart=on-failure
RestartSec=10
StartLimitIntervalSec=0

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable blesentry.service
echo "service enabled: $(systemctl is-enabled blesentry.service)"

# Deploys must be able to restart the service so the latest code
# is always what runs (scripts/deploy.sh calls this); narrowly
# scoped to exactly these commands.
cat > /etc/sudoers.d/blesentry-service <<'SUDO'
ryanspeed ALL=(root) NOPASSWD: /usr/bin/systemctl restart blesentry.service, /usr/bin/systemctl stop blesentry.service, /usr/bin/systemctl start blesentry.service
SUDO
chmod 440 /etc/sudoers.d/blesentry-service
echo "deploy-restart sudoers installed"

mkdir -p /var/log/journal
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=persistent\nSystemMaxUse=64M\n' \
  > /etc/systemd/journald.conf.d/blesentry.conf
echo "persistent journald configured"

rm -f /boot/firmware/install-service.sh
sed -i 's| systemd.run.*||g' /boot/firmware/cmdline.txt
sync
echo "=== install complete; rebooting into normal operation ==="
exit 0
