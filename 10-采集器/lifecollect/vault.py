"""密钥管理与信封。

这个模块回答一个问题：**主密钥怎么存，才能在二十年内、换了电脑、
甚至作者不在了之后，仍然能取出来。**

三种方式（对应标准 §5 的三个安全级别）：

  basic     → 不加密。没有任何密钥。
  standard  → 主密钥被 Windows DPAPI 包裹（绑定当前 Windows 账户），
              **同时**被用户口令包裹一份。日常在本人机器上免输密码；
              换机器 / 账户重装后凭口令恢复。
  paranoid  → 只用口令包裹，不落 DPAPI。每次解锁都要输口令。

    为什么 standard 也要给口令包裹：
    只靠 DPAPI 的话，Windows 账户一旦重建（换主板、重装系统、账户迁移），
    密钥就永久取不回来了。DPAPI 是便利性，不是保险。真正保证
    "换了设备还能打开"的是口令 + 文件里保存的 KDF 参数。
    这一点必须写进给用户的说明，否则用户会以为"他电脑上能开"等于"永远能开"。

信封文件是**明文 JSON**，放在档案根目录。它本身不泄露密钥——只包含
包裹后的密钥和恢复所需的 KDF 参数。明文是刻意的：二十年后没有这个程序时，
一个懂密码学的人能照着文件头把它解开。
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import crypto


class VaultError(Exception):
    pass


ENVELOPE_NAME = "密钥信封.json"
ENVELOPE_FORMAT = "lifecollect-key-envelope"
ENVELOPE_VERSION = 1

WAY_DPAPI = "dpapi"
WAY_PASSWORD = "password"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().replace(
        microsecond=0).isoformat()


# =======================================================================
# Windows DPAPI
# =======================================================================

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _make_blob(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    return blob, buf


def _blob_bytes(blob: _DATA_BLOB) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def dpapi_available() -> bool:
    return sys.platform == "win32"


def dpapi_protect(data: bytes) -> bytes:
    """用当前 Windows 账户加密。同一台机器同一账户才能解开。"""
    if not dpapi_available():
        raise VaultError("DPAPI 只在 Windows 上可用")
    blob_in, _buf = _make_blob(data)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise VaultError(
            f"DPAPI 加密失败（Windows 错误码 {ctypes.get_last_error()}）")
    try:
        return _blob_bytes(blob_out)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(data: bytes) -> bytes:
    if not dpapi_available():
        raise VaultError("DPAPI 只在 Windows 上可用")
    blob_in, _buf = _make_blob(data)
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out))
    if not ok:
        raise VaultError(
            "DPAPI 解密失败：这个档案不是在这台电脑的这个 Windows 账户下加密的，"
            "或者 Windows 账户已被重建。请改用口令解锁。"
            f"（Windows 错误码 {ctypes.get_last_error()}）")
    try:
        return _blob_bytes(blob_out)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


# =======================================================================
# 信封
# =======================================================================

def _wrap_with_password(master_key: bytes, password: str) -> dict:
    salt = crypto.new_salt()
    nonce = os.urandom(crypto.NONCE_SIZE)
    kek = crypto.derive_key(password, salt)
    # 用 AESGCM 直接包（不用 crypto.encrypt_bytes——那层是给文件用的，
    # 有自己的头格式，这里只要一个最小的包裹）
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    wrapped = AESGCM(kek).encrypt(nonce, master_key, b"lifecollect:keywrap:v1")
    return {
        "type": crypto.KDF_NAME,
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "wrapped_key": _b64(wrapped),
        "created_at": _now(),
        "fingerprint_hint": crypto.key_fingerprint(master_key),
    }


def _unwrap_with_password(way: dict, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    kdf = crypto.kdf_params()
    salt = _unb64(way["salt"])
    kek = crypto.derive_key(password, salt, n=kdf["n"], r=kdf["r"],
                            p=kdf["p"], dklen=kdf["dklen"])
    try:
        return AESGCM(kek).decrypt(
            _unb64(way["nonce"]), _unb64(way["wrapped_key"]),
            b"lifecollect:keywrap:v1")
    except Exception as exc:
        raise VaultError("口令错误，或密钥信封被篡改") from exc


def create_envelope(master_key: bytes, password: str,
                    security_level: str) -> dict:
    """生成信封。security_level 决定放哪几种解锁方式。"""
    if security_level not in ("standard", "paranoid"):
        raise VaultError(f"该安全级别不需要信封: {security_level}")
    if not password:
        raise VaultError(
            "加密档案必须设置口令。只靠 Windows 账户保护是不够的——"
            "换电脑或重装系统后就再也打不开了。")

    envelope = {
        "format": ENVELOPE_FORMAT,
        "version": ENVELOPE_VERSION,
        "security_level": security_level,
        "created_at": _now(),
        "kdf": crypto.kdf_params(),
        "key_fingerprint": crypto.key_fingerprint(master_key),
        "ways": [],
        "recovery": {
            "how_to_open_without_this_program": (
                "本文件是明文 JSON。主密钥被以下任一方式包裹："
                "(1) password 方式——用 kdf 段记录的 scrypt 参数和 salt "
                "从口令派生出 32 字节密钥，用它做 AES-256-GCM 解密 "
                "wrapped_key（nonce 见同段，AAD 为 "
                "'lifecollect:keywrap:v1'）；"
                "(2) dpapi 方式——只能在原 Windows 账户下用 "
                "CryptUnprotectData 解开。"
                "拿到主密钥后，每个文件用 HMAC-SHA256(key=主密钥, "
                "msg='lifecollect:file:v1:'+文件相对路径) 派生子密钥，"
                "再按文件头声明的算法解密。"
            ),
            "warning": (
                "口令丢失且 Windows 账户已重建，则主密钥永久不可恢复。"
                "没有任何人（包括本程序的作者）能帮你找回。"
            ),
        },
        "manually_removed_ways": [],
    }

    # 口令方式永远存在——它是可移植性的唯一保证
    envelope["ways"].append({
        "way": WAY_PASSWORD,
        "note": "跨设备可用。知道口令就能在任何机器上恢复。",
        **_wrap_with_password(master_key, password),
    })

    if security_level == "standard" and dpapi_available():
        envelope["ways"].append({
            "way": WAY_DPAPI,
            "note": "仅限本机本账户。换电脑会失效，请用口令方式。",
            "protected_key": _b64(dpapi_protect(master_key)),
            "created_at": _now(),
            "fingerprint_hint": crypto.key_fingerprint(master_key),
        })

    return envelope


def save_envelope(archive_root: Path, envelope: dict) -> Path:
    path = Path(archive_root) / ENVELOPE_NAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_envelope(archive_root: Path) -> dict:
    path = Path(archive_root) / ENVELOPE_NAME
    if not path.exists():
        raise VaultError(
            f"找不到密钥信封: {path}\n"
            "这个档案可能没有加密，或者被移动过。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise VaultError(f"密钥信封损坏，无法解析: {exc}") from exc
    if data.get("format") != ENVELOPE_FORMAT:
        raise VaultError(f"不是 lifecollect 密钥信封: {data.get('format')!r}")
    return data


def envelope_exists(archive_root: Path) -> bool:
    return (Path(archive_root) / ENVELOPE_NAME).exists()


def describe_ways(envelope: dict) -> list[str]:
    return [w.get("way", "?") for w in envelope.get("ways", [])]


def unlock_key(envelope: dict, password: str | None = None,
               allow_dpapi: bool = True) -> bytes:
    """按可用方式依次尝试解开主密钥。

    顺序：先试 DPAPI（免输口令），失败再试口令。
    这个顺序是有意的——日常使用不该每次都让用户敲密码。
    """
    ways = envelope.get("ways", [])
    if not ways:
        raise VaultError(
            "这个信封里没有任何解锁方式。档案已不可恢复。\n"
            "（除非你手工备份过主密钥）")

    # 1. DPAPI
    if allow_dpapi:
        for way in ways:
            if way.get("way") != WAY_DPAPI:
                continue
            try:
                key = dpapi_unprotect(_unb64(way["protected_key"]))
                _verify_fingerprint(envelope, key)
                return key
            except VaultError:
                # DPAPI 失败是预期情况（换机器 / 账户重建），继续试口令
                continue

    # 2. 口令
    pw_ways = [w for w in ways if w.get("way") == WAY_PASSWORD]
    if not pw_ways:
        raise VaultError(
            "没有可用的口令解锁方式，且 Windows 账户解锁失败。\n"
            "这个档案在这台机器上打不开了。")
    if password is None:
        raise VaultError("需要口令")
    last_err: Exception | None = None
    for way in pw_ways:
        try:
            key = _unwrap_with_password(way, password)
            _verify_fingerprint(envelope, key)
            return key
        except VaultError as exc:
            last_err = exc
    raise VaultError(str(last_err) if last_err else "口令错误")


def _verify_fingerprint(envelope: dict, key: bytes) -> None:
    """解开后立刻验证，防止解开一个"看起来对"的错密钥。"""
    expected = envelope.get("key_fingerprint")
    if expected and crypto.key_fingerprint(key) != expected:
        raise VaultError(
            "解出的密钥与信封记录的指纹不符。信封可能被篡改。")


def change_password(archive_root: Path, old_password: str,
                    new_password: str) -> None:
    """换口令。**只重新包裹主密钥，不重新加密档案。**

    这是重要的设计：换口令不应该意味着重写几 GB 的文件，
    也不应该有机会把数据搞坏。
    """
    envelope = load_envelope(archive_root)
    master_key = unlock_key(envelope, old_password, allow_dpapi=False)

    ways = [w for w in envelope.get("ways", [])
            if w.get("way") != WAY_PASSWORD]
    ways.append({
        "way": WAY_PASSWORD,
        "note": "跨设备可用。知道口令就能在任何机器上恢复。",
        **_wrap_with_password(master_key, new_password),
    })
    envelope["ways"] = ways
    envelope["updated_at"] = _now()
    save_envelope(archive_root, envelope)


def remove_dpapi_way(archive_root: Path) -> bool:
    """删掉 DPAPI 解锁方式（比如用户打算把档案拷到别的机器）。

    返回是否真的有东西被删。
    """
    envelope = load_envelope(archive_root)
    before = len(envelope.get("ways", []))
    envelope["ways"] = [w for w in envelope.get("ways", [])
                        if w.get("way") != WAY_DPAPI]
    if len(envelope["ways"]) == before:
        return False
    envelope.setdefault("manually_removed_ways", []).append({
        "way": WAY_DPAPI, "removed_at": _now(),
    })
    envelope["updated_at"] = _now()
    save_envelope(archive_root, envelope)
    return True
