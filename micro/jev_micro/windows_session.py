"""Temporary display activity for unattended Windows SC2 test sessions."""

import os
from contextlib import contextmanager


@contextmanager
def active_display():
    """Keep the display active and restore the screen-saver flag on exit.

    SPI flags=0 changes only the current session, not the user profile. This
    does not unlock a secured desktop, change passwords or change lock policies.
    """
    if os.name != "nt":
        yield
        return
    import ctypes as C
    from ctypes import wintypes as W
    user = C.WinDLL("user32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    user.SystemParametersInfoW.argtypes = [W.UINT, W.UINT, W.LPVOID, W.UINT]
    user.SystemParametersInfoW.restype = W.BOOL
    kernel.SetThreadExecutionState.argtypes = [W.DWORD]
    kernel.SetThreadExecutionState.restype = W.DWORD
    enabled = W.BOOL()
    queried = user.SystemParametersInfoW(0x10, 0, C.byref(enabled), 0)
    changed = bool(queried and enabled.value and user.SystemParametersInfoW(0x11, 0, None, 0))
    previous_execution_state = kernel.SetThreadExecutionState(0x80000003)
    if changed:
        print("Windows: screen saver temporarily suspended for this test process; restored on exit.", flush=True)
    try:
        yield
    finally:
        if previous_execution_state:
            kernel.SetThreadExecutionState(previous_execution_state | 0x80000000)
        if changed:
            restored = user.SystemParametersInfoW(0x11, 1, None, 0)
            print(f"Windows: screen-saver setting restored={bool(restored)}", flush=True)
