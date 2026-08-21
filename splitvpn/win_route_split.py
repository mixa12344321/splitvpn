"""Route-table based split tunneling on Windows: the whole system shares
one tun/tap adapter, and only selected CIDRs (or everything except
selected CIDRs) are routed through it. Windows counterpart of
route_split.py -- same include_only/exclude_listed semantics, same
"don't clobber the connected route to the gateway's own subnet" defense,
just built on New-NetRoute/Remove-NetRoute instead of `ip route`.

Runs inside the openvpn --up/--down hooks, elevated (see win_elevate.py).
"""
from __future__ import annotations

import ipaddress

from . import win_ip_utils as ip_utils
from .state import SessionState


def apply_split_routes(state: SessionState, rules: dict) -> None:
    """Called once the tun/tap adapter is up and route_vpn_gateway is known."""
    gw = state.route_vpn_gateway
    if_index = state.tun_if_index
    if not gw or if_index is None:
        raise RuntimeError("tunnel gateway/interface unknown, cannot apply split routes")

    orig = state.orig_default
    if orig and state.trusted_ip and orig.get("if_index") is not None:
        ip_utils.route_replace(f"{state.trusted_ip}/32", via=orig["gateway"], if_index=orig["if_index"])
        state.added_routes.append(f"{state.trusted_ip}/32")

    split_mode = rules.get("split_mode", "include_only")
    cidrs = [ip_utils.validate_cidr(c) for c in rules.get("cidrs", [])]

    if split_mode == "include_only":
        for cidr in cidrs:
            ip_utils.route_replace(cidr, via=gw, if_index=if_index)
            state.added_routes.append(cidr)

    elif split_mode == "exclude_listed":
        # Full tunnel by default: everything through the VPN except the
        # listed CIDRs. Disable IPv6 for the duration of the session so it
        # can't silently bypass the tunnel -- this backend only manages
        # IPv4 routes, so leaving IPv6 enabled would otherwise leak.
        state.ipv6_prior_state = ip_utils.ipv6_get_disabled()
        if not state.ipv6_prior_state:
            ip_utils.ipv6_set_disabled(True)
            state.ipv6_disabled_by_us = True

        ip_utils.route_replace("0.0.0.0/0", via=gw, if_index=if_index)
        state.added_routes.append("default")

        if orig and orig.get("if_index") is not None:
            orig_gw_addr = ipaddress.ip_address(orig["gateway"]) if orig.get("gateway") else None
            for cidr in cidrs:
                if orig_gw_addr is not None and orig_gw_addr in ipaddress.ip_network(cidr):
                    # This CIDR contains the machine's own default gateway,
                    # i.e. it's the locally-connected subnet. Windows
                    # already has a connected route for it with no
                    # explicit next hop; replacing that with an explicit
                    # via-<gateway> route is both redundant and, on
                    # disconnect, leaves nothing on-link to resolve that
                    # gateway through when we try to restore the default
                    # route (the same bug this exact guard fixed on Linux
                    # -- see route_split.py). Leave it untouched.
                    continue
                ip_utils.route_replace(cidr, via=orig["gateway"], if_index=orig["if_index"])
                state.added_routes.append(cidr)
    else:
        raise ValueError(f"unknown split_mode {split_mode!r}")

    state.status = "connected"
    state.save()


def teardown_split_routes(state: SessionState) -> None:
    orig = state.orig_default
    remaining = list(state.added_routes)

    # Restore the default route first so the original gateway is reachable
    # again before we start deleting the more specific routes that were
    # keeping it reachable while the VPN default route was in place.
    if "default" in remaining and orig and orig.get("if_index") is not None:
        ip_utils.route_replace("0.0.0.0/0", via=orig["gateway"], if_index=orig["if_index"])
        remaining.remove("default")

    for cidr in reversed(remaining):
        ip_utils.route_del(cidr)
    state.added_routes = []

    if state.ipv6_disabled_by_us:
        ip_utils.ipv6_set_disabled(False)
        state.ipv6_disabled_by_us = False

    state.status = "disconnected"
    state.save()
