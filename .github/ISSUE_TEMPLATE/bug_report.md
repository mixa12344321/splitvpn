---
name: Bug report
about: Something doesn't work the way it should
title: ""
labels: bug
assignees: ""
---

**What happened**
A clear description of the bug.

**What you expected instead**

**Steps to reproduce**
1.
2.
3.

**Split-tunnel mode**
- [ ] Full tunnel (no split)
- [ ] Split by IP / subnet — include only / exclude listed (delete the one that doesn't apply)
- [ ] Split by application

**Environment**
- Split VPN version (or commit): 
- Install method: `makepkg -si` / `pip install .` / other
- Arch Linux (or other distro + version):
- OpenVPN version: `openvpn --version`

**Relevant log output**
Paste from the **Log** tab, or `/run/splitvpn/<session>/state.json` and
`openvpn.log`. **Redact your VPN server hostname/IP, username, and any
certificate/key content before pasting** — they aren't needed to debug
most issues, and if they are, say so and we'll ask privately.

```
paste here
```

**Anything else**
