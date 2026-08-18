"""gettext setup for the GUI.

Translations are looked up first in the package's own bundled locale/
directory (works for `pip install .` and running straight from a
checkout), then in the system locale directory (used by the Arch
package). The active language is picked automatically from the usual
gettext environment variables (LANGUAGE, LC_ALL, LC_MESSAGES, LANG) --
there's no in-app language switcher.
"""
from __future__ import annotations

import gettext
from pathlib import Path

DOMAIN = "splitvpn"

_CANDIDATE_LOCALE_DIRS = [
    Path(__file__).resolve().parent / "locale",
    Path("/usr/share/locale"),
]


def _find_localedir() -> str | None:
    for candidate in _CANDIDATE_LOCALE_DIRS:
        if candidate.is_dir():
            return str(candidate)
    return None


_translation = gettext.translation(DOMAIN, localedir=_find_localedir(), fallback=True)
_ = _translation.gettext
