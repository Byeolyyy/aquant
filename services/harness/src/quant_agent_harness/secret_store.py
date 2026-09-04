"""密钥存储。

两种后端，按部署形态选：

- Windows 桌面版用 DPAPI 加密后存进 SQLite，随当前用户绑定，无需主密码。
- 服务端用环境变量只读托管：密钥由 systemd 的 EnvironmentFile 提供，
  根本不进数据库。Web 可达的库里没有密钥材料，比"换个算法加密存进去"
  更安全，也省掉一个加密库依赖。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Any, Callable, ContextManager, Protocol, runtime_checkable


CRYPTPROTECT_UI_FORBIDDEN = 0x01


@runtime_checkable
class SecretBackend(Protocol):
    def is_configured(self, key: str) -> bool: ...

    def get(self, key: str) -> str: ...

    def set(self, key: str, plaintext: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def describe(self) -> str: ...


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


def env_var_name(key: str) -> str:
    """密钥名到环境变量名：model.api_key → QUANT_AGENT_MODEL_API_KEY。"""
    return "QUANT_AGENT_" + key.upper().replace(".", "_")


class DpapiSecretBackend:
    """桌面版：DPAPI 加密后存进 SQLite 的 secrets 表。"""

    def __init__(self, connect: Callable[[], ContextManager[Any]], lock: Any):
        self._connect = connect
        self._lock = lock

    def is_configured(self, key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM secrets WHERE secret_key=?", (key,)).fetchone()
        return row is not None

    def get(self, key: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT encrypted_value FROM secrets WHERE secret_key=?", (key,)
            ).fetchone()
        if row is None:
            return ""
        return WindowsDPAPI.decrypt(bytes(row["encrypted_value"]))

    def set(self, key: str, plaintext: str) -> None:
        encrypted = WindowsDPAPI.encrypt(plaintext)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO secrets(secret_key, encrypted_value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(secret_key) DO UPDATE SET
                    encrypted_value=excluded.encrypted_value,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (key, encrypted),
            )

    def delete(self, key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM secrets WHERE secret_key=?", (key,))

    def describe(self) -> str:
        return "Windows DPAPI（当前用户）"


class EnvVarSecretBackend:
    """服务端：密钥由环境变量托管，只读。

    密钥不进数据库，所以 Web 可达的 SQLite 里没有任何密钥材料。
    访客也就无从修改——写入路径直接拒绝。
    """

    def is_configured(self, key: str) -> bool:
        return bool(self.get(key))

    def get(self, key: str) -> str:
        return os.environ.get(env_var_name(key), "").strip()

    def set(self, key: str, plaintext: str) -> None:
        raise ValueError(f"服务端密钥由环境变量 {env_var_name(key)} 配置，不能在应用内修改")

    def delete(self, key: str) -> None:
        raise ValueError(f"服务端密钥由环境变量 {env_var_name(key)} 配置，不能在应用内清除")

    def describe(self) -> str:
        return "服务端环境变量（只读）"


def make_secret_backend(connect: Callable[[], ContextManager[Any]], lock: Any) -> SecretBackend:
    """选后端：显式指定优先，否则按平台——Windows 用 DPAPI，其余用环境变量。"""
    choice = os.environ.get("QUANT_AGENT_SECRET_BACKEND", "").strip().lower()
    if choice == "env":
        return EnvVarSecretBackend()
    if choice == "dpapi":
        return DpapiSecretBackend(connect, lock)
    return DpapiSecretBackend(connect, lock) if os.name == "nt" else EnvVarSecretBackend()

