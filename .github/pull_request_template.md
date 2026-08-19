## What this changes and why

## Checklist

- [ ] `ruff check splitvpn` passes
- [ ] `python -m py_compile` passes on every changed file
- [ ] If this touches `helper.py`, `ip_utils.py`, `route_split.py`, or
      `netns_split.py`: I actually ran a real connect/disconnect cycle
      (not just read the diff) and checked `ip route` / `ip netns list` /
      `iptables -L -t nat` before and after — see
      [CONTRIBUTING.md](../CONTRIBUTING.md).
- [ ] If this adds/changes user-facing GUI strings: they're wrapped in
      `_()` and the `.pot`/`ru.po`/`.mo` are refreshed (or I've noted that
      a translator needs to catch up).
- [ ] If this changes packaging (`pyproject.toml`, `PKGBUILD`): I rebuilt
      with `makepkg -si` and it still installs cleanly.

## How you tested it

## Anything reviewers should pay extra attention to
