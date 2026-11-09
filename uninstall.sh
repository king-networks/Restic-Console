#!/usr/bin/env bash
# Remove Restic Console. Backup repositories are never touched.
#   sudo ./uninstall.sh           # keeps /etc/restic-console (config, repository passwords)
#   sudo ./uninstall.sh --purge   # also deletes /etc/restic-console. Save your repository passwords first!
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "run with sudo"; exit 1; }
for t in $(systemctl list-units --all --plain --no-legend 'restic-backup@*.timer' | awk '{print $1}'); do
  systemctl disable --now "$t" || true
done
systemctl disable --now restic-console.service 2>/dev/null || true
rm -rf /etc/systemd/system/restic-backup@*.timer.d
rm -f /etc/systemd/system/restic-console.service /etc/systemd/system/restic-backup@.service \
      /etc/systemd/system/restic-backup@.timer /etc/systemd/system/restic-backup-failed@.service
systemctl daemon-reload
rm -rf /opt/restic-console
rm -f /usr/local/bin/restic-console /usr/share/applications/restic-console.desktop
if [ "${1:-}" = --purge ]; then
  rm -rf /etc/restic-console
  echo "Removed, including /etc/restic-console."
else
  echo "Removed. Config and repository passwords are still in /etc/restic-console."
fi
