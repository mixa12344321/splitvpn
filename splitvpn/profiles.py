"""Local storage of imported OpenVPN profiles and their split-tunnel rules.

Everything here runs unprivileged, under ~/.config/splitvpn/ on Linux or
%APPDATA%\\splitvpn\\ on Windows.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ovpn_parser import parse_ovpn

if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "splitvpn"
else:
    CONFIG_DIR = Path.home() / ".config" / "splitvpn"
PROFILES_DIR = CONFIG_DIR / "profiles"

# Directives that reference a separate file on disk (as opposed to an
# inline <tag>...</tag> block); these need to travel with the profile.
_EXTERNAL_FILE_KEYS = ("ca", "cert", "key", "tls-auth", "tls-crypt", "pkcs12", "dh", "crl-verify")


@dataclass
class Profile:
    id: str
    name: str
    ovpn_file: str                           # path to our stored copy of the .ovpn
    split_type: str = "none"                 # none|routes|netns
    route_split_mode: str = "include_only"   # include_only|exclude_listed
    cidrs: list[str] = field(default_factory=list)
    apps: list[dict] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return PROFILES_DIR / self.id

    @property
    def meta_file(self) -> Path:
        return self.dir / "profile.json"

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta_file.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False))

    @classmethod
    def load(cls, profile_id: str) -> Profile:
        d = PROFILES_DIR / profile_id
        data = json.loads((d / "profile.json").read_text())
        return cls(**data)


def list_profiles() -> list[Profile]:
    if not PROFILES_DIR.exists():
        return []
    out = []
    for entry in sorted(PROFILES_DIR.iterdir()):
        if (entry / "profile.json").exists():
            try:
                out.append(Profile.load(entry.name))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
    return out


def import_ovpn(source_path: Path, display_name: str | None = None) -> Profile:
    """Copy a .ovpn file, plus any sibling cert/key files it references by
    relative path, into our own config directory so the profile keeps
    working even if the original file/USB stick/download goes away.
    """
    profile_id = uuid.uuid4().hex[:12]
    dest_dir = PROFILES_DIR / profile_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    parsed = parse_ovpn(source_path)
    lines = list(parsed.raw_lines)

    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";", "<")):
            continue
        try:
            tokens = shlex.split(stripped)
        except ValueError:
            continue
        if not tokens or tokens[0] not in _EXTERNAL_FILE_KEYS or len(tokens) < 2:
            continue
        ref_path = source_path.parent / tokens[1]
        if ref_path.is_file():
            shutil.copy2(ref_path, dest_dir / ref_path.name)
            tokens[1] = ref_path.name
            lines[i] = " ".join(shlex.quote(t) for t in tokens)

    dest_ovpn = dest_dir / "config.ovpn"
    dest_ovpn.write_text("\n".join(lines) + "\n")

    profile = Profile(id=profile_id, name=display_name or source_path.stem, ovpn_file=str(dest_ovpn))
    profile.save()
    return profile


def delete_profile(profile: Profile) -> None:
    shutil.rmtree(profile.dir, ignore_errors=True)
