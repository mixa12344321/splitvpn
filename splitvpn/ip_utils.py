"""Thin wrappers around external networking tools used by the privileged helper."""
from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess

log = logging.getLogger("splitvpn.ip_utils")

# run() is only ever called (directly or via netns_exec) with these network
# utilities -- never with openvpn or anything that takes a credential as a
# CLI argument. Gating full-argument logging/error messages behind this
# allowlist means a future call site accidentally passing sensitive data
# through this generic wrapper gets its arguments redacted by default
# instead of landing in a debug log or an error message shown to the user.
_SAFE_TO_LOG_PROGRAMS = {"ip", "iptables", "sysctl"}


def _display(cmd: list[str]) -> str:
    if cmd and cmd[0] in _SAFE_TO_LOG_PROGRAMS:
        return " ".join(cmd)
    return f"{cmd[0]} [{len(cmd) - 1} arg(s) redacted]" if cmd else "<empty command>"


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{_display(cmd)} failed ({returncode}): {stderr.strip()}")


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("+ %s", _display(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stderr)
    return proc


def require_tools(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        raise RuntimeError(f"Missing required tools: {', '.join(missing)}")


def validate_cidr(value: str) -> str:
    """Raise ValueError if not a valid IPv4/IPv6 network; return its normalized form."""
    net = ipaddress.ip_network(value, strict=False)
    return str(net)


_DEFAULT_ROUTE_RE = re.compile(r"^default\s+via\s+(?P<gw>\S+)\s+dev\s+(?P<dev>\S+)")


def get_default_route() -> dict | None:
    proc = run(["ip", "-4", "route", "show", "default"], check=False)
    for line in proc.stdout.splitlines():
        m = _DEFAULT_ROUTE_RE.match(line.strip())
        if m:
            return {"gateway": m.group("gw"), "dev": m.group("dev")}
    return None


def route_replace(cidr: str, *, via: str | None = None, dev: str | None = None,
                   metric: int | None = None) -> None:
    cmd = ["ip", "route", "replace", cidr]
    if via:
        cmd += ["via", via]
    if dev:
        cmd += ["dev", dev]
    if metric is not None:
        cmd += ["metric", str(metric)]
    run(cmd)


def route_del(cidr: str, *, dev: str | None = None) -> None:
    cmd = ["ip", "route", "del", cidr]
    if dev:
        cmd += ["dev", dev]
    run(cmd, check=False)


def netns_add(name: str) -> None:
    run(["ip", "netns", "add", name])


def netns_del(name: str) -> None:
    run(["ip", "netns", "del", name], check=False)


def netns_exec(name: str, cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return run(["ip", "netns", "exec", name] + cmd, **kw)


def netns_pids(name: str) -> list[int]:
    proc = run(["ip", "netns", "pids", name], check=False)
    return [int(p) for p in proc.stdout.split() if p.isdigit()]


def sysctl_set(key: str, value: str) -> None:
    run(["sysctl", "-qw", f"{key}={value}"], check=False)


def sysctl_get(key: str) -> str:
    proc = run(["sysctl", "-n", key], check=False)
    return proc.stdout.strip()
