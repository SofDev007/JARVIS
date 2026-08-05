"""Windows DPAPI (Data Protection API) via ctypes — no extra dependency.

``CryptProtectData`` / ``CryptUnprotectData`` in the CurrentUser scope tie a
blob to the logged-in Windows account: only the same user on the same machine
can decrypt it, and the key is managed by the OS, never held by us. We use it
for the two things that must never sit on disk in the clear — device private
keys and the audit tail anchor.

``pywin32`` would give the same calls, but Phase A already established the
"subprocess/ctypes over a new dependency" policy; ctypes keeps this
dependency-free. ``ctypes.windll`` is referenced only inside the functions so
this module still *imports* on non-Windows (calling then raises, clearly).

An optional ``entropy`` argument is a second, application-supplied secret mixed
into the protection, so one protected blob cannot be unprotected by a different
feature that happens to run as the same user.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class DpapiError(RuntimeError):
    """DPAPI protect/unprotect failed, or DPAPI is unavailable."""


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def is_available() -> bool:
    return sys.platform == "win32"


def _blob(data: bytes):
    """Build a DATA_BLOB and return it with the backing buffer (keep alive)."""
    buffer = ctypes.create_string_buffer(bytes(data), len(data))
    return _DATA_BLOB(len(data), ctypes.cast(
        buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _run(func, data: bytes, entropy: bytes) -> bytes:
    if not is_available():
        raise DpapiError("DPAPI is only available on Windows")
    in_blob, _in_buf = _blob(data)
    ent_blob, _ent_buf = _blob(entropy)
    out_blob = _DATA_BLOB()
    _CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = func(
        ctypes.byref(in_blob),
        None,  # description
        ctypes.byref(ent_blob) if entropy else None,
        None, None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise DpapiError(
            f"{func.__name__} failed (Windows error "
            f"{ctypes.GetLastError()})")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


def protect(data: bytes, entropy: bytes = b"") -> bytes:
    """Encrypt *data* to the CurrentUser. Returns the opaque protected blob."""
    return _run(ctypes.windll.crypt32.CryptProtectData, data, entropy)


def unprotect(blob: bytes, entropy: bytes = b"") -> bytes:
    """Decrypt a blob produced by :func:`protect` (same user, same entropy)."""
    return _run(ctypes.windll.crypt32.CryptUnprotectData, blob, entropy)
