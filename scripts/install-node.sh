#!/usr/bin/env bash
# install-node.sh — bring a riscv64 board into the ruyici WireGuard network and
# start it as a k3s agent.
#
# Idempotent: safe to re-run. Run as root on the board.
#
# Usage:
#   install-node.sh \
#     --name k3-17 \
#     --wg-conf /path/to/k3-17.wg0.conf \
#     --wg-ip 10.100.0.4 \
#     --k3s-server https://10.100.0.3:6443 \
#     --k3s-token-file /etc/rancher/k3s/agent-token \
#     [--iface wg0]
#
set -euo pipefail

IFACE="wg0"
NAME=""
WG_CONF=""
WG_IP=""
K3S_SERVER=""
K3S_TOKEN_FILE=""
UNIT_SRC="/usr/local/share/ruyici/k3s-agent.service"

usage() {
  grep '^#' "$0" | sed 's/^#//' ;
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --wg-conf) WG_CONF="$2"; shift 2 ;;
    --wg-ip) WG_IP="$2"; shift 2 ;;
    --k3s-server) K3S_SERVER="$2"; shift 2 ;;
    --k3s-token-file) K3S_TOKEN_FILE="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --unit-src) UNIT_SRC="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1"; usage ;;
  esac
done

[[ -n "$NAME" && -n "$WG_CONF" && -n "$WG_IP" && -n "$K3S_SERVER" && -n "$K3S_TOKEN_FILE" ]] || usage
[[ -f "$WG_CONF" ]] || { echo "error: wg config not found: $WG_CONF"; exit 1; }
[[ -f "$UNIT_SRC" ]] || { echo "error: unit template not found: $UNIT_SRC"; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: required command missing: $1"; exit 1; }
}
require_cmd wg-quick
require_cmd k3s

# --- 1. WireGuard ------------------------------------------------------------
DEST="/etc/wireguard/${IFACE}.conf"
echo "[1/3] installing WireGuard config"
if [[ -f "$DEST" && "$(realpath -m "$WG_CONF")" == "$(realpath -m "$DEST")" ]]; then
  echo "  source and destination are the same ($DEST); skipping copy"
else
  install -m 600 "$WG_CONF" "$DEST"
fi
systemctl enable --now "wg-quick@${IFACE}"
wg show "${IFACE}" >/dev/null

# --- 2. k3s agent unit -------------------------------------------------------
# Fill the template's placeholders with sed. (Do NOT strip the multi-line
# ExecStart via grep: its continuation lines would be left orphaned and systemd
# would reject the unit with "Missing '='" / bad ExecStart.)
echo "[2/3] installing k3s-agent unit"
{
  sed -e "s|__K3S_SERVER_URL__|${K3S_SERVER}|g" \
      -e "s|__K3S_TOKEN_FILE__|${K3S_TOKEN_FILE}|g" \
      -e "s|__NODE_WG_IP__|${WG_IP}|g" \
      -e "s|__NODE_NAME__|${NAME}|g" \
      "$UNIT_SRC"
} > /etc/systemd/system/k3s-agent.service
systemctl daemon-reload
systemctl enable --now k3s-agent

# --- 3. verify ---------------------------------------------------------------
echo "[3/3] verifying"
echo "WireGuard peers:"
wg show "${IFACE}"

echo
echo "k3s agent status:"
systemctl --no-pager --full status k3s-agent | head -n 12 || true

if command -v kubectl >/dev/null 2>&1; then
  echo "kubectl not present on node (expected for a worker). Check from the k3s server:"
  echo "  kubectl get nodes -l kubernetes.io/arch=riscv64"
fi

echo
echo "node ${NAME} ready (WG ${WG_IP}) — from the control plane run:"
echo "  scripts/wg.py label-node --name ${NAME} --label ruyici.dev/board=<pool>"
