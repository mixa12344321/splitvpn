# Contributing to Split VPN

Thanks for considering a contribution. This project is Linux-only (Arch
specifically, though most of it is distro-agnostic) — you'll need a real
Linux environment to test anything beyond pure syntax, since it drives
`openvpn`, `ip`, `iptables`, `ip netns`, and GTK directly. A VM or WSL2
with an Arch (or other) distro works fine.

## Ways to help

- **Bug reports.** Open an issue with your OpenVPN provider's config shape
  (redact secrets), what you expected, and what actually happened. The
  `Log` tab's openvpn output and `/run/splitvpn/<session>/state.json` are
  usually the first things worth including.
- **Translations.** The GUI is localized via gettext
  ([`splitvpn/locale/`](splitvpn/locale/)). Adding a language means
  translating [`splitvpn/locale/splitvpn.pot`](splitvpn/locale/splitvpn.pot)
  into `splitvpn/locale/<lang>/LC_MESSAGES/splitvpn.po` and compiling it
  (see below) — no code changes required.
- **Code.** Bug fixes, small features, packaging improvements. For
  anything larger than a bug fix, open an issue first to agree on the
  approach before investing time in a PR — this project has real
  root-privileged networking code in it, and design mistakes there are
  expensive to unwind.

## Setting up a dev environment

```bash
pacman -S --needed python python-gobject gtk3 openvpn iproute2 iptables util-linux polkit python-build python-installer python-wheel python-setuptools ruff
git clone https://github.com/mixa12344321/splitvpn.git
cd splitvpn
pip install --user -e .
```

## Before opening a PR

```bash
ruff check splitvpn
python -m py_compile $(find splitvpn -name '*.py')
```

If your change touches `splitvpn/helper.py`, `ip_utils.py`, `route_split.py`,
or `netns_split.py` (anything the root-privileged helper does), please
actually exercise it end to end rather than relying on review alone —
build the package with `makepkg -si` (see [`packaging/PKGBUILD`](packaging/PKGBUILD))
and connect/disconnect against a real or test OpenVPN server, checking
`ip route` / `ip netns list` / `iptables -L -t nat` before and after. This
codebase has already had real bugs (path resolution under
`script-security 2`, a route-table corruption case) that only showed up
under an actual connection, not from reading the diff.

## Adding or updating a translation

```bash
# extract/refresh the template after changing translatable strings
xgettext --language=Python --keyword=_ --from-code=UTF-8 \
  --package-name=splitvpn --output=splitvpn/locale/splitvpn.pot \
  splitvpn/gui/main_window.py splitvpn/gui/dialogs.py

# compile a .po you've translated
msgfmt --check -o splitvpn/locale/<lang>/LC_MESSAGES/splitvpn.mo \
  splitvpn/locale/<lang>/LC_MESSAGES/splitvpn.po
```

Commit both the `.po` (source) and the compiled `.mo`. Test it by running
the app with `LANGUAGE=<lang>` set.

## Code style

- No comments explaining *what* code does — only *why*, when it's
  genuinely non-obvious (a workaround, a hidden constraint). The existing
  code is the reference for tone.
- Keep the privilege boundary intact: only `splitvpn/helper.py` (and the
  modules it calls: `ip_utils.py`, `route_split.py`, `netns_split.py`)
  should ever run as root or shell out to `ip`/`iptables`/`openvpn`. The
  GUI and `client.py` stay unprivileged and talk to the helper only via
  `pkexec`.
- `subprocess` calls take argument lists, never `shell=True`. User input
  that becomes a network parameter (CIDRs, interface names) gets
  validated before it reaches a command.

## Reporting a security issue

Please don't open a public issue for anything that looks exploitable —
see [SECURITY.md](SECURITY.md).
