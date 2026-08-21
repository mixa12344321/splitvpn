"""On-disk state for active splitvpn sessions.

On Linux this lives under /run/splitvpn/<session>/ -- tmpfs on virtually
every distro, cleared on reboot, and state.json is written world-readable
so the unprivileged GUI can poll status without going through pkexec
again. On Windows there's no tmpfs equivalent exposed the same way, so we
use %PROGRAMDATA%\\splitvpn\\run\\<session>\\ instead; files created there by
the elevated helper inherit ProgramData's default ACL, which already
grants standard users Read & Execute, giving the same polling-without-
elevation behavior.
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    RUN_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "splitvpn" / "run"
else:
    RUN_DIR = Path("/run/splitvpn")


@dataclasses.dataclass
class SessionState:
    session: str
    mode: str                              # "none" | "routes" | "netns"
    profile_name: str
    status: str = "starting"               # starting|connecting|connected|error|disconnected
    pid: int | None = None
    tun_dev: str | None = None
    tun_if_index: int | None = None        # Windows: interface index (from openvpn's dev_idx)
    tun_local_ip: str | None = None        # Windows: tunnel's own local address (ifconfig_local)
    app_split_daemon_pid: int | None = None  # Windows: process hosting the WinDivert AppSplitEngine
    route_vpn_gateway: str | None = None
    trusted_ip: str | None = None
    orig_default: dict | None = None
    added_routes: list[str] = dataclasses.field(default_factory=list)
    ipv6_disabled_by_us: bool = False
    ipv6_prior_state: bool = False
    netns_name: str | None = None
    veth_host: str | None = None
    veth_ns: str | None = None
    host_addr: str | None = None
    ns_addr: str | None = None
    nat_iface: str | None = None
    iptables_rules: list[list[str]] = dataclasses.field(default_factory=list)
    tracked_pids: list[int] = dataclasses.field(default_factory=list)  # Windows app-split
    error: str | None = None
    created_at: float = dataclasses.field(default_factory=time.time)
    updated_at: float = dataclasses.field(default_factory=time.time)

    @property
    def dir(self) -> Path:
        return RUN_DIR / self.session

    @property
    def state_file(self) -> Path:
        return self.dir / "state.json"

    def save(self) -> None:
        self.updated_at = time.time()
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(dataclasses.asdict(self), indent=2))
        tmp.chmod(0o644)
        tmp.replace(self.state_file)
        self.state_file.chmod(0o644)

    @classmethod
    def load(cls, session: str) -> SessionState:
        path = RUN_DIR / session / "state.json"
        data = json.loads(path.read_text())
        return cls(**data)


def list_sessions() -> list[SessionState]:
    if not RUN_DIR.exists():
        return []
    out = []
    for entry in RUN_DIR.iterdir():
        state_file = entry / "state.json"
        if state_file.exists():
            try:
                out.append(SessionState.load(entry.name))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
    return out
