#!/usr/bin/env python3
"""ruyici WireGuard fleet helper.

Hub-and-spoke provisioning for a fleet of RISC-V boards behind one WireGuard
hub. Adding a peer is NON-DISRUPTIVE: it applies via `wg set` on the live
interface (never `wg-quick down/up` on an in-use network) and appends a
[Peer] block to the persisted config.

Secrets never live in this repo: the allocation state and all keys are written
under a root-only state directory. Only non-sensitive metadata (name/IP/
provider/board) is printed or can later be exported.

Architecture in use:
    WG hub            = the host at 10.100.0.1/24 (routing only)
    k3s server/CP     = 10.100.0.3 (k3s-server-root, runs main.py + k3s server)
    runner boards     = k3s agents joining https://10.100.0.3:6443 over WG

Usage (see docstring of each command):
    wg.py network init --hub-ip 10.100.0.1 --port 51820
    wg.py peer add --name k3-17 [--ip 10.100.0.4] [--provider scaleway]
    wg.py peer list
    wg.py peer remove --name k3-17
    wg.py verify
    wg.py node join-config --name k3-17 --out /tmp/k3-17.conf
    wg.py label-node --name k3-17 --label ruyici.dev/board=scaleway-em-rv1
"""

import argparse
import ipaddress
import json
import os
import secrets
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_IFACE = "wg0"
DEFAULT_SUBNET = "10.100.0.0/24"
DEFAULT_HUB_IP = "10.100.0.1"
DEFAULT_PORT = 51820
DEFAULT_MTU = 1400

def _state_dir() -> Path:
    return Path(os.environ.get("RUIYICI_WG_STATE_DIR", "/etc/ruyici-wg"))


def _conf_file() -> Path:
    return Path(os.environ.get("RUIYICI_WG_CONF_FILE", "/etc/wireguard/wg0.conf"))


STATE_DIR = _state_dir()
STATE_FILE = STATE_DIR / "state.json"
HUB_PRIKEY = STATE_DIR / "hub.privatekey"
HUB_PUBKEY = STATE_DIR / "hub.publickey"
PEERS_KEYDIR = STATE_DIR / "peers"  # one subdir per peer: privatekey, psk
CONF_FILE = _conf_file()


class StateError(RuntimeError):
    pass


@dataclass
class Peer:
    name: str
    ip: str
    public_key: str = ""
    private_key: str = ""
    psk: str = ""
    provider: str = ""
    board: str = ""
    lan: str = ""
    added: str = ""


def _now_utc() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat(" ", "minutes")


# --------------------------------------------------------------------------- #
# low-level shell helpers
# --------------------------------------------------------------------------- #
def _run(cmd: list[str], check: bool = True) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"command failed {shlex.join(cmd)}: {r.stderr.strip()}")
    return (r.stdout or "").strip()


def _ensure_state_dir() -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    (STATE_DIR / "peers").mkdir(mode=0o700, parents=True, exist_ok=True)


def _write_secret(path: Path, data: str) -> None:
    _ensure_state_dir()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes((data + "\n").encode())
    os.chmod(path, 0o600)


def _read_secret(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


def _write_conf(body: str) -> None:
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONF_FILE, "w") as f:
        f.write(body)
    os.chmod(CONF_FILE, 0o600)


# --------------------------------------------------------------------------- #
# state (allocation) persistence
# --------------------------------------------------------------------------- #
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            raise StateError(f"corrupt state file: {STATE_FILE}")
    return {"subnet": DEFAULT_SUBNET, "peers": {}}


def _save_state(state: dict) -> None:
    _ensure_state_dir()
    tmp = STATE_DIR / "state.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(STATE_FILE)


def _used_ips(state: dict) -> set:
    used = {p["ip"] for p in state["peers"].values()}
    used.add(str(ipaddress.ip_network(state["subnet"]).network_address + 1))  # hub
    return used


def allocate_ip(state: dict, want: str | None = None) -> str:
    """Return the next free host address in the subnet, or `want` if free."""
    net = ipaddress.ip_network(state["subnet"], strict=False)
    if want:
        ip = ipaddress.ip_address(want)
        if ip not in net:
            raise StateError(f"{want} is outside subnet {net}")
    used = _used_ips(state)
    if want and want in used:
        raise StateError(f"IP {want} already allocated")
    if want:
        return want
    for host in net.hosts():
        if str(host) not in used:
            return str(host)
    raise StateError(f"no free address in {net}")


# --------------------------------------------------------------------------- #
# key generation (via wg, read-only w.r.t. the network)
# --------------------------------------------------------------------------- #
def _gen_private_key() -> str:
    return _run(["wg", "genkey"])


