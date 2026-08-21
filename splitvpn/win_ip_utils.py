"""Windows networking command wrappers (PowerShell NetTCPIP cmdlets), used by
the privileged helper on Windows. Mirrors ip_utils.py's role so
win_route_split.py has the same shape as the Linux route_split.py it's
modeled on -- see platform_backend.py for how the two get selected.

All state-changing calls go through PowerShell's NetTCPIP module
(Get/New/Remove-NetRoute, Get/Set-NetAdapterBinding) rather than the
legacy `route.exe`/`netsh` text-based tools, since they return structured
data and avoid locale-dependent text parsing.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("splitvpn.win_ip_utils")


class CommandError(RuntimeError):
    def __init__(self, script: str, returncode: int, stderr: str):
        self.script = script
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"powershell failed ({returncode}): {stderr.strip()}")


def _find_powershell() -> str:
    """openvpn's --up/--down scripts run with a stripped-down PATH
    (just System32/WINDOWS/WINDOWS/System32/Wbem -- confirmed empirically,
    it does not include WindowsPowerShell), so `shutil.which` can fail
    even though PowerShell is obviously installed. Fall back to its
    well-known location under %SystemRoot%.
    """
    found = shutil.which("powershell")
    if found:
        return found
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    default = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(default)


POWERSHELL = _find_powershell()


def run_ps(script: str, *, check: bool = True) -> subprocess.CompletedProcess:
    cmd = [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script]
    log.debug("+ powershell (%d char script)", len(script))
    # errors="replace": stdout encoding for text captured this way is not
    # reliably UTF-8 on Windows PowerShell 5.1 (see run_ps_json for the
    # cases where that actually matters); this path is only used for
    # ASCII-safe output (counts, exit codes), so replacement never bites.
    proc = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=False)
    if check and proc.returncode != 0:
        raise CommandError(script, proc.returncode, proc.stderr)
    return proc


def run_ps_json(script: str):
    """Run a PowerShell pipeline ending in `ConvertTo-Json` and return the
    parsed result (None if empty/on error).

    Windows PowerShell 5.1 does not reliably emit UTF-8 on stdout when it's
    redirected to a pipe (as it is here) rather than a real console, which
    silently mangles any non-ASCII text (adapter names, etc.) if read back
    naively. Routing the JSON through a temp file written with an explicit
    UTF-8 `Out-File` sidesteps that entirely.
    """
    fd, path = tempfile.mkstemp(prefix="splitvpn-ps-", suffix=".json")
    import os
    os.close(fd)
    out_path = Path(path)
    try:
        full_script = f"{script} | Out-File -LiteralPath '{path}' -Encoding utf8"
        run_ps(full_script, check=False)
        text = out_path.read_text(encoding="utf-8-sig", errors="replace").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    finally:
        out_path.unlink(missing_ok=True)


def validate_cidr(value: str) -> str:
    """Raise ValueError if not a valid IPv4/IPv6 network; return its normalized form."""
    net = ipaddress.ip_network(value, strict=False)
    return str(net)


def get_default_route() -> dict | None:
    """Return the current lowest-metric IPv4 default route, or None."""
    data = run_ps_json(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue "
        "| Sort-Object -Property RouteMetric "
        "| Select-Object -First 1 -Property NextHop,InterfaceIndex,InterfaceAlias "
        "| ConvertTo-Json -Compress"
    )
    if not data:
        return None
    return {
        "gateway": data.get("NextHop"),
        "if_index": data.get("InterfaceIndex"),
        "if_alias": data.get("InterfaceAlias"),
    }


def route_replace(cidr: str, *, via: str | None = None, if_index: int | None = None,
                   metric: int | None = None) -> None:
    """Add cidr as a route, replacing any existing route to the same prefix
    on the same interface (New-NetRoute errors on an exact duplicate, so we
    remove first and ignore errors from that removal).
    """
    sel = [f"-DestinationPrefix '{cidr}'"]
    if if_index is not None:
        sel.append(f"-InterfaceIndex {int(if_index)}")

    new_args = list(sel)
    if via:
        new_args.append(f"-NextHop '{via}'")
    if metric is not None:
        new_args.append(f"-RouteMetric {int(metric)}")

    script = (
        f"Remove-NetRoute {' '.join(sel)} -Confirm:$false -ErrorAction SilentlyContinue; "
        f"New-NetRoute {' '.join(new_args)} -Confirm:$false -ErrorAction Stop | Out-Null"
    )
    run_ps(script)


def route_del(cidr: str, *, if_index: int | None = None) -> None:
    sel = [f"-DestinationPrefix '{cidr}'"]
    if if_index is not None:
        sel.append(f"-InterfaceIndex {int(if_index)}")
    run_ps(f"Remove-NetRoute {' '.join(sel)} -Confirm:$false -ErrorAction SilentlyContinue", check=False)


def route_exists(cidr: str, *, if_index: int | None = None) -> bool:
    sel = [f"-DestinationPrefix '{cidr}'"]
    if if_index is not None:
        sel.append(f"-InterfaceIndex {int(if_index)}")
    proc = run_ps(
        f"(Get-NetRoute {' '.join(sel)} -ErrorAction SilentlyContinue | Measure-Object).Count",
        check=False,
    )
    return proc.stdout.strip() not in ("", "0")


def ipv6_get_disabled() -> bool:
    """True if IPv6 is currently disabled (unbound) on every adapter."""
    proc = run_ps(
        "@(Get-NetAdapterBinding -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue "
        "| Where-Object Enabled -eq $true).Count",
        check=False,
    )
    try:
        return int(proc.stdout.strip() or "1") == 0
    except ValueError:
        return False


def ipv6_set_disabled(disabled: bool) -> None:
    """Unbind/rebind the IPv6 protocol on every adapter. Takes effect
    immediately (unlike the DisabledComponents registry value, which needs
    a reboot), and is the Windows analogue of the Linux
    net.ipv6.conf.all.disable_ipv6 leak guard used in "exclude listed
    subnets" mode.
    """
    value = "$false" if disabled else "$true"
    run_ps(
        f"Get-NetAdapterBinding -ComponentID ms_tcpip6 -ErrorAction SilentlyContinue "
        f"| Set-NetAdapterBinding -ComponentID ms_tcpip6 -Enabled {value} -ErrorAction SilentlyContinue",
        check=False,
    )
