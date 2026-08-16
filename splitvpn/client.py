"""Unprivileged helper used by the GUI to talk to the privileged
splitvpn-helper (via pkexec) and to read session status.

Status/log reads never go through pkexec: state.json under /run/splitvpn is
written world-readable by the root helper specifically so the GUI can poll
it directly without repeated authentication prompts.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

HELPER_BIN = "splitvpn-helper"
RUN_DIR = Path("/run/splitvpn")


class HelperError(RuntimeError):
    pass


def _pkexec(args: list[str]) -> dict:
    proc = subprocess.run(["pkexec", HELPER_BIN] + args, capture_output=True, text=True, check=False)
    if proc.returncode in (126, 127):
        raise HelperError("Authentication was cancelled, or pkexec/splitvpn-helper is not installed.")

    lines = proc.stdout.strip().splitlines()
    last = lines[-1] if lines else ""
    try:
        data = json.loads(last)
    except (json.JSONDecodeError, IndexError):
        raise HelperError(
            proc.stderr.strip() or f"splitvpn-helper exited with code {proc.returncode}"
        ) from None
    if data.get("status") == "error":
        raise HelperError(data.get("error", "unknown error"))
    return data


def connect(ovpn_path: Path, name: str, rules: dict,
            username: str | None, password: str | None) -> dict:
    tmp_rules = Path(tempfile.mkstemp(prefix="splitvpn-rules-", suffix=".json")[1])
    tmp_auth = None
    try:
        tmp_rules.write_text(json.dumps(rules))
        args = ["connect", "--ovpn", str(ovpn_path), "--name", name, "--rules", str(tmp_rules)]

        if username is not None:
            tmp_auth = Path(tempfile.mkstemp(prefix="splitvpn-auth-", suffix=".txt")[1])
            tmp_auth.chmod(0o600)
            tmp_auth.write_text(f"{username}\n{password or ''}\n")
            args += ["--auth-file", str(tmp_auth)]

        return _pkexec(args)
    finally:
        tmp_rules.unlink(missing_ok=True)
        if tmp_auth:
            tmp_auth.unlink(missing_ok=True)


def disconnect(session: str) -> dict:
    return _pkexec(["disconnect", "--session", session])


def run_app(session: str, command: list[str]) -> dict:
    args = ["run-app", "--session", session]
    for env_var, flag in (
        ("DISPLAY", "--display"),
        ("WAYLAND_DISPLAY", "--wayland-display"),
        ("XAUTHORITY", "--xauthority"),
        ("DBUS_SESSION_BUS_ADDRESS", "--dbus-session-bus-address"),
    ):
        value = os.environ.get(env_var)
        if value:
            args += [flag, value]
    args += command
    return _pkexec(args)


def list_sessions() -> list[dict]:
    if not RUN_DIR.exists():
        return []
    out = []
    for entry in RUN_DIR.iterdir():
        state_file = entry / "state.json"
        if state_file.exists():
            try:
                out.append(json.loads(state_file.read_text()))
            except (OSError, json.JSONDecodeError):
                continue
    return out


def session_status(session: str) -> dict | None:
    state_file = RUN_DIR / session / "state.json"
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def tail_log(session: str, n: int = 200) -> str:
    log_file = RUN_DIR / session / "openvpn.log"
    if not log_file.exists():
        return ""
    try:
        lines = log_file.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])
