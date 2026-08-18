# Security Policy

## Supported versions

Split VPN is pre-1.0 software. Only the latest release on the `main`
branch is supported — please update before reporting an issue.

| Version | Supported |
| ------- | --------- |
| 0.2.x   | ✅        |
| < 0.2   | ❌        |

## Reporting a vulnerability

`splitvpn-helper` runs as root and drives `ip`, `iptables`, `ip netns`,
and `openvpn` directly, so security issues here can have real impact —
please report them responsibly rather than opening a public issue.

Open a [GitHub Security Advisory](https://github.com/mixa12344321/splitvpn/security/advisories/new)
for this repository, or open a regular issue if you'd prefer public
disclosure and the issue isn't sensitive (e.g. a hardening suggestion
rather than an exploitable bug).

Please include:

- The affected version/commit.
- Steps to reproduce, or a description of the flaw if reproduction steps
  aren't practical to share.
- The potential impact as you see it (e.g. privilege escalation via the
  helper, a routing/namespace bug that leaks traffic outside the VPN).

There's no fixed SLA (this is a small side project), but reports will be
acknowledged and addressed as a priority over regular feature work.
