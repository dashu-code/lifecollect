"""档案级加锁 / 解锁。

三条不可违背的规则：

  1. **永远明文**：`00-说明.md`、`01-授权/`、`密钥信封.json`、`清单.json.enc-manifest`。
     否则会做出一个"没人知道是什么、也没人知道谁能用"的加密包——比不加密更糟。

  2. **先验证再删原文**。顺序是：加密 → 用同一把钥匙解一遍验证 →
     确认完全一致 → 才删除明文。反过来做，中途失败就是永久丢数据。

  3. **解锁不删除密文**，除非用户明确要求。解锁只是让用户能读；
     删掉密文才是不可逆的。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import crypto
from .archive import Archive

ENCRYPTED_SUFFIX = ".lfc"
MANIFEST_NAME = "加密清单.json"
MANIFEST_FORMAT = "lifecollect-enc-manifest"

# 这些永远不加密。理由是设计约束，不是便利。
#
# 90-审计 不在加密范围内，这一条值得解释：
#   锁操作本身要写审计日志。如果审计日志也被加密，那么
#   「加密 → 写日志 → 日志变成新的明文」会永远不收敛——
#   锁完之后档案里必然躺着一个新的明文 events.jsonl。
#   所以审计日志**保持明文**。
#
#   代价要说清楚：日志里记录了操作时间、文件数、密钥指纹前几位，
#   不含消息内容和口令。**它泄露的是"有哪些操作发生过"，不是"内容是什么"。**
#   这个代价换的是"整个流程处于稳定终态"——比一个永远差一步的加密更有价值。
ALWAYS_CLEAR = (
    "00-说明.md",
    "01-授权/",
    "90-审计/",
    "密钥信封.json",
    "加密清单.json",
    "_meta.json",
)

# 说明文件里写的"永远明文"清单必须与上面一致，否则文档会骗人。
ALWAYS_CLEAR_DOC = (
    "00-说明.md",
    "01-授权/",
    "90-审计/",
    "密钥信封.json",
    "加密清单.json",
    "_meta.json",
)

# 锁定时要处理的目录。索引层也加密——它含有全部消息正文。
ENCRYPT_DIRS = ("10-原始", "20-派生", "30-索引", "40-呈现")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().replace(
        microsecond=0).isoformat()


def _is_always_clear(rel_posix: str) -> bool:
    for prefix in ALWAYS_CLEAR:
        if rel_posix == prefix or rel_posix.startswith(prefix):
            return True
    return False


def iter_encryptable(archive: Archive):
    """列出所有应该被加密的普通文件。

    陷阱：解锁时保留了密文（默认行为），于是明文和 stale 的 `.lfc`
    同时存在。重新加锁时如果直接跳过 `.lfc`，就会漏掉这些文件。
    所以在产出明文文件之前，先清掉同一路径下已被取代的旧密文——
    明文刚刚被验证过，旧密文是碎片的可能性更高（比如用户手动改过明文）。
    """
    for dirname in ENCRYPT_DIRS:
        base = archive.root / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            rel = archive.rel(path)
            if _is_always_clear(rel):
                continue
            if rel.endswith(ENCRYPTED_SUFFIX):
                continue
            if path.name.startswith("."):
                continue

            stale = path.with_name(path.name + ENCRYPTED_SUFFIX)
            if stale.exists():
                try:
                    stale.unlink()
                except OSError:
                    pass
            yield path, rel


def _manifest_path(archive: Archive) -> Path:
    return archive.root / MANIFEST_NAME


def read_manifest(archive: Archive) -> dict | None:
    p = _manifest_path(archive)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def is_locked(archive: Archive) -> bool:
    """有没有还没解回的密文文件。

    以**磁盘上的实际状态**为准，不信清单里的标志位——清单和磁盘
    不一致时，磁盘是事实。这个判断被 UI 的每个读取接口依赖。
    """
    manifest = read_manifest(archive)
    if not manifest:
        return False
    for entry in manifest.get("entries", []):
        enc = archive.root / entry.get("enc_path", "")
        plain = archive.root / entry.get("rel_path", "")
        if enc.exists() and not plain.exists():
            return True
    return False


# =======================================================================
# 加锁
# =======================================================================

def lock(archive: Archive, master_key: bytes, verbose: bool = True,
         skip_verify: bool = False) -> dict:
    """加密档案里所有应该加密的文件，验证后删除明文。"""
    if not archive.exists():
        raise FileNotFoundError(f"不是一个已初始化的档案: {archive.root}")

    targets = list(iter_encryptable(archive))
    if not targets:
        if verbose:
            print("没有需要加密的文件。")
        return {"files": 0, "bytes_in": 0, "bytes_out": 0, "verified": 0,
                "skipped": 0}

    manifest = read_manifest(archive) or {
        "format": MANIFEST_FORMAT,
        "version": 1,
        "key_fingerprint": crypto.key_fingerprint(master_key),
        "created_at": _now(),
        "entries": [],
    }
    known = {e["rel_path"]: e for e in manifest.get("entries", [])}

    report = {"files": 0, "bytes_in": 0, "bytes_out": 0,
              "verified": 0, "skipped": 0}

    for path, rel in targets:
        if verbose:
            print(f"  加密 {rel} ... ", end="", flush=True)

        plaintext = path.read_bytes()
        key = crypto.file_key(master_key, rel)
        blob = crypto.encrypt_bytes(
            plaintext, key, rel,
            extra_header={"source_sha256": crypto.hashlib.sha256(plaintext).hexdigest(),
                          "source_name": path.name},
        )

        enc_path = path.with_name(path.name + ENCRYPTED_SUFFIX)
        tmp = enc_path.with_name(enc_path.name + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, enc_path)

        # --- 验证：用同一把钥匙解一遍，必须逐字节一致 ---
        if not skip_verify:
            back = crypto.decrypt_bytes(
                enc_path.read_bytes(),
                crypto.file_key(master_key, rel), rel)
            if back != plaintext:
                # 验证失败：保留明文，删掉坏掉的密文，中止。
                # 宁可少加密一个文件，也不能删掉唯一正确的副本。
                try:
                    enc_path.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"加密验证失败：{rel}\n"
                    f"明文已保留，密文已删除。档案未被破坏，但加密未完成。"
                    f"这是一个 bug，请反馈。")
            report["verified"] += 1

        # 验证通过后才删明文
        try:
            path.unlink()
        except OSError as exc:
            raise RuntimeError(
                f"明文删除失败: {rel}（{exc}）。\n"
                f"密文已存在且已验证，可以手动删除明文。") from exc

        known[rel] = {
            "rel_path": rel,
            "enc_path": archive.rel(enc_path),
            "size_plain": len(plaintext),
            "size_enc": len(blob),
            "sha256_plain": crypto.hashlib.sha256(plaintext).hexdigest(),
            "encrypted_at": _now(),
            "decrypted": False,
        }
        report["files"] += 1
        report["bytes_in"] += len(plaintext)
        report["bytes_out"] += len(blob)
        if verbose:
            print("OK")

    manifest["entries"] = list(known.values())
    manifest["updated_at"] = _now()
    manifest["key_fingerprint"] = crypto.key_fingerprint(master_key)
    _write_manifest(archive, manifest)

    archive.audit("locked", {
        "files": report["files"],
        "bytes_in": report["bytes_in"],
        "bytes_out": report["bytes_out"],
        "verified": report["verified"],
        "key_fingerprint": crypto.key_fingerprint(master_key),
    })
    return report


# =======================================================================
# 解锁
# =======================================================================

def unlock(archive: Archive, master_key: bytes, verbose: bool = True,
           remove_ciphertext: bool = False) -> dict:
    """把密文解回明文。默认保留密文。"""
    manifest = read_manifest(archive)
    if manifest is None:
        raise FileNotFoundError(
            f"找不到加密清单: {_manifest_path(archive)}\n"
            "这个档案可能没有加密过，或者清单被删除了。")

    if manifest.get("key_fingerprint") and \
            manifest["key_fingerprint"] != crypto.key_fingerprint(master_key):
        raise ValueError(
            "密钥与档案不匹配。\n"
            f"清单记录的指纹: {manifest['key_fingerprint']}\n"
            f"提供的密钥指纹: {crypto.key_fingerprint(master_key)}\n"
            "用错档案的密钥是解不开的。")

    report = {"files": 0, "bytes": 0, "missing": 0, "failed": 0}
    for entry in manifest.get("entries", []):
        rel = entry["rel_path"]
        enc_path = archive.root / entry["enc_path"]
        out_path = archive.root / rel

        # 明文已经在盘上 → 不需要再解一次
        if out_path.exists() and not entry.get("decrypted"):
            entry["decrypted"] = True
            manifest["_dirty"] = True
        if entry.get("decrypted") and out_path.exists():
            continue

        if not enc_path.exists():
            report["missing"] += 1
            if verbose:
                print(f"  [缺失] {entry['enc_path']}")
            continue

        if verbose:
            print(f"  解密 {rel} ... ", end="", flush=True)
        try:
            blob = enc_path.read_bytes()
            plaintext = crypto.decrypt_bytes(
                blob, crypto.file_key(master_key, rel), rel)
        except crypto.CryptoError as exc:
            report["failed"] += 1
            if verbose:
                print(f"失败: {exc}")
            continue

        expected = entry.get("sha256_plain")
        if expected:
            actual = crypto.hashlib.sha256(plaintext).hexdigest()
            if actual != expected:
                report["failed"] += 1
                if verbose:
                    print("失败: 解密后哈希与清单不符")
                continue

        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + ".tmp")
        tmp.write_bytes(plaintext)
        os.replace(tmp, out_path)
        report["files"] += 1
        report["bytes"] += len(plaintext)

        # 明文落盘就算"已解密"——这与"是否删除密文"是两件独立的事。
        # 之前把这两个绑在一起，导致解锁成功后 is_locked 仍然为 True。
        entry["decrypted"] = True
        entry["decrypted_at"] = _now()
        manifest["_dirty"] = True

        if remove_ciphertext:
            try:
                enc_path.unlink()
                entry["ciphertext_removed"] = True
            except OSError:
                pass

        if verbose:
            print("OK")

    if manifest.pop("_dirty", False):
        manifest["updated_at"] = _now()
        _write_manifest(archive, manifest)

    archive.audit("unlocked", {
        "files": report["files"],
        "bytes": report["bytes"],
        "missing": report["missing"],
        "failed": report["failed"],
        "removed_ciphertext": remove_ciphertext,
    })
    return report


# =======================================================================
# 校验
# =======================================================================

def verify(archive: Archive, master_key: bytes,
           verbose: bool = True) -> dict:
    """逐文件验证「密文能用这把钥匙解开」。不解出明文落盘。"""
    manifest = read_manifest(archive)
    if manifest is None:
        raise FileNotFoundError("找不到加密清单")

    report = {"ok": 0, "failed": 0, "missing": 0, "problems": []}
    for entry in manifest.get("entries", []):
        if entry.get("decrypted"):
            continue
        rel = entry["rel_path"]
        enc_path = archive.root / entry["enc_path"]
        if not enc_path.exists():
            report["missing"] += 1
            report["problems"].append(f"缺失: {entry['enc_path']}")
            continue
        try:
            blob = enc_path.read_bytes()
            plaintext = crypto.decrypt_bytes(
                blob, crypto.file_key(master_key, rel), rel)
            expected = entry.get("sha256_plain")
            if expected and crypto.hashlib.sha256(plaintext).hexdigest() != expected:
                raise crypto.CryptoError("解密后哈希不符")
            report["ok"] += 1
            if verbose:
                print(f"  [OK] {rel}")
        except crypto.CryptoError as exc:
            report["failed"] += 1
            report["problems"].append(f"{rel}: {exc}")
            if verbose:
                print(f"  [坏] {rel}: {exc}")
    return report


def status(archive: Archive) -> dict:
    """不需要密钥就能看的状态。"""
    manifest = read_manifest(archive)
    enc_files = list(archive.root.rglob("*" + ENCRYPTED_SUFFIX))
    plain_files = []
    for dirname in ENCRYPT_DIRS:
        base = archive.root / dirname
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and not p.name.endswith(ENCRYPTED_SUFFIX):
                plain_files.append(archive.rel(p))

    return {
        "locked": is_locked(archive),
        "encrypted_files": len(enc_files),
        "plain_files": len(plain_files),
        "manifest_entries": len(manifest.get("entries", [])) if manifest else 0,
        "key_fingerprint": manifest.get("key_fingerprint") if manifest else None,
        "always_clear": list(ALWAYS_CLEAR),
        "plain_file_list": plain_files,
    }


def _write_manifest(archive: Archive, manifest: dict) -> None:
    p = _manifest_path(archive)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)


def read_manifest_public(archive: Archive) -> dict | None:
    """给 presents 用：不涉及密钥的清单信息。"""
    return read_manifest(archive)
