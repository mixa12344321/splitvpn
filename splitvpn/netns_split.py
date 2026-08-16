"""Network-namespace based per-application split tunneling.

A dedicated netns gets a veth pair to the host (NATed to the real default
interface) and its own openvpn instance with a normal full-tunnel default
route -- but that route only affects processes running inside the namespace.
Everything else on the system is completely unaffected.
"""
from __future__ import annotations

import os
import pwd
import subprocess
from pathlib import Path

from . import ip_utils
from .state import SessionState

_HOST_SUFFIX = 1
_NS_SUFFIX = 2


def setup_namespace(state: SessionState, subnet_index: int) -> None:
    ns = state.netns_name
    veth_h, veth_n = state.veth_host, state.veth_ns
    if not ns or not veth_h or not veth_n:
        raise RuntimeError("netns/veth names must be set before setup_namespace()")

    host_addr = f"10.200.{subnet_index}.{_HOST_SUFFIX}/30"
    ns_addr = f"10.200.{subnet_index}.{_NS_SUFFIX}/30"
    state.host_addr, state.ns_addr = host_addr, ns_addr

    ip_utils.netns_add(ns)
    ip_utils.run(["ip", "link", "add", veth_h, "type", "veth", "peer", "name", veth_n])
    ip_utils.run(["ip", "link", "set", veth_n, "netns", ns])
    ip_utils.run(["ip", "addr", "add", host_addr, "dev", veth_h])
    ip_utils.run(["ip", "link", "set", veth_h, "up"])
    ip_utils.netns_exec(ns, ["ip", "addr", "add", ns_addr, "dev", veth_n])
    ip_utils.netns_exec(ns, ["ip", "link", "set", veth_n, "up"])
    ip_utils.netns_exec(ns, ["ip", "link", "set", "lo", "up"])
    host_ip = host_addr.split("/")[0]
    ip_utils.netns_exec(ns, ["ip", "route", "add", "default", "via", host_ip])

    ip_utils.sysctl_set("net.ipv4.ip_forward", "1")

    default = ip_utils.get_default_route()
    nat_iface = default["dev"] if default else None
    state.nat_iface = nat_iface
    ns_subnet = f"10.200.{subnet_index}.0/30"
    if nat_iface:
        rules = [
            ["-t", "nat", "-A", "POSTROUTING", "-s", ns_subnet, "-o", nat_iface, "-j", "MASQUERADE"],
            ["-A", "FORWARD", "-i", veth_h, "-o", nat_iface, "-j", "ACCEPT"],
            ["-A", "FORWARD", "-i", nat_iface, "-o", veth_h,
             "-m", "state", "--state", "ESTABLISHED,RELATED", "-j", "ACCEPT"],
        ]
        for rule in rules:
            ip_utils.run(["iptables"] + rule)
            state.iptables_rules.append(rule)

    state.save()


def teardown_namespace(state: SessionState) -> None:
    for rule in reversed(state.iptables_rules):
        undo = list(rule)
        for flag in ("-A", "-I"):
            if flag in undo:
                undo[undo.index(flag)] = "-D"
                break
        ip_utils.run(["iptables"] + undo, check=False)
    state.iptables_rules = []

    if state.veth_host:
        ip_utils.run(["ip", "link", "del", state.veth_host], check=False)
    if state.netns_name:
        ip_utils.netns_del(state.netns_name)

    state.status = "disconnected"
    state.save()


def write_resolv_conf(ns: str, dns_servers: list[str]) -> None:
    d = Path(f"/etc/netns/{ns}")
    d.mkdir(parents=True, exist_ok=True)
    content = "".join(f"nameserver {s}\n" for s in dns_servers)
    (d / "resolv.conf").write_text(content)


def launch_in_namespace(state: SessionState, uid: int, command: list[str],
                         env_extra: dict[str, str], log_path: Path) -> None:
    """Fire-and-forget: spawn command inside the namespace as the given uid."""
    if not state.netns_name:
        raise RuntimeError("session is not in per-application split mode")

    pw = pwd.getpwuid(uid)
    cmd = [
        "ip", "netns", "exec", state.netns_name,
        "setpriv", "--reuid", str(pw.pw_uid), "--regid", str(pw.pw_gid), "--init-groups", "--",
    ] + command

    env = os.environ.copy()
    env.update(env_extra)
    env["HOME"] = pw.pw_dir
    env["USER"] = pw.pw_name
    env["LOGNAME"] = pw.pw_name

    with open(log_path, "wb") as fh:
        subprocess.Popen(cmd, env=env, stdout=fh, stderr=fh, start_new_session=True)
