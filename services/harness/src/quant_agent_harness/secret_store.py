from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


CRYPTPROTECT_UI_FORBIDDEN = 0x01


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDPAPI:
    """Encrypt secrets for the current Windows user without a master password."""

    @staticmethod
    def encrypt(plaintext: str) -> bytes:
        if os.name != "nt":
            raise RuntimeError("当前版本的安全密钥存储仅支持 Windows DPAPI")
        raw = plaintext.encode("utf-8")
        if not raw:
            raise ValueError("不能加密空密钥")
        input_blob, input_buffer = _blob(raw)
        output_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "QuantAgent secret",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            kernel32.LocalFree(output_blob.pbData)

    @staticmethod
    def decrypt(ciphertext: bytes) -> str:
        if os.name != "nt":
            raise RuntimeError("当前版本的安全密钥存储仅支持 Windows DPAPI")
        if not ciphertext:
            return ""
        input_blob, input_buffer = _blob(ciphertext)
        output_blob = _DataBlob()
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        del input_buffer
        if not success:
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
        finally:
            kernel32.LocalFree(output_blob.pbData)


def _blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(value, len(value))
    pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
    return _DataBlob(len(value), pointer), buffer

