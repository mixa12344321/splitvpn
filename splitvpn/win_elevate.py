"""Windows UAC elevation (and de-elevation) for splitvpn-helper.

Two related but opposite problems, both replacing something pkexec/setpriv
give for free on Linux:

  - run_elevated(): splitvpn-helper itself needs to run as Administrator.
    ShellExecuteExW with the "runas" verb triggers the native UAC consent
    prompt, but -- unlike pkexec -- gives no stdout pipe back to the
    caller. splitvpn-helper is told (via --output-file) to write its
    final JSON result to a temp file instead, which this function waits
    for and reads back once the elevated process exits.

  - launch_deelevated(): the opposite problem. Once splitvpn-helper *is*
    elevated, launching a user's app "normally" would hand it an
    Administrator token too -- undesirable, and not what "Launch" should
    do. Windows has no direct "run this unelevated" API, so this
    duplicates explorer.exe's own (always medium-integrity, i.e. normal
    user) token via OpenProcessToken/DuplicateTokenEx and starts the
    target process with that token via CreateProcessWithTokenW. Verified
    on a real machine: the resulting process's group SID is
    S-1-16-8192 (Medium Mandatory Level), not S-1-16-12288 (High) --
    i.e. it is genuinely not elevated, matching what setpriv achieves on
    Linux by dropping to the invoking user.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
from ctypes import wintypes
from pathlib import Path

SW_HIDE = 0
SEE_MASK_NOCLOSEPROCESS = 0x00000040
INFINITE = 0xFFFFFFFF
ERROR_CANCELLED = 1223

TOKEN_DUPLICATE = 0x0002
TOKEN_ALL_ACCESS = 0xF01FF
PROCESS_QUERY_INFORMATION = 0x0400
SECURITY_IMPERSONATION = 2
TOKEN_PRIMARY = 1
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HANDLE),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HANDLE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hKeyClass", wintypes.HANDLE),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]


class ElevationError(RuntimeError):
    pass


def _quote(arg: str) -> str:
    return f'"{arg}"' if (" " in arg or not arg) else arg


def _quote_cmdline(args: list[str]) -> str:
    return " ".join(_quote(a) for a in args)


def _find_explorer_pid() -> int:
    proc = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq explorer.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, check=False,
    )
    line = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    if not line:
        raise ElevationError("explorer.exe not found -- no interactive user session to de-elevate into.")
    # CSV row: "explorer.exe","<pid>","Console","<session>","<mem>"
    return int(line.split(",")[1].strip('"'))


def run_elevated(exe_path: str, args: list[str]) -> dict:
    """Run exe_path elevated with args, returning its parsed JSON result.

    Appends "--output-file <tmp>" to args; the child is responsible for
    writing its JSON result there (see helper.py's --output-file handling).
    """
    out_fd, out_path = tempfile.mkstemp(prefix="splitvpn-out-", suffix=".json")
    os.close(out_fd)
    Path(out_path).unlink()  # the child creates it fresh; we just needed a unique name

    full_args = [*args, "--output-file", out_path]
    param_str = " ".join(_quote(a) for a in full_args)

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(_SHELLEXECUTEINFOW)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.hwnd = None
    info.lpVerb = "runas"
    info.lpFile = exe_path
    info.lpParameters = param_str
    info.lpDirectory = None
    info.nShow = SW_HIDE
    info.hInstApp = None

    ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info))
    if not ok:
        code = ctypes.GetLastError()
        Path(out_path).unlink(missing_ok=True)
        if code == ERROR_CANCELLED:
            raise ElevationError("Authentication was cancelled.")
        raise ElevationError(f"Failed to elevate (Win32 error {code}).")

    if info.hProcess:
        ctypes.windll.kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        ctypes.windll.kernel32.CloseHandle(info.hProcess)

    out_file = Path(out_path)
    if not out_file.exists():
        raise ElevationError(
            "splitvpn-helper produced no output (it may have crashed before writing a result)."
        )
    try:
        return json.loads(out_file.read_text(encoding="utf-8"))
    finally:
        out_file.unlink(missing_ok=True)


def launch_deelevated(command: list[str], log_path: Path) -> int:
    """Launch command as the normal (medium-integrity) interactive user
    rather than as Administrator, redirecting its stdout/stderr to
    log_path. Returns the new process's PID.
    """
    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    explorer_pid = _find_explorer_pid()
    hproc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, explorer_pid)
    if not hproc:
        raise ElevationError(f"OpenProcess(explorer.exe) failed (Win32 error {ctypes.GetLastError()}).")

    try:
        htok = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(hproc, TOKEN_DUPLICATE, ctypes.byref(htok)):
            raise ElevationError(f"OpenProcessToken failed (Win32 error {ctypes.GetLastError()}).")

        try:
            hdup = wintypes.HANDLE()
            if not advapi32.DuplicateTokenEx(
                htok, TOKEN_ALL_ACCESS, None, SECURITY_IMPERSONATION, TOKEN_PRIMARY, ctypes.byref(hdup)
            ):
                raise ElevationError(f"DuplicateTokenEx failed (Win32 error {ctypes.GetLastError()}).")
        finally:
            kernel32.CloseHandle(htok)
    finally:
        kernel32.CloseHandle(hproc)

    try:
        sec_attrs_null = None  # inheritable-by-default handles from CreateFile below
        h_log = kernel32.CreateFileW(
            str(log_path), 0x40000000, 0x00000001, sec_attrs_null, 4,  # GENERIC_WRITE, FILE_SHARE_READ, CREATE_ALWAYS
            0x80, None,  # FILE_ATTRIBUTE_NORMAL
        )
        if h_log == wintypes.HANDLE(-1).value:
            raise ElevationError(f"Could not open log file (Win32 error {ctypes.GetLastError()}).")

        # Handles passed via STARTUPINFO must be inheritable.
        kernel32.SetHandleInformation(h_log, 0x00000001, 0x00000001)  # HANDLE_FLAG_INHERIT

        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdOutput = h_log
        si.hStdError = h_log
        si.hStdInput = None
        pi = _PROCESS_INFORMATION()

        cmdline = _quote_cmdline(command)
        ok = advapi32.CreateProcessWithTokenW(
            hdup, 0, None, cmdline, CREATE_UNICODE_ENVIRONMENT, None, None,
            ctypes.byref(si), ctypes.byref(pi),
        )
        kernel32.CloseHandle(h_log)
        if not ok:
            raise ElevationError(f"CreateProcessWithTokenW failed (Win32 error {ctypes.GetLastError()}).")

        kernel32.CloseHandle(pi.hThread)
        kernel32.CloseHandle(pi.hProcess)
        return pi.dwProcessId
    finally:
        kernel32.CloseHandle(hdup)
