"""Root-privileged CLI: performs all networking operations for splitvpn.

Invoked in three ways:
  * directly, via `pkexec splitvpn-helper <connect|disconnect|run-app|status>`
    on Linux, or via UAC elevation (win_elevate.run_elevated) on Windows,
    triggered by the unprivileged GUI;
  * by openvpn itself, as the --up/--down script (`_up-hook`/`_down-hook`),
    while already running elevated as a child of the daemonized openvpn
    process this helper launched;
  * on Windows only, as a detached background process (`_app-split-daemon`)
    hosting the WinDivert-based per-application split engine for the
    lifetime of a "split by application" session -- see win_app_split.py's
    module docstring for why that needs a persistent process on Windows
    but not on Linux (network namespaces persist in the kernel on their
    own; WinDivert packet redirection runs in Python threads that don't).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes

    from . import win_app_split, win_elevate
    from . import win_ip_utils as ip_utils
    from . import win_route_split as route_split
else:
    import pwd

    from . import ip_utils, netns_split, route_split

from .ovpn_parser import build_launch_config, parse_ovpn
from .state import SessionState, list_sessions

log = logging.getLogger("splitvpn.helper")


def _find_openvpn() -> str:
    found = shutil.which("openvpn")
    if found:
        return found
    if IS_WINDOWS:
        default = Path(r"C:\Program Files\OpenVPN\bin\openvpn.exe")
        if default.exists():
            return str(default)
    return "openvpn"


OPENVPN = _find_openvpn()


def _require_root() -> None:
    if IS_WINDOWS:
        if not ctypes.windll.shell32.IsUserAnAdmin():
            print("splitvpn-helper must run elevated (Administrator)", file=sys.stderr)
            sys.exit(1)
    elif os.geteuid() != 0:
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


def _emit(args: argparse.Namespace, result: dict) -> None:
    """Print the result as JSON, and -- when invoked via Windows UAC
    elevation, which gives no stdout pipe back to the caller -- also
    write it to --output-file for win_elevate.run_elevated() to read.
    """
    print(json.dumps(result))
    out_file = getattr(args, "output_file", None)
    if out_file:
        Path(out_file).write_text(json.dumps(result), encoding="utf-8")


def _kill_process(pid: int, *, graceful_timeout: float = 5.0) -> None:
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
        deadline = time.time() + graceful_timeout
        while time.time() < deadline:
            probe = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, check=False)
            if str(pid) not in probe.stdout:
                return
            time.sleep(0.1)
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + graceful_timeout
    while time.time() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cmd_connect(args: argparse.Namespace) -> int:
    _require_root()
    if IS_WINDOWS:
        ip_utils.run_ps("$null", check=False)  # cheap sanity check that PowerShell itself works
    else:
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
        if not IS_WINDOWS:
            auth_file.chmod(0o600)

    # script-security 2 (deliberately not 3) means openvpn execve()s this
    # directly rather than going through a shell, so $PATH is not searched
    # -- the up/down command needs to be an absolute path. openvpn's
    # up/down directive also accepts exactly one parameter, which on
    # Windows is used as a literal executable path with no further
    # word-splitting -- so "helper --session x" as one value doesn't work
    # there. A tiny per-session wrapper script sidesteps both issues on
    # both platforms: openvpn always gets a single, bare, existing script
    # path, and the wrapper itself carries the real arguments.
    helper_path = shutil.which("splitvpn-helper") or os.path.realpath(sys.argv[0])
    if IS_WINDOWS:
        up_hook = state.dir / "up.cmd"
        down_hook = state.dir / "down.cmd"
        hook_log = state.dir / "hook.log"
        up_hook.write_text(
            f'@echo off\r\n"{helper_path}" _up-hook --session {session} %* >> "{hook_log}" 2>&1\r\n'
        )
        down_hook.write_text(
            f'@echo off\r\n"{helper_path}" _down-hook --session {session} %* >> "{hook_log}" 2>&1\r\n'
        )
    else:
        up_hook = state.dir / "up.sh"
        down_hook = state.dir / "down.sh"
        up_hook.write_text(f'#!/bin/sh\nexec "{helper_path}" _up-hook --session {session} "$@"\n')
        down_hook.write_text(f'#!/bin/sh\nexec "{helper_path}" _down-hook --session {session} "$@"\n')
        up_hook.chmod(0o700)
        down_hook.chmod(0o700)

    if IS_WINDOWS:
        # Windows has no netns equivalent: "split by application" is done
        # entirely by WinDivert packet redirection (win_app_split.py),
        # which -- like "split by subnet" -- needs openvpn to leave the
        # system's own routing table alone.
        route_noexec = mode in ("routes", "netns")
    else:
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
        up_script=str(up_hook),
        down_script=str(down_hook),
        auth_file=auth_file,
        route_noexec=route_noexec,
    )
    config_path = state.dir / "config.ovpn"
    config_path.write_text(config_text)
    if not IS_WINDOWS:
        config_path.chmod(0o600)

    log_path = state.dir / "openvpn.log"

    state.status = "connecting"
    state.save()

    if IS_WINDOWS:
        # openvpn's own --daemon (FreeConsole()-based) has been observed to
        # fail outright ("daemon() failed or unsupported", errno=6) when
        # the parent process itself has no normal console -- which is
        # exactly the case here, launched via UAC's ShellExecuteExW. Windows
        # doesn't need fork()-style daemonizing to run something in the
        # background anyway, so we detach it ourselves instead and manage
        # its lifetime (pid, log) directly, the same way _app-split-daemon
        # is spawned below.
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        ovpn_cmd = [OPENVPN, "--config", str(config_path)]
        with open(log_path, "wb") as fh:
            proc = subprocess.Popen(
                ovpn_cmd, stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            )
        state.pid = proc.pid
        state.save()

        # openvpn fails fast (bad config, can't reach remote's UDP port
        # locally, etc.) -- give it a moment so a fast failure is reported
        # back as an error rather than a false "connecting".
        time.sleep(1.0)
        if proc.poll() is not None and proc.returncode != 0:
            state.status = "error"
            tail = log_path.read_text(errors="replace").strip().splitlines() if log_path.exists() else []
            state.error = "\n".join(tail[-5:]) or f"openvpn exited with code {proc.returncode}"
            state.save()
            _emit(args, {"session": session, "status": "error", "error": state.error})
            return 1
    else:
        pid_path = state.dir / "openvpn.pid"
        ovpn_cmd = [OPENVPN, "--config", str(config_path), "--daemon",
                    "--writepid", str(pid_path), "--log", str(log_path)]
        if mode == "netns":
            ovpn_cmd = ["ip", "netns", "exec", state.netns_name] + ovpn_cmd

        proc = subprocess.run(ovpn_cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            state.status = "error"
            state.error = proc.stderr.strip() or proc.stdout.strip() or "openvpn failed to start"
            state.save()
            _emit(args, {"session": session, "status": "error", "error": state.error})
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

    _emit(args, {"session": session, "status": "connecting"})
    return 0


def cmd_up_hook(args: argparse.Namespace) -> int:
    _require_root()
    state = SessionState.load(args.session)
    rules = json.loads((state.dir / "rules.json").read_text())

    state.tun_dev = os.environ.get("dev")
    # route_vpn_gateway isn't populated for classic point-to-point (net30)
    # tun setups without --topology subnet; ifconfig_remote (the peer's tun
    # address) is the correct via-address to fall back to in that case.
    state.route_vpn_gateway = os.environ.get("route_vpn_gateway") or os.environ.get("ifconfig_remote")
    state.trusted_ip = os.environ.get("trusted_ip")
    if IS_WINDOWS:
        dev_idx = os.environ.get("dev_idx")
        state.tun_if_index = int(dev_idx) if dev_idx else None
        state.tun_local_ip = os.environ.get("ifconfig_local")
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
    elif state.mode == "netns" and IS_WINDOWS:
        _start_app_split_daemon(state)
        state.status = "connected"
        state.save()
    elif state.mode == "netns":
        if dns_servers and state.netns_name:
            netns_split.write_resolv_conf(state.netns_name, dns_servers)
        state.status = "connected"
        state.save()
    else:
        state.status = "connected"
        state.save()

    return 0


def _start_app_split_daemon(state: SessionState) -> None:
    helper_path = shutil.which("splitvpn-helper") or os.path.realpath(sys.argv[0])
    log_path = state.dir / "app-split-daemon.log"
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    with open(log_path, "wb") as fh:
        proc = subprocess.Popen(
            [helper_path, "_app-split-daemon", "--session", state.session],
            stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
    state.app_split_daemon_pid = proc.pid
    state.save()


def cmd_app_split_daemon(args: argparse.Namespace) -> int:
    """Windows only: hosts win_app_split.AppSplitEngine for the lifetime of
    a "split by application" session. Spawned detached by cmd_up_hook,
    polls state.json for newly-launched app PIDs to track and for the
    session ending (killed outright by cmd_disconnect otherwise).
    """
    _require_root()
    state = SessionState.load(args.session)
    if not state.tun_local_ip or state.tun_if_index is None or not state.orig_default:
        log.error("missing tunnel info, cannot start app-split engine")
        return 1

    engine = win_app_split.AppSplitEngine(
        tunnel_ip=state.tun_local_ip,
        tun_if_index=state.tun_if_index,
        real_if_index=state.orig_default["if_index"],
    )
    engine.start()
    known_pids: set[int] = set()
    try:
        while True:
            time.sleep(1.0)
            try:
                fresh = SessionState.load(args.session)
            except (OSError, json.JSONDecodeError):
                break
            if fresh.status not in ("connecting", "connected"):
                break
            for pid in fresh.tracked_pids:
                if pid not in known_pids:
                    engine.track(pid)
                    known_pids.add(pid)
    finally:
        engine.stop()
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
        _kill_process(state.pid)

    if IS_WINDOWS and state.app_split_daemon_pid:
        _kill_process(state.app_split_daemon_pid, graceful_timeout=1.0)

    # Re-read: the down-script (run by openvpn itself on graceful exit)
    # may already have performed route teardown by this point. On
    # Windows, openvpn is force-killed rather than sent a graceful
    # SIGTERM-equivalent, so this is normally the path that actually
    # tears routes down there, not just a fallback.
    state = SessionState.load(args.session)
    if state.mode == "routes" and state.added_routes:
        route_split.teardown_split_routes(state)
    if state.mode == "netns" and not IS_WINDOWS:
        for pid in ip_utils.netns_pids(state.netns_name or ""):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        netns_split.teardown_namespace(state)

    state.status = "disconnected"
    state.save()
    _emit(args, {"session": state.session, "status": "disconnected"})
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    print(json.dumps([s.__dict__ for s in list_sessions()], default=str))
    return 0


def cmd_run_app(args: argparse.Namespace) -> int:
    _require_root()
    state = SessionState.load(args.session)
    if state.mode != "netns":
        _emit(args, {"status": "error", "error": "session is not in per-application split mode"})
        return 1

    if IS_WINDOWS:
        log_path = state.dir / f"app-{uuid.uuid4().hex[:6]}.log"
        pid = win_elevate.launch_deelevated(args.command, log_path)
        state.tracked_pids.append(pid)
        state.save()
        _emit(args, {"launched": args.command, "pid": pid})
        return 0

    if not state.netns_name:
        _emit(args, {"status": "error", "error": "session is not in per-application split mode"})
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

    _emit(args, {"launched": args.command, "user": pw.pw_name})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="splitvpn-helper")
    sub = p.add_subparsers(dest="action", required=True)

    # ShellExecuteExW (Windows UAC elevation) gives no stdout pipe back to
    # the caller, so every subcommand that client.py might invoke that way
    # accepts an optional --output-file to additionally write its JSON
    # result to (see _emit()). Harmless/unused on Linux, where pkexec
    # already captures stdout normally.
    c = sub.add_parser("connect")
    c.add_argument("--ovpn", required=True)
    c.add_argument("--rules", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--auth-file")
    c.add_argument("--output-file")
    c.set_defaults(func=cmd_connect)

    d = sub.add_parser("disconnect")
    d.add_argument("--session", required=True)
    d.add_argument("--output-file")
    d.set_defaults(func=cmd_disconnect)

    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)

    ra = sub.add_parser("run-app")
    ra.add_argument("--session", required=True)
    ra.add_argument("--display")
    ra.add_argument("--wayland-display")
    ra.add_argument("--xauthority")
    ra.add_argument("--dbus-session-bus-address")
    ra.add_argument("--output-file")
    ra.add_argument("command", nargs=argparse.REMAINDER)
    ra.set_defaults(func=cmd_run_app)

    # openvpn always appends positional args to --up/--down scripts (dev,
    # tun-mtu, link-mtu, ifconfig_local, ifconfig_remote, "init"/"restart");
    # we read everything we need from the environment instead, but still
    # need to accept and ignore whatever openvpn tacks on.
    uh = sub.add_parser("_up-hook")
    uh.add_argument("--session", required=True)
    uh.add_argument("openvpn_args", nargs="*")
    uh.set_defaults(func=cmd_up_hook)

    dh = sub.add_parser("_down-hook")
    dh.add_argument("--session", required=True)
    dh.add_argument("openvpn_args", nargs="*")
    dh.set_defaults(func=cmd_down_hook)

    asd = sub.add_parser("_app-split-daemon")
    asd.add_argument("--session", required=True)
    asd.set_defaults(func=cmd_app_split_daemon)

    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                         format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - surfaced to the GUI as JSON
        log.exception("command failed")
        _emit(args, {"status": "error", "error": str(exc)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
