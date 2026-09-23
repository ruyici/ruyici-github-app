import importlib
import json

import pytest


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Point state/key paths at a temp dir, then reload the module so the
    module-level path constants resolve to the temp dir."""
    monkeypatch.setenv("RUIYICI_WG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("RUIYICI_WG_CONF_FILE", str(tmp_path / "wg0.conf"))
    wg = importlib.import_module("scripts.wg")
    importlib.reload(wg)
    return wg


def _wg():
    return importlib.import_module("scripts.wg")


def test_allocate_first_ip_after_hub():
    wg = _wg()
    state = {"subnet": "10.100.0.0/24", "peers": {}}
    # 10.100.0.1 is the hub; first allocatable is .2
    assert wg.allocate_ip(state) == "10.100.0.2"


def test_allocate_with_fixed_free_ip():
    wg = _wg()
    state = {"subnet": "10.100.0.0/24", "peers": {}}
    assert wg.allocate_ip(state, "10.100.0.50") == "10.100.0.50"


def test_allocate_fixed_conflict_raises():
    wg = _wg()
    state = {"subnet": "10.100.0.0/24", "peers": {"a": {"ip": "10.100.0.50"}}}
    with pytest.raises(wg.StateError):
        wg.allocate_ip(state, "10.100.0.50")


def test_allocate_outside_subnet_raises():
    wg = _wg()
    state = {"subnet": "10.100.0.0/24", "peers": {}}
    with pytest.raises(wg.StateError):
        wg.allocate_ip(state, "192.168.9.9")


def test_allocate_skips_used():
    wg = _wg()
    state = {
        "subnet": "10.100.0.0/24",
        "peers": {"a": {"ip": "10.100.0.2"}, "b": {"ip": "10.100.0.3"}},
    }
    assert wg.allocate_ip(state) == "10.100.0.4"


def test_save_load_roundtrip(tmp_path):
    wg = _wg()
    state = {
        "subnet": "10.100.0.0/24",
        "peers": {"k3-17": {"ip": "10.100.0.4", "provider": "scaleway"}},
    }
    wg_dir = tmp_path / "state"
    wg_dir.mkdir()
    (wg_dir / "state.json").write_text(json.dumps(state))
    loaded = wg._load_state()
    assert loaded["peers"]["k3-17"]["ip"] == "10.100.0.4"


def test_hub_conf_body_includes_peer_lan():
    wg = _wg()
    state = {
        "subnet": "10.100.0.0/24",
        "hub_ip": "10.100.0.1",
        "port": 51820,
        "peers": {"k3-17": {"ip": "10.100.0.4", "lan": "192.168.1.0/24"}},
    }
    # write a peer public key + psk into the peer dir so the body renders
    wg._write_secret(wg.PEERS_KEYDIR / "k3-17" / "publickey", "pubkey-abc")
    wg._write_secret(wg.PEERS_KEYDIR / "k3-17" / "psk", "psk-abc")
    body = wg._hub_conf_body(state)
    assert "k3-17" in body
    assert "pubkey-abc" in body
    assert "10.100.0.4/32, 192.168.1.0/24" in body


def test_write_secret_restricts_perm(tmp_path):
    wg = _wg()
    path = tmp_path / "secret.key"
    wg._write_secret(path, "sekret")
    assert wg._read_secret(path) == "sekret"
    assert path.stat().st_mode & 0o777 == 0o600
