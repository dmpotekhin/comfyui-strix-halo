"""Stage NET-1: the bind-address guard and the path scoping that keeps members apart.

These are the two places where a mistake is expensive — publishing the studio on every interface,
or writing one member's files where another member's URL can reach them — so they are tested as
units, without the HTTP layer in the way.
"""
from __future__ import annotations

import socket

import pytest

from app.config import Settings, address_is_local, check_bind, is_loopback
from app.jobs import _prefix_outputs, _scope_output, _scoped_root


# ---------------------------------------------------------------- bind guard

def _settings(**over) -> Settings:
    base: dict[str, object] = {"lan_subnet": "192.168.31.0/24"}
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_loopback_is_always_allowed():
    s = _settings()
    for host in ("127.0.0.1", "localhost", "::1"):
        ok, reason = check_bind(host, s)
        assert ok, (host, reason)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "*"])
def test_every_interface_bind_is_refused(host):
    """0.0.0.0 would also open the studio on docker bridges and any VPN."""
    ok, reason = check_bind(host, _settings())
    assert not ok
    assert "every interface" in reason


def test_hostname_is_refused():
    ok, reason = check_bind("laptop.local", _settings())
    assert not ok
    assert "not an IP address" in reason


def test_address_outside_the_lan_subnet_is_refused():
    ok, reason = check_bind("192.172.0.5", _settings())  # neighbour of the subnet, not inside
    assert not ok
    assert "lan_subnet" in reason


def test_public_address_is_refused():
    ok, reason = check_bind("8.8.8.8", _settings(lan_subnet="0.0.0.0/0"))
    assert not ok
    assert "not an address of this machine" in reason


def test_address_inside_subnet_but_not_local_is_refused():
    # TEST-NET-1: reserved for documentation, so no machine will ever own it.
    ok, reason = check_bind("192.0.2.10", _settings(lan_subnet="192.0.2.0/24"))
    assert not ok
    assert "not an address of this machine" in reason


def test_broken_subnet_is_reported_not_crashed():
    ok, reason = check_bind("192.168.31.60", _settings(lan_subnet="not-a-network"))
    assert not ok
    assert "not a valid network" in reason


def test_empty_host_is_refused():
    ok, reason = check_bind("   ", _settings())
    assert not ok
    assert "empty" in reason


def _own_lan_ip() -> str | None:
    """This machine's outbound address on its default route, if it has one."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(0.2)
            s.connect(("192.0.2.1", 9))  # no packet is sent for UDP connect
            addr = s.getsockname()[0]
    except OSError:
        return None
    return None if addr.startswith("127.") else addr


def test_this_machines_lan_address_is_accepted():
    ip = _own_lan_ip()
    if ip is None:
        pytest.skip("machine has no non-loopback IPv4 address")
    subnet = ".".join(ip.split(".")[:3]) + ".0/24"
    ok, reason = check_bind(ip, _settings(lan_subnet=subnet))
    assert ok, reason
    assert address_is_local(ip)


def test_is_loopback_recognises_the_usual_spellings():
    assert is_loopback("127.0.0.1") and is_loopback("LOCALHOST") and is_loopback("::1")
    assert not is_loopback("192.168.31.60")


# ---------------------------------------------------------------- output scoping

def test_prefix_outputs_rewrites_save_prefixes():
    graph = {"7": {"class_type": "SaveImage",
                   "inputs": {"filename_prefix": "studio/test", "images": ["6", 0]}},
             "9": {"class_type": "SaveVideo",
                   "inputs": {"filename_prefix": "video/s5", "frames": ["8", 0]}}}
    out = _prefix_outputs(graph, "users/ab12")
    assert out["7"]["inputs"]["filename_prefix"] == "users/ab12/studio/test"
    assert out["9"]["inputs"]["filename_prefix"] == "users/ab12/video/s5"
    # Normalisation is idempotent: prefixing twice must not produce users/ab12/users/ab12/...
    again = _prefix_outputs(out, "users/ab12")
    assert again == out


def test_prefix_outputs_leaves_other_strings_alone():
    params = {"prompt": "a fox in the snow", "width": 512, "tags": ["studio"]}
    assert _prefix_outputs(params, "users/ab12") == params


def test_scope_output_makes_a_host_tool_path_project_relative():
    raw = {"filename": "clip.mp4", "subfolder": "studio", "type": "output", "kind": "video"}
    scoped = _scope_output(raw, "ab12")
    assert scoped["subfolder"] == "users/ab12/studio"
    assert scoped["filename"] == "clip.mp4"
    assert raw["subfolder"] == "studio"  # the caller's dict is not mutated


def test_scope_output_is_a_noop_without_an_owner():
    raw = {"filename": "clip.mp4", "subfolder": "studio"}
    assert _scope_output(raw, "") is raw


def test_scoped_root_creates_the_user_directory(tmp_path):
    root = _scoped_root(tmp_path, "ab12")
    assert root == tmp_path / "users" / "ab12"
    assert root.is_dir()


def test_scoped_root_refuses_a_traversing_id(tmp_path):
    for bad in ("../escape", "ab/12", "ab 12", "ab-12"):
        with pytest.raises(ValueError):
            _scoped_root(tmp_path, bad)
