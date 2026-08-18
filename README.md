# Split VPN

[Русская версия](README.ru.md)

A GTK3 OpenVPN client for Linux (built and packaged for Arch) that imports
`.ovpn` profiles and supports two kinds of split tunneling:

- **By IP/subnet** — only selected CIDRs go through the VPN (or, inverted,
  everything goes through the VPN except selected CIDRs), while the rest of
  the system keeps using the normal default route.
- **By application** — the VPN runs entirely inside an isolated network
  namespace; only applications you explicitly launch from Split VPN use the
  tunnel, everything else on the system is completely unaffected.

Each imported profile picks one mode. OpenVPN itself (the real `openvpn`
binary) does the actual protocol/crypto work; Split VPN parses the `.ovpn`
file, drives `openvpn` as a subprocess, and manages routing/namespaces
around it.

> **Testing note:** this has been built and exercised end to end on a real
> Arch Linux system (WSL2) — package built with `makepkg`, installed, and
> run against a local OpenVPN test server for both split-by-subnet modes
> and split-by-application (netns) mode, including real traffic crossing
> the tunnel, disconnect/route-restore, and recovery after a hard `kill -9`
> of the openvpn process. Three real bugs turned up during that testing and
> were fixed (see the git log). What's **not** yet been exercised: the GTK
> GUI itself (WSL2 here had no display server, so only the backend/CLI path
> was driven directly), a real TLS/certificate-based provider `.ovpn` (only
> a static-key config was used as the test target), and bare-metal Arch
> (WSL2's kernel/networking is close to but not identical to bare metal).
> The checklist near the bottom of this file still covers what to double
> check on your own machine.

## How it works

```
GUI (unprivileged, GTK3)  ──pkexec──▶  splitvpn-helper (root)
     reads /run/splitvpn/*/state.json     │
     directly for status, no pkexec       ├─ writes an augmented .ovpn
     needed for polling                   ├─ launches `openvpn --daemon`
                                           ├─ (per-app mode) sets up a
                                           │   netns + veth + NAT first
                                           └─ openvpn's --up/--down hooks
                                              call back into the helper
                                              to apply/tear down routes
```

- `splitvpn` — the GTK GUI. Runs as your normal user. Never touches the
  network directly.
- `splitvpn-helper` — a root-only CLI, invoked via `pkexec`. This is the
  only part of the program that runs privileged operations (`ip`,
  `iptables`, `ip netns`, launching `openvpn`).
- State for active sessions lives under `/run/splitvpn/<session>/`
  (tmpfs, cleared on reboot) and is written world-readable so the GUI can
  poll status/log without repeated authentication prompts. Only mutating
  actions (connect/disconnect/launch-app) go through `pkexec`.
- Imported profiles (plus any cert/key files they reference by relative
  path) are copied into `~/.config/splitvpn/profiles/<id>/`.

### Split by IP/subnet

OpenVPN is launched with `route-noexec` and `pull-filter ignore` on
`redirect-gateway`/`route`, so it never touches the routing table itself.
Once the tunnel is up, the `--up` hook adds exactly the routes you asked
for (via `ip route replace`), and the `--down` hook removes them. In
"everything except listed subnets" mode, IPv6 is temporarily disabled
system-wide for the session so it can't bypass the tunnel — this version
only manages IPv4 routes, so leaving IPv6 enabled would otherwise leak.

### Split by application

A dedicated network namespace + veth pair is created, NATed through your
normal default interface. OpenVPN itself runs *inside* that namespace with
completely normal (non-split) behavior — its full-tunnel default route only
exists inside the namespace, so it never affects the rest of the system.
Clicking "Launch" next to an app runs it inside the namespace as your own
user (via `setpriv`, not root), inheriting `DISPLAY`/`WAYLAND_DISPLAY`/
`XAUTHORITY`/`DBUS_SESSION_BUS_ADDRESS` so GUI apps keep working normally.

## Installing on Arch

```bash
pacman -S --needed python python-gobject gtk3 openvpn iproute2 iptables util-linux polkit python-build python-installer python-wheel python-setuptools
makepkg -si
```

Run from inside this repository — the `PKGBUILD` builds directly from the
checkout rather than downloading a tarball. This installs `splitvpn` and
`splitvpn-helper` on your `PATH`, the `.desktop` launcher, and a polkit
policy that gives the `pkexec` prompt a proper description.

### Manual / dev install (without packaging)

```bash
pip install --user .
```

This creates the same `splitvpn` / `splitvpn-helper` console scripts via
the `[project.scripts]` entry points in `pyproject.toml`. You'll still need
`python-gobject`, `gtk3`, `openvpn`, `iproute2`, `iptables`, `util-linux`
and `polkit` installed system-wide (PyGObject binds to the system GTK).

## Using it

1. Launch **Split VPN** from your application menu (or run `splitvpn`).
2. Click the **+** button and pick a `.ovpn` file. If it references
   external `ca`/`cert`/`key` files by relative path (common with
   multi-file provider exports), those are copied in alongside it
   automatically.
3. Pick a split-tunnel mode for that profile:
   - **Full tunnel** — normal VPN behavior, no split.
   - **Split by IP/subnet** — add CIDRs on the *IP / Subnet rules* tab and
     choose whether they're the only thing tunneled, or the only thing
     excluded.
   - **Split by application** — add applications on the *Applications*
     tab (pick an installed `.desktop` entry or type a raw command).
4. Click **Connect**. If the profile needs a username/password
   (`auth-user-pass` with no inline file), you'll be prompted first.
   `pkexec` will ask for your password once — this is expected, it's how
   the GUI hands off to the privileged helper.
5. For per-application mode, once status shows "connected", use the
   **Launch** button next to each app on the *Applications* tab.
6. Watch the **Log** tab for openvpn's own output if something looks
   stuck.

## Known limitations (v1)

- IPv4 only for split-by-IP routing; IPv6 is force-disabled for the
  duration of an "everything except listed subnets" session as a leak
  guard (see above), rather than being split itself.
- One active split-by-application session uses one dedicated `/30` out of
  `10.200.0.0/16` per session; that's up to ~248 concurrent netns
  sessions, which is far more than anyone needs in practice.
- No IPv6 support inside the per-application namespace either — apps
  there simply won't have an IPv6 route (fails closed, not a leak).
- `/run/splitvpn` isn't proactively cleaned up on disconnect (logs are
  kept for troubleshooting); it's tmpfs, so it clears on reboot. Remove a
  stale `/run/splitvpn/<session>/` by hand if needed.
