"""Thin wrappers around external networking tools used by the privileged helper."""
from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess

log = logging.getLogger("splitvpn.ip_utils")

# run() is a generic subprocess wrapper reused for every `ip`/`iptables`/
# `sysctl` call the helper makes. No argument *value* from cmd is ever
# included in a log message or error string -- only the program name and
# an argument count -- so a future call site can't accidentally leak
# sensitive data through this function's debug/error output, and nobody
# needs to prove per-call-site that nothing sensitive is passed in. The
# command's own stderr (never derived from our input) is still surfaced
# in full for actual troubleshooting.
def _program_name(cmd: list[str]) -> str:
    return cmd[0] if cmd else "<empty command>"


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stderr: str):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{_program_name(cmd)} failed ({returncode}): {stderr.strip()}")


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    log.debug("+ %s (%d arg(s))", _program_name(cmd), max(len(cmd) - 1, 0))
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
