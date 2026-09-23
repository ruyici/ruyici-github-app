#!/usr/bin/env bash
# manual-peer-add.sh — add a WireGuard spoke to a LIVE hub without restarting wg0.
#
# This is for hubs that were set up OUTSIDE wg.py (no /etc/ruyici-wg state), so
# `scripts/wg.py peer add` fails with KeyError: 'hub_ip'. It mirrors how existing
# peers were added: `wg set` on the live interface (non-disruptive) + append the
# [Peer] block to the persisted conf so it survives reboot. No peer is touched.
#
# Requires: wg, wg-quick, root on the hub.
#
# Usage:
#   manual-peer-add.sh \
#     --name k3-09 \
#     --ip 10.100.0.2 \
#     --endpoint 47.242.94.15:51820 \
#     [--iface wg0] [--lan 192.168.1.0/24] [--mtu 1400] [--emit /tmp/k3-09.wg0.conf]
#
# --emit writes the SPOKE config (for /etc/wireguard/wg0.conf on the board).
set -euo pipefail

IFACE=wg0
NAME=""
IP=""
ENDPOINT=""        # hub public endpoint, e.g. 47.242.94.15:51820
LAN=""             # optional board LAN CIDR to route through the tunnel
MTU=1400
EMIT=""            # write spoke config here
CONF="/etc/wireguard/wg0.conf"

usage() {
  cat <<'EOF'
Add a WireGuard spoke to a LIVE hub without restarting wg0.

This is for hubs that were set up OUTSIDE wg.py (no /etc/ruyici-wg state).
It mirrors how existing peers were added: `wg set` on the live interface
(non-disruptive) + append the [Peer] block to the persisted conf.

Usage:
  manual-peer-add.sh --name <board> --ip <10.100.0.x> --endpoint <hub-public>:51820 \
      [--iface wg0] [--lan <lan-cidr>] [--mtu 1400] [--emit <path>]

  --emit   write the SPOKE config here (for /etc/wireguard/wg0.conf on the board)
  --lan    optional board LAN CIDR to route through the tunnel

Run as root on the hub. Requires wg.
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --endpoint) ENDPOINT="$2"; shift 2 ;;
    --lan) LAN="$2"; shift 2 ;;
    --mtu) MTU="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    --emit) EMIT="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1"; usage ;;
  esac
done

[[ -n "$NAME" && -n "$IP" && -n "$ENDPOINT" ]] || usage
command -v wg >/dev/null 2>&1 || { echo "error: wg not found"; exit 1; }

if ! ip link show "$IFACE" >/dev/null 2>&1; then
  echo "error: interface $IFACE not found (is the hub up?)"; exit 1
fi

# --- keys / psk -----------------------------------------------------------
PRV=$(wg genkey)
PUB=$(echo "$PRV" | wg pubkey)
PSK=$(wg genpsk)
HUB_PUB=$(wg show "$IFACE" public-key)
echo "peer $NAME @ $IP  publicKey=$PUB"

# --- persist: append peer block to the conf (survives reboot) -----------------
ALLOWED="$IP/32"
[[ -n "$LAN" ]] && ALLOWED="$IP/32, $LAN"
[[ -f "$CONF" ]] || { echo "error: $CONF not found"; exit 1; }
cp "$CONF" "$CONF.bak.$(date +%s)"
{
  echo
  echo "# $NAME (added by manual-peer-add.sh)"
  echo "[Peer]"
  echo "PublicKey = $PUB"
  echo "PresharedKey = $PSK"
  echo "AllowedIPs = $ALLOWED"
  echo
} >> "$CONF"
chmod 600 "$CONF"
echo "persisted to $CONF (backup written)"

# --- apply to the LIVE interface, non-disruptively ---------------------------
# Use `wg set` with the PSK passed as a temp FILE PATH: some wg builds treat an
# inline PSK as a filename and fail fopen. wg addconf/setconf can NOT be used
# here because the hub conf is wg-quick format (Address/MTU/PostUp), which the
# low-level wg tools refuse to parse. wg set only touches this new peer — all
# existing peers/handshakes are untouched.
PSK_FILE=$(mktemp)
trap 'rm -f "$PSK_FILE"' EXIT
printf '%s\n' "$PSK" > "$PSK_FILE"
wg set "$IFACE" peer "$PUB" preshared-key "$PSK_FILE" allowed-ips "$ALLOWED"
echo "added to live $IFACE (no restart)"

# --- optionally emit the spoke config for the board --------------------------
if [[ -n "$EMIT" ]]; then
  {
    echo "[Interface]"
    echo "Address = ${IP}/24"
    echo "PrivateKey = $PRV"
    echo "MTU = $MTU"
    echo
    echo "[Peer]"
    echo "PublicKey = $HUB_PUB"
    echo "PresharedKey = $PSK"
    echo "AllowedIPs = 10.100.0.0/24"
    echo "Endpoint = $ENDPOINT"
    echo "PersistentKeepalive = 25"
  } > "$EMIT"
  chmod 600 "$EMIT"
  echo "spoke config written to $EMIT (copy to the board's $CONF)"
fi

echo
echo "done. verify:"
echo "  sudo wg show $IFACE"