- Assumes a routed (`dev tun`, not `dev tap`) OpenVPN client config, which
  covers the overwhelming majority of provider-supplied `.ovpn` files.

## Before you trust it with real traffic — a checklist

The backend has been verified against a local test server (see the testing
note above), but not against your actual VPN provider or the GUI itself.
Work through this once after installing:

- [ ] Import a real `.ovpn` and confirm **Full tunnel** mode connects and
      `curl ifconfig.me` (or similar) shows the VPN's exit IP.
- [ ] Switch to **Split by IP/subnet → include only**, add a single test
      CIDR (e.g. a server you control), connect, and confirm `ip route`
      shows a route to it via `tunX` while your normal default route is
      untouched, and unrelated traffic still exits normally.
- [ ] Switch to **exclude listed subnets**, add your LAN CIDR, connect,
      and confirm you can still reach LAN devices while general traffic
      goes through the VPN. Confirm `sysctl net.ipv6.conf.all.disable_ipv6`
      reads `1` while connected and `0` again after disconnecting.
- [ ] Switch to **Split by application**, add e.g. `xterm` (or a browser),
      connect, click Launch, and confirm (inside that app) traffic exits
      via the VPN while a normal terminal on the host still exits
      normally.
- [ ] Disconnect from each mode and confirm `ip route`, `ip netns list`,
      and `iptables -L -t nat` are back to how they were before connecting.
- [ ] Kill `openvpn` externally (`kill -9`) while connected and confirm
      the GUI notices ("error (process exited)") instead of hanging on
      "connecting" forever, and that `splitvpn-helper disconnect` still
      cleans up leftover routes/namespaces.

## Security notes

- `splitvpn-helper` is the only component that runs as root, and only for
  the duration of each `connect`/`disconnect`/`run-app` call — it's
  invoked fresh via `pkexec` each time, never left running as a
  standing privileged daemon.
- Credentials typed into the "VPN credentials" dialog are written to a
  `0600` temp file, copied by the root helper into the session directory,
  and never touch the GUI process's own long-lived storage.
- All subprocess calls use argument lists (never `shell=True`), and
  CIDRs are validated with Python's `ipaddress` module before being
  handed to `ip route`.
