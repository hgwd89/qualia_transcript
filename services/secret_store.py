from __future__ import annotations

import base64
import os
from dataclasses import dataclass

from models.setting import AppSetting


PROTECTED_PREFIX = "dpapi:v1:"
SECRET_SETTING_KEYS = {
    "openai_api_key",
    "rakuten_application_id",
    "rakuten_access_key",
}


class SecretStorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class SecretMigrationResult:
    migrated_keys: tuple[str, ...]
    skipped_keys: tuple[str, ...]


def is_protected_secret(value: str | None) -> bool:
    return bool(value and str(value).startswith(PROTECTED_PREFIX))


def _protect_windows(plaintext: str) -> str:
    if os.name != "nt":
        raise SecretStorageError("Windows DPAPI is only available on Windows")

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    raw = plaintext.encode("utf-8")
    raw_buffer = ctypes.create_string_buffer(raw)
    in_blob = DATA_BLOB(
        len(raw),
        ctypes.cast(raw_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = DATA_BLOB()

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Qualia Transcript secret",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStorageError(
            f"CryptProtectData failed with Windows error {ctypes.get_last_error()}"
        )

    try:
        protected = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))

    return PROTECTED_PREFIX + base64.b64encode(protected).decode("ascii")


def _unprotect_windows(stored: str) -> str:
    if os.name != "nt":
        raise SecretStorageError("Windows DPAPI is only available on Windows")
    if not is_protected_secret(stored):
        raise SecretStorageError("secret is not DPAPI protected")

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_byte)),
        ]

    try:
        protected = base64.b64decode(
            stored[len(PROTECTED_PREFIX):], validate=True
        )
    except Exception as exc:
        raise SecretStorageError("protected secret payload is invalid") from exc

    protected_buffer = ctypes.create_string_buffer(protected)
    in_blob = DATA_BLOB(
        len(protected),
        ctypes.cast(protected_buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    out_blob = DATA_BLOB()

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStorageError(
            f"CryptUnprotectData failed with Windows error {ctypes.get_last_error()}"
        )

    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretStorageError("decrypted secret is not valid UTF-8") from exc


def protect_secret(plaintext: str) -> str:
    value = str(plaintext or "")
    if not value:
        return ""
    return _protect_windows(value)


def unprotect_secret(stored: str) -> str:
    return _unprotect_windows(stored)


def set_secret_setting(key: str, value: str) -> None:
    if key not in SECRET_SETTING_KEYS:
        raise ValueError(f"unsupported secret setting: {key}")
    protected = protect_secret(value)
    AppSetting.set(key, protected)


def get_secret_setting(key: str, fallback: str = "") -> str:
    if key not in SECRET_SETTING_KEYS:
        raise ValueError(f"unsupported secret setting: {key}")
    stored = AppSetting.get(key)
    if not stored:
        return str(fallback or "")
    if is_protected_secret(stored):
        return unprotect_secret(stored)
    # Legacy plaintext remains readable until startup migration succeeds.
    return str(stored)


def secret_setting_is_configured(key: str, fallback: str = "") -> bool:
    try:
        return bool(get_secret_setting(key, fallback))
    except SecretStorageError:
        return False


def migrate_legacy_plaintext_secrets() -> SecretMigrationResult:
    """Encrypt legacy plaintext DB secrets for the current Windows user.

    Migration is intentionally Windows-only. Non-Windows environments continue
    to use environment-variable fallbacks and must not rewrite secrets into a
    weaker DB representation.
    """
    if os.name != "nt":
        return SecretMigrationResult((), tuple(sorted(SECRET_SETTING_KEYS)))

    migrated: list[str] = []
    skipped: list[str] = []
    for key in sorted(SECRET_SETTING_KEYS):
        stored = AppSetting.get(key)
        if not stored or is_protected_secret(stored):
            skipped.append(key)
            continue
        AppSetting.set(key, protect_secret(stored))
        migrated.append(key)
    return SecretMigrationResult(tuple(migrated), tuple(skipped))
