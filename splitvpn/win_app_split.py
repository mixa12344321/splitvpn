"""Per-application split tunneling on Windows via WinDivert.

Windows has no equivalent of Linux network namespaces, so instead of
isolating an app's traffic into a separate network stack (as
netns_split.py does on Linux), this module transparently NATs it:

  - A background thread listens on WinDivert's SOCKET layer for
    connect/bind events and checks each event's process ID against the
    currently tracked set (the PIDs the user launched via "Launch", plus
    -- refreshed periodically through psutil -- all of their descendant
    processes, so e.g. a browser's renderer subprocesses are covered
    too). Matches are recorded as (protocol, local port) -> tracked.

  - A second thread opens a NETWORK-layer handle covering both
    directions and, for each packet:
      * Outbound, from a tracked (protocol, port): rewrite the source
        address to the VPN tunnel's own local address, remember the
        original address keyed by (protocol, port) so the reply can be
        un-translated, force it out the tunnel adapter, recompute
        checksums, reinject.
      * Inbound, addressed to the tunnel's local address on a
        previously-NATed (protocol, port): rewrite the destination back
        to the app's real local address, force it back out the app's
        real adapter (Windows' strong host model expects the receiving
        interface to match the destination address), recompute
        checksums, reinject.
      * Everything else: reinject completely unchanged.

This has been verified mechanically on real Windows (WinDivert driver
loads, SOCKET-layer events correlate to real PIDs, checksum
recalculation produces valid packets) but not end-to-end against a live
remote OpenVPN server -- there was no second machine available to prove
round-trip correctness against. Treat this backend as unverified in that
sense until someone runs it against a real connection and reports back.
"""
from __future__ import annotations

import logging
import threading

import psutil
import pydivert

log = logging.getLogger("splitvpn.win_app_split")

_PID_REFRESH_INTERVAL = 2.0  # seconds

# pydivert doesn't expose named constants for WINDIVERT_EVENT_SOCKET_*, and
# WinDivert's own filter language does not accept symbolic event names at
# the SOCKET layer (confirmed empirically: a filter of "event == CONNECT"
# silently matched nothing, while "true" plus filtering on the numeric
# .event value in Python worked). These match WinDivert's own header
# (WINDIVERT_EVENT_SOCKET_BIND/CONNECT).
_EVENT_BIND = 3
_EVENT_CONNECT = 4


class AppSplitEngine:
    """One instance per active per-application split session.

    Usage: construct with the tunnel's own local address and interface
    index plus the real (pre-VPN) default interface index, call
    ``start()``, ``track(pid)`` for each launched app's root process,
    and ``stop()`` on disconnect.
    """

    def __init__(self, tunnel_ip: str, tun_if_index: int, real_if_index: int):
        self.tunnel_ip = tunnel_ip
        self.tun_if_index = int(tun_if_index)
        self.real_if_index = int(real_if_index)

        self._root_pids: set[int] = set()
        self._tracked_pids: set[int] = set()
        self._allowed_ports: set[tuple[int, int]] = set()  # (protocol, local_port)
        # (protocol, port) -> original address, for un-NATing replies
        self._nat_table: dict[tuple[int, int], str] = {}
        self._lock = threading.Lock()

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ public --

    def track(self, pid: int) -> None:
        with self._lock:
            self._root_pids.add(pid)
            # Also update _tracked_pids immediately rather than waiting for
            # the next periodic refresh: a launched app can open its first
            # connection within milliseconds, well before a multi-second
            # poll interval would otherwise notice it, and that first
            # connection is exactly the one most likely to matter.
            self._tracked_pids.add(pid)

    def untrack(self, pid: int) -> None:
        with self._lock:
            self._root_pids.discard(pid)
            self._tracked_pids.discard(pid)

    def start(self) -> None:
        self._threads = [
            threading.Thread(target=self._pid_refresh_loop, name="splitvpn-pidwatch", daemon=True),
            threading.Thread(target=self._socket_layer_loop, name="splitvpn-socketlayer", daemon=True),
            threading.Thread(target=self._network_layer_loop, name="splitvpn-netlayer", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._threads = []

    # --------------------------------------------------------- PID watch --

    def _pid_refresh_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                roots = set(self._root_pids)
            live: set[int] = set()
            for pid in roots:
                try:
                    proc = psutil.Process(pid)
                    live.add(pid)
                    for child in proc.children(recursive=True):
                        live.add(child.pid)
                except psutil.NoSuchProcess:
                    continue
            with self._lock:
                self._tracked_pids = live
            self._stop.wait(_PID_REFRESH_INTERVAL)

    # ------------------------------------------------------ socket layer --

    def _socket_layer_loop(self) -> None:
        try:
            w = pydivert.WinDivert(
                "true",
                layer=pydivert.Layer.SOCKET,
                flags=pydivert.Flag.RECV_ONLY | pydivert.Flag.SNIFF,
            )
            w.open()
        except OSError:
            log.exception("failed to open WinDivert SOCKET layer handle")
            return
        try:
            while not self._stop.is_set():
                try:
                    event = w.recv()
                except OSError:
                    break
                if event is None or event.socket is None:
                    continue
                if event.event not in (_EVENT_BIND, _EVENT_CONNECT):
                    continue
                pid = event.socket.ProcessId
                if pid in (0, 4):
                    # Idle (0) and System (4) are never a real tracked app,
                    # but WinDivert emits events attributed to PID 4 that
                    # duplicate a just-seen real event for the same
                    # (protocol, port) -- observed empirically, not
                    # documented -- which would otherwise immediately
                    # evict the correct entry this loop just added.
                    continue
                key = (event.socket.Protocol, event.socket.LocalPort)
                with self._lock:
                    tracked = pid in self._tracked_pids
                    if tracked:
                        self._allowed_ports.add(key)
                        log.debug("tracking %s (pid %d)", key, pid)
                    else:
                        self._allowed_ports.discard(key)
        finally:
            w.close()

    # ----------------------------------------------------- network layer --

    def _network_layer_loop(self) -> None:
        net_filter = (
            "(outbound and !loopback and (tcp or udp)) or "
            f"(inbound and ip.DstAddr == {self.tunnel_ip} and (tcp or udp))"
        )
        try:
            w = pydivert.WinDivert(net_filter, layer=pydivert.Layer.NETWORK)
            w.open()
        except OSError:
            log.exception("failed to open WinDivert NETWORK layer handle")
            return
        try:
            while not self._stop.is_set():
                try:
                    packet = w.recv()
                except OSError:
                    break
                if packet is None:
                    continue
                self._handle_packet(packet)
                w.send(packet)
        finally:
            w.close()

    def _handle_packet(self, packet) -> None:
        proto, _proto_start = packet.protocol  # (ipproto, header_offset), not the socket-layer field
        if proto is None:
            return
        if packet.is_outbound:
            port = packet.src_port
            with self._lock:
                tracked = (proto, port) in self._allowed_ports
                if tracked:
                    self._nat_table[(proto, port)] = packet.src_addr
            if not tracked:
                return
            packet.src_addr = self.tunnel_ip
            packet.interface = (self.tun_if_index, 0)
            packet.recalculate_checksums()
        else:
            port = packet.dst_port
            with self._lock:
                original_addr = self._nat_table.get((proto, port))
            if original_addr is None:
                return
            packet.dst_addr = original_addr
            packet.interface = (self.real_if_index, 0)
            packet.recalculate_checksums()
