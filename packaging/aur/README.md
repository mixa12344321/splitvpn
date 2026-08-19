# AUR packaging

This `PKGBUILD` (and its generated `.SRCINFO`) is the AUR-ready variant:
it builds from the pinned `v0.2.0` GitHub release tarball with a checked
sha256sum, not from a local checkout — that's what AUR requires, and
it's different from [`packaging/PKGBUILD`](../PKGBUILD) at the repo root,
which is a dev-convenience build that always builds from whatever's in
your working tree (used by the README's `makepkg -si` instructions).

Both have been build-tested with `makepkg -s` in a clean environment.

## Publishing (needs your own AUR account)

This part can't be automated by an assistant — it needs your own AUR
account and SSH key, which are credentials only you should hold.

1. Create an account at [aur.archlinux.org](https://aur.archlinux.org) if
   you don't have one, and add an SSH public key under
   *My Account → My SSH Public Keys*.
2. Clone the (currently empty) AUR repo for this package name:
   ```bash
   git clone ssh://aur@aur.archlinux.org/splitvpn.git aur-splitvpn
   ```
   (If `splitvpn` is already taken on AUR, you'll need to pick a
   different `pkgname`, e.g. `splitvpn-git` or `splitvpn-gtk`, and update
   it in `PKGBUILD` and `.SRCINFO` first.)
3. Copy this directory's `PKGBUILD` and `.SRCINFO` into that clone:
   ```bash
   cp PKGBUILD .SRCINFO aur-splitvpn/
   cd aur-splitvpn
   ```
4. Commit and push:
   ```bash
   git add PKGBUILD .SRCINFO
   git commit -m "splitvpn 0.2.0"
   git push
   ```

That's it — the package appears on AUR at
`https://aur.archlinux.org/packages/splitvpn` shortly after the push.

## Keeping it updated for future releases

After tagging a new GitHub release (`vX.Y.Z`):

```bash
cd packaging/aur
# update pkgver in PKGBUILD, then:
curl -sL -o /tmp/splitvpn.tar.gz \
  "https://github.com/mixa12344321/splitvpn/archive/refs/tags/vX.Y.Z.tar.gz"
sha256sum /tmp/splitvpn.tar.gz   # paste into sha256sums=() in PKGBUILD
makepkg --printsrcinfo > .SRCINFO
# then copy both files into your aur-splitvpn clone and push, as above
```
