"""
Thin ctypes wrappers around the handful of Win32 calls ERP Desk needs - stdlib
only, no pywin32:

  * JobObject      - every child process (Streamlit, digest run, Edge window) is
                     assigned to one job with KILL_ON_JOB_CLOSE, so nothing can
                     outlive the launcher, even if the launcher itself is killed.
  * SingleInstance - a named mutex so a second launch reuses the running app.
  * window helpers - find / focus / politely close the chrome-less app window.
  * message_box    - the only way to tell the user something when there is no
                     console (the app is normally started with pythonw).

Every function degrades to a harmless no-op off Windows so the modules import
(and the unit-ish checks run) anywhere.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

logger = logging.getLogger("erp_desk.win")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _u32 = ctypes.WinDLL("user32", use_last_error=True)

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CreateMutexW.restype = wintypes.HANDLE
    _k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]

    _u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.IsWindowVisible.argtypes = [wintypes.HWND]
    _u32.IsIconic.argtypes = [wintypes.HWND]
    _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _u32.BringWindowToTop.argtypes = [wintypes.HWND]
    _u32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _u32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _u32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_ERROR_ALREADY_EXISTS = 183
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SW_RESTORE = 9
_WM_CLOSE = 0x0010

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = 0x00000200 if IS_WINDOWS else 0


# ---------------------------------------------------------------------------
# Job object: kill everything we started when the launcher goes away
# ---------------------------------------------------------------------------
class JobObject:
    def __init__(self) -> None:
        self._handle = None
        if not IS_WINDOWS:
            return
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            logger.warning("CreateJobObject failed (%s) - falling back to taskkill only", ctypes.get_last_error())
            return
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _k32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            logger.warning("SetInformationJobObject failed (%s)", ctypes.get_last_error())
            _k32.CloseHandle(handle)
            return
        self._handle = handle

    @property
    def active(self) -> bool:
        return self._handle is not None

    def add(self, proc: subprocess.Popen) -> bool:
        """Put a freshly started child into the job (its own children follow)."""
        if not self._handle:
            return False
        try:
            ok = _k32.AssignProcessToJobObject(self._handle, wintypes.HANDLE(int(proc._handle)))  # type: ignore[attr-defined]
        except Exception:
            logger.exception("AssignProcessToJobObject raised")
            return False
        if not ok:
            logger.warning("Could not add pid %s to the job (%s)", proc.pid, ctypes.get_last_error())
        return bool(ok)

    def close(self) -> None:
        """Closing the last handle terminates every process still in the job."""
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


def kill_tree(pid: int) -> None:
    """Belt and braces next to the job object: kill a process and its children."""
    if not IS_WINDOWS or not pid:
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW, timeout=15)
    except Exception:
        logger.exception("taskkill /T for pid %s failed", pid)


# ---------------------------------------------------------------------------
# Single instance
# ---------------------------------------------------------------------------
class SingleInstance:
    """Named mutex, held for the life of the process. `acquired` is False when
    another launcher already owns it."""

    def __init__(self, name: str) -> None:
        self._handle = None
        self.acquired = True
        if not IS_WINDOWS:
            return
        self._handle = _k32.CreateMutexW(None, False, name)
        self.acquired = ctypes.get_last_error() != _ERROR_ALREADY_EXISTS

    def release(self) -> None:
        if self._handle:
            _k32.CloseHandle(self._handle)
            self._handle = None


# ---------------------------------------------------------------------------
# Windows (the chrome-less browser window)
# ---------------------------------------------------------------------------
def _exe_name(pid: int) -> str:
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _k32.CloseHandle(handle)


def find_app_windows(title_prefix: str) -> list[int]:
    """Top-level Chromium windows whose title starts with `title_prefix`.
    Minimized windows still count (they exist and are visible to the taskbar)."""
    if not IS_WINDOWS:
        return []
    found: list[int] = []

    def _cb(hwnd, _lparam):
        try:
            if not _u32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            _u32.GetClassNameW(hwnd, cls, 64)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            title = ctypes.create_unicode_buffer(256)
            _u32.GetWindowTextW(hwnd, title, 256)
            if not title.value.startswith(title_prefix):
                return True
            pid = wintypes.DWORD(0)
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if _exe_name(pid.value) in ("msedge.exe", "chrome.exe"):
                found.append(int(hwnd))
        except Exception:
            pass
        return True

    _u32.EnumWindows(_WNDENUMPROC(_cb), 0)
    return found


def focus_window(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return False
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, _SW_RESTORE)
    # Windows refuses SetForegroundWindow from a background process; a synthetic
    # Alt key press right before the call is the well-known way to be allowed.
    _u32.keybd_event(0x12, 0, 0, 0)
    _u32.keybd_event(0x12, 0, 0x0002, 0)
    _u32.BringWindowToTop(hwnd)
    return bool(_u32.SetForegroundWindow(hwnd))


def close_window(hwnd: int) -> None:
    """Ask a window to close normally (WM_CLOSE) - gentler than killing it."""
    if IS_WINDOWS:
        _u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)


def message_box(title: str, text: str, error: bool = False) -> None:
    """Show a native dialog. Used because pythonw has no console to print to."""
    logger.info("message box: %s - %s", title, text)
    if not IS_WINDOWS:
        print(f"{title}: {text}")
        return
    _u32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