def _pub_of(priv: str) -> str:
    r = subprocess.run(
        ["wg", "pubkey"], input=priv.encode(), capture_output=True, check=False
    )
    if r.returncode != 0:
        raise RuntimeError("failed to derive public key")
    return r.stdout.decode().strip()


# --------------------------------------------------------------------------- #
# config builders (templated, no secrets)
# --------------------------------------------------------------------------- #
def _hub_conf_body(state: dict) -> str:
    lines = [
        "[Interface]",
        f"Address = {state['hub_ip']}/24",
        f"ListenPort = {state['port']}",
        f"PrivateKey = {_read_secret(HUB_PRIKEY)}",  # root-only
        "PostUp = iptables -A FORWARD -i wg0 -j ACCEPT; iptables -t nat -A POSTROUTING -o $(ip -4 route ls | head -1 | awk '{print $5}') -j MASQUERADE",
        "PreDown = iptables -D FORWARD -i wg0 -j ACCEPT; iptables -t nat -D POSTROUTING -o $(ip -4 route ls | head -1 | awk '{print $5}') -j MASQUERADE",
        "",
    ]
    for name, p in state["peers"].items():
        # peer files may live under the state dir; load keys there
        privdir = PEERS_KEYDIR / name
        pk = _read_secret(privdir / "publickey")
        psk = _read_secret(privdir / "psk")
        allowed = f"{p['ip']}/32"
        if p.get("lan"):
            allowed += f", {p['lan']}"
        lines += [
            f"# {name}",
            "[Peer]",
            f"PublicKey = {pk}",
            f"PresharedKey = {psk}",
            f"AllowedIPs = {allowed}",
            "",
        ]
    return "\n".join(lines)


def _spoke_config(p: Peer, state: dict, endpoint: str) -> str:
    privdir = PEERS_KEYDIR / p.name
    psk = _read_secret(privdir / "psk")
    hub_pub = _read_secret(HUB_PUBKEY)
    return f"""[Interface]
Address = {p.ip}/24
PrivateKey = {_read_secret(privdir / 'privatekey')}
MTU = {state.get('mtu', DEFAULT_MTU)}

[Peer]
PublicKey = {hub_pub}
PresharedKey = {psk}
AllowedIPs = {state['subnet']}
Endpoint = {endpoint}:{state['port']}
PersistentKeepalive = 25
"""


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_init(args: argparse.Namespace) -> None:
    _ensure_state_dir()
    state = _load_state()
    hub_ip = state.get("hub_ip") or args.hub_ip
    state.update(
        subnet=args.subnet,
        hub_ip=hub_ip,
        port=args.port,
        mtu=args.mtu,
        peers=state.get("peers", {}),
    )
    # (re)generate hub keys only if missing
    if not HUB_PRIKEY.exists():
        priv = _gen_private_key()
        _write_secret(HUB_PRIKEY, priv)
        _write_secret(HUB_PUBKEY, _pub_of(priv))
    _save_state(state)
    _write_conf(_hub_conf_body(state))
    _run(["systemctl", "enable", "--now", f"wg-quick@{args.iface}"])
    print(f"hub initialised: {CONF_FILE}, listening on {hub_ip}:{args.port}")


def cmd_peer_add(args: argparse.Namespace) -> None:
    state = _load_state()
    if args.name in state["peers"]:
        raise StateError(f"peer {args.name} already exists")
    ip = allocate_ip(state, args.ip)
    peers_dir = PEERS_KEYDIR / args.name
    peers_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    priv = _gen_private_key() if not _read_secret(peers_dir / "privatekey") else _read_secret(peers_dir / "privatekey")
    pub = _pub_of(priv) if not _read_secret(peers_dir / "publickey") else _read_secret(peers_dir / "publickey")
    psk = secrets.token_hex(32) if not _read_secret(peers_dir / "psk") else _read_secret(peers_dir / "psk")
    _write_secret(peers_dir / "privatekey", priv)
    _write_secret(peers_dir / "publickey", pub)
    _write_secret(peers_dir / "psk", psk)
    state["peers"][args.name] = {
        "ip": ip,
        "provider": args.provider or "",
        "board": args.board or "",
        "lan": args.lan or "",
        "added": _now_utc(),
    }
    _save_state(state)
    # persist (rewrite conf) then hot-add without restarting the interface
    _write_conf(_hub_conf_body(state))
    allowed_ips = f"{ip}/32" if not args.lan else f"{ip}/32, {args.lan}"
    _run([
        "wg", "set", args.iface, "peer", pub,
        "preshared-key", psk, "allowed-ips", allowed_ips,
    ])
    print(f"added peer {args.name} @ {ip} (non-disruptive, no restart)")
    if args.emit:
        endpoint = args.endpoint or "<hub-public-ip>"
        Path(args.emit).write_text(_spoke_config(Peer(args.name, ip), state, endpoint))
        os.chmod(args.emit, 0o600)
        print(f"spoke config written to {args.emit}")


