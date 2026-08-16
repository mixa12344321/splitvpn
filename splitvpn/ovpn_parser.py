"""Parsing and launch-config augmentation for OpenVPN .ovpn profile files."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

_INLINE_TAG_RE = re.compile(r"^<(/?)([\w-]+)>$")

# Directives we always inject ourselves; any existing occurrence in the
# source profile is stripped before re-adding our own, to avoid ambiguous
# duplicate options being handed to openvpn.
_OVERRIDE_KEYS = {
    "up", "down", "script-security", "route-noexec", "pull-filter",
    "auth-user-pass", "up-restart",
}


@dataclass
class OvpnProfile:
    path: Path
    raw_lines: list[str]
    directives: dict[str, list[list[str]]] = field(default_factory=dict)
    inline_blocks: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def remotes(self) -> list[tuple[str, str, str]]:
        out = []
        for args in self.directives.get("remote", []):
            host = args[0] if len(args) > 0 else "?"
            port = args[1] if len(args) > 1 else "1194"
            proto = args[2] if len(args) > 2 else self._default_proto()
            out.append((host, port, proto))
        return out

    def _default_proto(self) -> str:
        for args in self.directives.get("proto", []):
            if args:
                return args[0]
        return "udp"

    @property
    def requires_auth_user_pass(self) -> bool:
        return "auth-user-pass" in self.directives

    @property
    def auth_user_pass_file(self) -> str | None:
        for args in self.directives.get("auth-user-pass", []):
            if args:
                return args[0]
        return None

    @property
    def cipher(self) -> str | None:
        for key in ("cipher", "data-ciphers"):
            for args in self.directives.get(key, []):
                if args:
                    return args[0]
        return None

    def summary(self) -> dict:
        remotes = self.remotes
        return {
            "name": self.name,
            "path": str(self.path),
            "remotes": [f"{h}:{p}/{proto}" for h, p, proto in remotes],
            "cipher": self.cipher or "default",
            "needs_credentials": self.requires_auth_user_pass and not self.auth_user_pass_file,
        }


def parse_ovpn(path: Path) -> OvpnProfile:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    directives: dict[str, list[list[str]]] = {}
    inline_blocks: dict[str, str] = {}

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line or line.startswith(("#", ";")):
            continue

        m = _INLINE_TAG_RE.match(line)
        if m and m.group(1) == "":
            tag = m.group(2)
            body_lines = []
            while i < n:
                closing = lines[i].strip()
                cm = _INLINE_TAG_RE.match(closing)
                if cm and cm.group(1) == "/" and cm.group(2) == tag:
                    i += 1
                    break
                body_lines.append(lines[i])
                i += 1
            inline_blocks[tag] = "\n".join(body_lines)
            continue

        try:
            tokens = shlex.split(line, comments=False)
        except ValueError:
            tokens = line.split()
        if not tokens:
            continue
        key, args = tokens[0], tokens[1:]
        directives.setdefault(key, []).append(args)

    return OvpnProfile(path=path, raw_lines=lines, directives=directives, inline_blocks=inline_blocks)


def build_launch_config(
    profile: OvpnProfile,
    *,
    up_script: str,
    down_script: str,
    auth_file: Path | None,
    route_noexec: bool,
) -> str:
    """Return the full text of an augmented .ovpn config ready to hand to openvpn.

    ``up_script``/``down_script`` are full command lines (already the final
    argv string splitvpn wants openvpn to invoke), not paths to write.
    """
    filtered: list[str] = []
    for raw in profile.raw_lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith(("#", ";")):
            try:
                tok = shlex.split(stripped)[0]
            except (ValueError, IndexError):
                tok = ""
            if tok in _OVERRIDE_KEYS:
                continue
        filtered.append(raw)

    extra = [
        "",
        "# --- injected by splitvpn ---",
        "script-security 2",
        f'up "{up_script}"',
        f'down "{down_script}"',
        "up-restart",
    ]
    if route_noexec:
        extra.append("route-noexec")
        extra.append('pull-filter ignore "redirect-gateway"')
        extra.append('pull-filter ignore "route "')
    if auth_file is not None:
        extra.append(f"auth-user-pass {shlex.quote(str(auth_file))}")

    return "\n".join(filtered) + "\n" + "\n".join(extra) + "\n"
