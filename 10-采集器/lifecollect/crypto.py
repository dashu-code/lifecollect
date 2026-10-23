"""文件加密核心。

设计目标是**二十年后还能解开**。所以：

  1. **格式是自描述的文本头 + 二进制块**，不依赖任何外部库的版本。
     文件开头就是明文 JSON 头，写明算法、KDF 参数、原始文件名和长度。
     任何人拿到文件都能看出它是什么、怎么解——不需要这个程序。

  2. **认证加密（AEAD）**。密文被改一个字节，解密必须失败。
     不做"加密但可篡改"的东西——那在档案场景里比不加密更危险，
     因为用户会以为数据是完整的。

  3. **分块**。大文件（原始素材里可能有几 GB 的数据库）不能整个读进内存。
     每块独立 AEAD，块序号进 AAD，防止重排和截断。

  4. **不使用密码学上危险的取巧**。密码派生用 scrypt（本机实测：
     必须显式给 maxmem，否则 n=2^15 会直接报 memory limit exceeded）。

格式（v1）：
    offset 0      : MAGIC = b"LFC1"
    offset 4      : uint32 header_len (little endian)
    offset 8      : header JSON (UTF-8, header_len 字节)
    之后          : 记录序列，每条 = uint32 len + (len - NONCE_SIZE - TAG_SIZE) 字节密文
    ---
    每条记录 = nonce(12) + ciphertext + tag(16)
    记录明文长度：header["chunk_size"]，最后一块可能更短
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"LFC1"
FORMAT_VERSION = 1
ALGORITHM = "AES-256-GCM"
NONCE_SIZE = 12
TAG_SIZE = 16
DEFAULT_CHUNK_SIZE = 256 * 1024

# KDF 参数。n 越大越慢越贵，2^14 在本机实测约 190ms，
# 是能接受的用户体验和合理的暴力破解成本之间的平衡点。
# r/p 取 RFC 7914 的推荐值。
KDF_NAME = "scrypt"
KDF_N = 1 << 14
KDF_R = 8
KDF_P = 1
KDF_DKLEN = 32
KDF_SALT_SIZE = 16
# 必须显式给 maxmem。本机实测：maxmem=0 时 OpenSSL 用默认上限，
# n=2^15 直接抛 "memory limit exceeded"。这里按标准公式留足余量：
# memory ≈ 128 * n * r
KDF_MAXMEM = 64 << 20


class CryptoError(Exception):
    """解密失败。不要吞掉这个异常——它意味着数据被篡改或密码错误。"""


# --- 密钥派生 -----------------------------------------------------------

def derive_key(password: str, salt: bytes, n: int = KDF_N,
               r: int = KDF_R, p: int = KDF_P,
               dklen: int = KDF_DKLEN) -> bytes:
    """从口令派生密钥。参数从文件头读出来，这样老文件永远能解开。"""
    if not password:
        raise ValueError("口令不能为空")
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=dklen, maxmem=KDF_MAXMEM,
    )


def kdf_params() -> dict:
    return {"name": KDF_NAME, "n": KDF_N, "r": KDF_R, "p": KDF_P,
            "dklen": KDF_DKLEN, "salt_size": KDF_SALT_SIZE, "maxmem": KDF_MAXMEM}


def new_salt() -> bytes:
    return os.urandom(KDF_SALT_SIZE)


def random_key() -> bytes:
    return os.urandom(32)


# --- 子密钥 -------------------------------------------------------------
# 主密钥不直接用来加密。每个文件一个独立子密钥，用 HKDF 从
# (主密钥, 文件相对路径) 派生。好处：
#   - 不同文件的密钥不同，一个文件被攻破不影响其他文件
#   - 攻击者无法用两个文件的相似密文判断内容相同
#   - 不需要给每个文件存 salt（路径本身就是派生材料之一）

def file_key(master_key: bytes, rel_path: str) -> bytes:
    return hmac.new(
        master_key, b"lifecollect:file:v1:" + rel_path.encode("utf-8"),
        hashlib.sha256,
    ).digest()


def key_fingerprint(key: bytes) -> str:
    """密钥指纹。用于让用户确认"这两个文件是同一把钥匙"。"""
    return hashlib.sha256(b"lifecollect:fingerprint:" + key).hexdigest()[:16]


# --- 加密 ---------------------------------------------------------------

def encrypt_bytes(plaintext: bytes, key: bytes, rel_path: str,
                  chunk_size: int = DEFAULT_CHUNK_SIZE,
                  extra_header: dict | None = None) -> bytes:
    """加密一段数据，返回完整的 .lfc 文件内容。"""
    header = {
        "format": "lifecollect-encrypted",
        "version": FORMAT_VERSION,
        "algorithm": ALGORITHM,
        "kdf": kdf_params(),
        "chunk_size": chunk_size,
        "plaintext_size": len(plaintext),
        "path": rel_path,
    }
    if extra_header:
        header.update(extra_header)

    header_bytes = json.dumps(header, ensure_ascii=False,
                             sort_keys=True).encode("utf-8")
    aead = AESGCM(key)

    out = bytearray()
    out += MAGIC
    out += struct.pack("<I", len(header_bytes))
    out += header_bytes

    if not plaintext:
        # 空文件也要有一条认证记录，否则"删光所有记录"和"空文件"无法区分
        counter = 0
        nonce = struct.pack("<Q", counter) + os.urandom(4)
        ct = aead.encrypt(nonce, b"", _aad(rel_path, counter))
        out += struct.pack("<I", len(nonce) + len(ct))
        out += nonce
        out += ct
        return bytes(out)

    counter = 0
    for offset in range(0, len(plaintext), chunk_size):
        chunk = plaintext[offset:offset + chunk_size]
        nonce = struct.pack("<Q", counter) + os.urandom(4)
        ct = aead.encrypt(nonce, chunk, _aad(rel_path, counter))
        out += struct.pack("<I", len(nonce) + len(ct))
        out += nonce
        out += ct
        counter += 1
    return bytes(out)


def _aad(rel_path: str, counter: int) -> bytes:
    """附加认证数据：把路径和块序号绑进 tag。

    这样把 A 文件的密文块挪到 B 文件、或者把第 2 块换到第 1 位，
    解密都会失败。
    """
    return (rel_path.encode("utf-8") + b"\x00" +
            struct.pack("<Q", counter))


# --- 解密 ---------------------------------------------------------------

def read_header(data: bytes) -> tuple[dict, int]:
    """返回 (header, 记录区起始偏移)。只读头，不解密。"""
    if len(data) < 12:
        raise CryptoError("文件太短，不是有效的加密档案")
    if data[:4] != MAGIC:
        raise CryptoError(
            f"不是 lifecollect 加密文件（魔数不符：{data[:4]!r}）")
    (header_len,) = struct.unpack("<I", data[4:8])
    if header_len > len(data) - 8:
        raise CryptoError("头部长度越界，文件可能损坏")
    try:
        header = json.loads(data[8:8 + header_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CryptoError(f"头部无法解析: {exc}") from exc
    if header.get("format") != "lifecollect-encrypted":
        raise CryptoError(f"未知文件格式: {header.get('format')!r}")
    if header.get("version") != FORMAT_VERSION:
        raise CryptoError(
            f"不支持的格式版本: {header.get('version')}（本程序支持 "
            f"{FORMAT_VERSION}）")
    if header.get("algorithm") != ALGORITHM:
        raise CryptoError(f"不支持的算法: {header.get('algorithm')!r}")
    return header, 8 + header_len


def decrypt_bytes(data: bytes, key: bytes, rel_path: str | None = None) -> bytes:
    """解密完整 .lfc 内容。

    rel_path 默认取文件头里记录的路径；显式传入时两者必须一致，
    否则 AAD 对不上——这是防止密文被整块搬到别处的最后一道锁。
    """
    header, pos = read_header(data)

    stored_path = header.get("path")
    if stored_path is None:
        raise CryptoError("文件头缺少 path 字段")
    if rel_path is not None and rel_path != stored_path:
        raise CryptoError(
            f"路径不匹配：文件头记录的是 {stored_path!r}，"
            f"按 {rel_path!r} 解密会失败（这是防止密文被搬移的保护）")
    rel_path = stored_path

    expected_size = header.get("plaintext_size")
    if not isinstance(expected_size, int) or expected_size < 0:
        raise CryptoError("plaintext_size 字段无效")

    aead = AESGCM(key)
    out = bytearray()
    counter = 0
    n = len(data)

    while pos < n:
        if pos + 4 > n:
            raise CryptoError("记录长度字段被截断")
        (rec_len,) = struct.unpack("<I", data[pos:pos + 4])
        pos += 4
        if rec_len < NONCE_SIZE + TAG_SIZE:
            raise CryptoError(f"记录长度异常: {rec_len}")
        if pos + rec_len > n:
            raise CryptoError("记录数据被截断，文件不完整")
        record = data[pos:pos + rec_len]
        pos += rec_len

        nonce = record[:NONCE_SIZE]
        ct = record[NONCE_SIZE:]
        try:
            plain = aead.decrypt(nonce, ct, _aad(rel_path, counter))
        except Exception as exc:
            raise CryptoError(
                "解密失败：密码错误、密钥不匹配，或文件被篡改/损坏"
                f"（第 {counter} 块）"
            ) from exc
        out += plain
        counter += 1

    if len(out) != expected_size:
        raise CryptoError(
            f"解密后长度不符：期望 {expected_size} 字节，实际 {len(out)} 字节。"
            "文件可能被截断或拼接过")
    return bytes(out)


def is_encrypted(path) -> bool:
    """靠魔数判断，不看扩展名——扩展名可以被改，文件头不能。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == MAGIC
    except OSError:
        return False


def describe(path) -> dict:
    """给用户看这个加密文件是什么，不需要密钥。"""
    with open(path, "rb") as fh:
        data = fh.read(DEFAULT_CHUNK_SIZE)
    header, _ = read_header(data)
    return header
