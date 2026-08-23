#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root to install dedicated-server directories and units" >&2
  exit 1
fi

service_user=${BRIGADE_SERVICE_USER:-brigade}
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
getent passwd "$service_user" >/dev/null || {
  echo "required service user does not exist: $service_user" >&2
  exit 1
}
install -d -o "$service_user" -g "$service_user" -m 0700 /srv/openbrigade/app
install -d -o root -g "$service_user" -m 0750 /srv/openbrigade/services
for unit in openbrigade-web.service openbrigade-orchestrator.service; do
  install -m 0644 "$repo_root/deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
echo "Units copied as regular files. Configure /opt/openbrigade/.env, then run:"
echo "  systemctl enable --now openbrigade-web openbrigade-orchestrator"