def cmd_peer_list(_args: argparse.Namespace) -> None:
    state = _load_state()
    if not state["peers"]:
        print("no peers")
        return
    for name, p in state["peers"].items():
        print(f"{name}\t{p['ip']}\tprovider={p.get('provider','')}\tboard={p.get('board','')}\tlan={p.get('lan','')}\tadded={p.get('added','')}")


def cmd_peer_remove(args: argparse.Namespace) -> None:
    state = _load_state()
    if args.name not in state["peers"]:
        raise StateError(f"peer {args.name} does not exist")
    pub = _read_secret(PEERS_KEYDIR / args.name / "publickey")
    if pub:
        _run(["wg", "set", args.iface, "peer", pub, "remove"], check=False)
    del state["peers"][args.name]
    _save_state(state)
    _write_conf(_hub_conf_body(state))
    import shutil

    shutil.rmtree(PEERS_KEYDIR / args.name, ignore_errors=True)
    print(f"removed peer {args.name}")


def cmd_node_join_config(args: argparse.Namespace) -> None:
    state = _load_state()
    if args.name not in state["peers"]:
        raise StateError(f"peer {args.name} does not exist")
    p = Peer(args.name, state["peers"][args.name]["ip"])
    endpoint = args.endpoint or "<hub-public-ip>"
    print(_spoke_config(p, state, endpoint))


def cmd_label_node(args: argparse.Namespace) -> None:
    _run(["kubectl", "label", "node", args.name, args.label, "--overwrite"], check=False)
    print(f"labelled node {args.name} {args.label}")


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("network", help="initialise the hub")
    ssp = sp.add_subparsers(dest="subcommand", required=True)
    si = ssp.add_parser("init")
    si.add_argument("--subnet", default=DEFAULT_SUBNET)
    si.add_argument("--hub-ip", default=DEFAULT_HUB_IP)
    si.add_argument("--port", type=int, default=DEFAULT_PORT)
    si.add_argument("--mtu", type=int, default=DEFAULT_MTU)
    si.add_argument("--iface", default=DEFAULT_IFACE)
    si.set_defaults(func=cmd_init)

    pa = sub.add_parser("peer", help="manage peers")
    pp = pa.add_subparsers(dest="subcommand", required=True)
    add = pp.add_parser("add")
    add.add_argument("--name", required=True)
    add.add_argument("--ip", default=None, help="fixed IP (else auto-allocate)")
    add.add_argument("--provider", default="")
    add.add_argument("--board", default="")
    add.add_argument("--lan", default="", help="board LAN CIDR to route, e.g. 192.168.1.0/24")
    add.add_argument("--iface", default=DEFAULT_IFACE)
    add.add_argument("--endpoint", default="")
    add.add_argument("--emit", default="", help="write spoke config to this path")
    add.set_defaults(func=cmd_peer_add)

    ls = pp.add_parser("list")
    ls.set_defaults(func=cmd_peer_list)

    rm = pp.add_parser("remove")
    rm.add_argument("--name", required=True)
    rm.add_argument("--iface", default=DEFAULT_IFACE)
    rm.set_defaults(func=cmd_peer_remove)

    jc = sub.add_parser("node", help="node config helper")
    jj = jc.add_subparsers(dest="subcommand", required=True)
    join = jj.add_parser("join-config")
    join.add_argument("--name", required=True)
    join.add_argument("--endpoint", default="")
    join.set_defaults(func=cmd_node_join_config)

    ln = sub.add_parser("label-node")
    ln.add_argument("--name", required=True)
    ln.add_argument("--label", required=True)
    ln.set_defaults(func=cmd_label_node)

    v = sub.add_parser("verify", help="show WG handshakes and ping peers")
    v.set_defaults(func=cmd_verify)
    return p


def cmd_verify(_args: argparse.Namespace) -> None:
    try:
        print(_run(["wg", "show", DEFAULT_IFACE]))
    except RuntimeError as e:
        print(f"wg show failed: {e}")
    state = _load_state()
    for name, p in state["peers"].items():
        host = p["ip"]
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True,
            text=True,
            check=False,
        )
        print(f"{name}\t{host}\t{'OK' if r.returncode == 0 else 'unreachable'}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    try:
        main()
    except StateError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
