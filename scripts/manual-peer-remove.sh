#!/usr/bin/env bash
# manual-peer-remove.sh — remove a WireGuard spoke from a LIVE hub without
# touching other peers. Mirror of manual-peer-add.sh.
#
# - removes the peer from the live interface (`wg set ... peer <pub> remove`)
# - deletes its [Peer] block from /etc/wireguard/wg0.conf (wg-quick format),
#   keeping a timestamped backup of the conf.
#
# Usage:
#   manual-peer-remove.sh --name <board> [--iface wg0]
#
# Run as root on the hub. Requires wg.
set -euo pipefail

IFACE=wg0
NAME=""
CONF="/etc/wireguard/wg0.conf"

usage() {
  cat <<'EOF'
Remove a WireGuard spoke from a LIVE hub without touching other peers.

Backs up the conf, removes the peer from the live interface (non-disruptive)
and deletes its [Peer] block from /etc/wireguard/wg0.conf (wg-quick format).

Usage:
  manual-peer-remove.sh --name <board> [--iface wg0]

Run as root on the hub. Requires wg.
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --iface) IFACE="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1"; usage ;;
  esac
done

[[ -n "$NAME" ]] || usage
command -v wg >/dev/null 2>&1 || { echo "error: wg not found"; exit 1; }
[[ -f "$CONF" ]] || { echo "error: $CONF not found"; exit 1; }

MARKER="# $NAME (added by manual-peer-add.sh)"

# --- locate the peer block (lines from the marker comment to next block) -----
# awk prints the line range from the marker line to the end of its [Peer] block
# (i.e. up to the next blank line or the next [Interface]/[Peer] header).
BLOCK="$(awk -v m="$MARKER" '
  index($0, m) { inblock=1; print; next }
  inblock && $0 == "" { exit }
  inblock { print }
' "$CONF")"

if [[ -z "$BLOCK" ]]; then
  echo "error: no peer block marked '$MARKER' found in $CONF"
  echo "if it was added by hand, remove it manually (see backup for reference)."
  exit 1
fi

PUBKEY="$(printf '%s\n' "$BLOCK" | awk '/^PublicKey[ ]*=/ {gsub(/^[^=]*=[ ]*/,""); print; exit}')"
if [[ -z "$PUBKEY" ]]; then
  echo "error: could not parse PublicKey from the marked peer block"; exit 1
fi

echo "peer: $NAME  publicKey=$PUBKEY"

# --- remove from the live interface (non-disruptive) --------------------------
wg set "$IFACE" peer "$PUBKEY" remove
echo "removed from live $IFACE (other peers untouched)"

# --- delete the block from the persisted conf (with backup) -------------------
# A wg block is delimited by blank lines: the marker line starts it, and the
# first blank line after it ends it. Delete everything in between (inclusive).
cp "$CONF" "$CONF.bak.$(date +%s)"
awk -v m="$MARKER" '
  index($0, m) { inblock=1; next }
  inblock && $0 == "" { inblock=0; next }   # trailing blank line closes it
  inblock { next }
  { print }
' "$CONF" > "$CONF.tmp" && mv "$CONF.tmp" "$CONF"
chmod 600 "$CONF"
echo "removed $NAME block from $CONF (backup written)"

echo
echo "verify: sudo wg show $IFACE"
