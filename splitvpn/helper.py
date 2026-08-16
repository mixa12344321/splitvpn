"""Root-privileged CLI: performs all networking operations for splitvpn.

Invoked in two ways:
  * directly, via `pkexec splitvpn-helper <connect|disconnect|run-app|status>`,
    triggered by the unprivileged GUI;
  * by openvpn itself, as the --up/--down script (`_up-hook`/`_down-hook`),
    while already running as root as a child of the daemonized openvpn
    process this helper launched.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import ip_utils, netns_split, route_split
from .ovpn_parser import build_launch_config, parse_ovpn
from .state import SessionState, list_sessions

log = logging.getLogger("splitvpn.helper")

OPENVPN = "openvpn"


def _require_root() -> None:
    if os.geteuid() != 0:
        print("splitvpn-helper must run as root (invoked via pkexec)", file=sys.stderr)
        sys.exit(1)


def _real_invoking_uid() -> int:
    for key in ("PKEXEC_UID", "SUDO_UID"):
        v = os.environ.get(key)
        if v is not None:
            return int(v)
    return os.getuid()


def _next_subnet_index() -> int:
    used = set()
    for s in list_sessions():
        if s.host_addr:
            try:
                used.add(int(s.host_addr.split(".")[2]))
            except (IndexError, ValueError):
                pass
    for i in range(2, 250):
        if i not in used:
            return i
    raise RuntimeError("no free split-vpn subnet available")


def cmd_connect(args: argparse.Namespace) -> int:
    _require_root()
    ip_utils.require_tools("ip", "openvpn")

    session = uuid.uuid4().hex[:10]
    rules = json.loads(Path(args.rules).read_text())
    mode = rules.get("split_type", "none")
    if mode not in ("none", "routes", "netns"):
        raise ValueError(f"invalid split_type {mode!r}")

    state = SessionState(session=session, mode=mode, profile_name=args.name)
    state.dir.mkdir(parents=True, exist_ok=True)
    state.orig_default = ip_utils.get_default_route()

    profile = parse_ovpn(Path(args.ovpn))

    auth_file = None
    if args.auth_file:
        auth_file = state.dir / "auth.txt"
        shutil.copy(args.auth_file, auth_file)
        auth_file.chmod(0o600)

    up_hook = f"splitvpn-helper _up-hook --session {session}"
    down_hook = f"splitvpn-helper _down-hook --session {session}"
    route_noexec = mode == "routes"

    if mode == "netns":
        idx = _next_subnet_index()
        state.netns_name = f"svpn-{session[:8]}"
        state.veth_host = f"svh-{session[:8]}"
        state.veth_ns = f"svn-{session[:8]}"
        netns_split.setup_namespace(state, idx)

    (state.dir / "rules.json").write_text(json.dumps(rules))

    config_text = build_launch_config(
        profile,
        up_script=up_hook,
        down_script=down_hook,
        auth_file=auth_file,
        route_noexec=route_noexec,
    )
    config_path = state.dir / "config.ovpn"
    config_path.write_text(config_text)
    config_path.chmod(0o600)

    log_path = state.dir / "openvpn.log"
    pid_path = state.dir / "openvpn.pid"

    ovpn_cmd = [OPENVPN, "--config", str(config_path), "--daemon",
                "--writepid", str(pid_path), "--log", str(log_path)]
    if mode == "netns":
        ovpn_cmd = ["ip", "netns", "exec", state.netns_name] + ovpn_cmd

    state.status = "connecting"
    state.save()

    proc = subprocess.run(ovpn_cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        state.status = "error"
        state.error = proc.stderr.strip() or proc.stdout.strip() or "openvpn failed to start"
        state.save()
        print(json.dumps({"session": session, "status": "error", "error": state.error}))
        return 1

    for _ in range(50):
        if pid_path.exists():
            break
        time.sleep(0.1)
    if pid_path.exists():
        try:
            state.pid = int(pid_path.read_text().strip())
        except ValueError:
            pass
        state.save()

    print(json.dumps({"session": session, "status": "connecting"}))
    return 0


def cmd_up_hook(args: argparse.Namespace) -> int:
    _require_root()
    state = SessionState.load(args.session)
    rules = json.loads((state.dir / "rules.json").read_text())

    state.tun_dev = os.environ.get("dev")
    state.route_vpn_gateway = os.environ.get("route_vpn_gateway")
    state.trusted_ip = os.environ.get("trusted_ip")
    state.save()

    dns_servers = []
    i = 1
    while True:
        opt = os.environ.get(f"foreign_option_{i}")
        if not opt:
            break
        parts = opt.split()
        if len(parts) >= 3 and parts[0] == "dhcp-option" and parts[1] == "DNS":
            dns_servers.append(parts[2])
        i += 1

    if state.mode == "routes":
        route_split.apply_split_routes(state, rules)
    elif state.mode == "netns":
        if dns_servers and state.netns_name:
            netns_split.write_resolv_conf(state.netns_name, dns_servers)
        state.status = "connected"
        state.save()
    else:
        state.status = "connected"
        state.save()

    return 0


def cmd_down_hook(args: argparse.Namespace) -> int:
    _require_root()
    try:
        state = SessionState.load(args.session)
    except (OSError, json.JSONDecodeError):
        return 0
    if state.mode == "routes":
        route_split.teardown_split_routes(state)
    else:
        state.status = "disconnected"
        state.save()
    return 0


def cmd_disconnect(args: argparse.Namespace) -> int:
    _require_root()
    state = SessionState.load(args.session)

    if state.pid:
        try:
            os.kill(state.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(50):
            if not Path(f"/proc/{state.pid}").exists():
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(state.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # Re-read: the down-script (run by openvpn itself on graceful exit)
    # already performed route teardown by this point in the common case.
    state = SessionState.load(args.session)
    if state.mode == "routes" and state.added_routes:
        route_split.teardown_split_routes(state)
    if state.mode == "netns":
        for pid in ip_utils.netns_pids(state.netns_name or ""):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        netns_split.teardown_namespace(state)

    state.status = "disconnected"
    state.save()
    print(json.dumps({"session": state.session, "status": "disconnected"}))
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print(json.dumps([s.__dict__ for s in list_sessions()], default=str))
    return 0


def cmd_run_app(args: argparse.Namespace) -> int:
    _require_root()
    state = SessionState.load(args.session)
    if state.mode != "netns" or not state.netns_name:
        print(json.dumps({"status": "error", "error": "session is not in per-application split mode"}))
        return 1

    uid = _real_invoking_uid()
    pw = pwd.getpwuid(uid)

    env_extra = {}
    for env_key, dest in (
        ("DISPLAY", "display"),
        ("WAYLAND_DISPLAY", "wayland_display"),
        ("XAUTHORITY", "xauthority"),
        ("DBUS_SESSION_BUS_ADDRESS", "dbus_session_bus_address"),
    ):
        value = getattr(args, dest, None)
        if value:
            env_extra[env_key] = value

    log_path = state.dir / f"app-{uuid.uuid4().hex[:6]}.log"
    netns_split.launch_in_namespace(state, uid, args.command, env_extra, log_path)

    print(json.dumps({"launched": args.command, "user": pw.pw_name}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="splitvpn-helper")
    sub = p.add_subparsers(dest="action", required=True)

    c = sub.add_parser("connect")
    c.add_argument("--ovpn", required=True)
    c.add_argument("--rules", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--auth-file")
    c.set_defaults(func=cmd_connect)

    d = sub.add_parser("disconnect")
    d.add_argument("--session", required=True)
    d.set_defaults(func=cmd_disconnect)

    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)

    ra = sub.add_parser("run-app")
    ra.add_argument("--session", required=True)
    ra.add_argument("--display")
    ra.add_argument("--wayland-display")
    ra.add_argument("--xauthority")
    ra.add_argument("--dbus-session-bus-address")
    ra.add_argument("command", nargs=argparse.REMAINDER)
    ra.set_defaults(func=cmd_run_app)

    uh = sub.add_parser("_up-hook")
    uh.add_argument("--session", required=True)
    uh.set_defaults(func=cmd_up_hook)

    dh = sub.add_parser("_down-hook")
    dh.add_argument("--session", required=True)
    dh.set_defaults(func=cmd_down_hook)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        log.exception("command failed")
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
