"""Route-table based split tunneling: the whole system shares one tun device,
and only selected CIDRs (or everything except selected CIDRs) are routed
through it. Runs inside the openvpn --up/--down hooks, as root.
"""
from __future__ import annotations

from . import ip_utils
from .state import SessionState

_IPV6_DISABLE_KEY = "net.ipv6.conf.all.disable_ipv6"


def apply_split_routes(state: SessionState, rules: dict) -> None:
    """Called once the tunnel device is up and route_vpn_gateway is known."""
    gw = state.route_vpn_gateway
    dev = state.tun_dev
    if not gw or not dev:
        raise RuntimeError("tunnel gateway/device unknown, cannot apply split routes")

    orig = state.orig_default
    if orig and state.trusted_ip:
        ip_utils.route_replace(f"{state.trusted_ip}/32", via=orig["gateway"], dev=orig["dev"])
        state.added_routes.append(f"{state.trusted_ip}/32")

    split_mode = rules.get("split_mode", "include_only")
    cidrs = [ip_utils.validate_cidr(c) for c in rules.get("cidrs", [])]

    if split_mode == "include_only":
        for cidr in cidrs:
            ip_utils.route_replace(cidr, via=gw, dev=dev)
            state.added_routes.append(cidr)

    elif split_mode == "exclude_listed":
        # Full tunnel by default: everything through the VPN except the
        # listed CIDRs. Disable IPv6 for the duration of the session so it
        # can't silently bypass the tunnel (we don't manage IPv6 routes).
        state.ipv6_prior_state = ip_utils.sysctl_get(_IPV6_DISABLE_KEY) == "1"
        if not state.ipv6_prior_state:
            ip_utils.sysctl_set(_IPV6_DISABLE_KEY, "1")
            state.ipv6_disabled_by_us = True

        ip_utils.route_replace("default", via=gw, dev=dev)
        state.added_routes.append("default")
        if orig:
            for cidr in cidrs:
                ip_utils.route_replace(cidr, via=orig["gateway"], dev=orig["dev"])
                state.added_routes.append(cidr)
    else:
        raise ValueError(f"unknown split_mode {split_mode!r}")

    state.status = "connected"
    state.save()


def teardown_split_routes(state: SessionState) -> None:
    orig = state.orig_default
    for cidr in reversed(state.added_routes):
        if cidr == "default" and orig:
            ip_utils.route_replace("default", via=orig["gateway"], dev=orig["dev"])
        else:
            ip_utils.route_del(cidr)
    state.added_routes = []

    if state.ipv6_disabled_by_us:
        ip_utils.sysctl_set(_IPV6_DISABLE_KEY, "0")
        state.ipv6_disabled_by_us = False

    state.status = "disconnected"
    state.save()
