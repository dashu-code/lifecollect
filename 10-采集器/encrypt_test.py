"""加密相关的测试。

加密和别的功能测试不一样：**"能跑通"不等于"安全"**。
所以这里一半的用例在测"不该成功的时候必须失败"——
篡改、错密钥、截断、块重排，任何一个没被拦住都是严重缺陷。

测试用较小的 scrypt 参数跑（n=2^14 每次约 190ms，太多会拖慢整个套件），
但另有一个用例专门验证生产参数能跑通。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifecollect import crypto, encrypt, vault           # noqa: E402
from lifecollect import records as R                     # noqa: E402
from lifecollect.archive import Archive, read_jsonl       # noqa: E402
from lifecollect import pipeline                          # noqa: E402

# 测试用快速参数。生产参数在 test_production_kdf_params_work 里单独验证。
FAST = dict(n=2 ** 10, r=8, p=1)


def fast_derive(password: str, salt: bytes, **kw) -> bytes:
    return crypto.derive_key(password, salt, **{**FAST, **kw})


class TestCryptoCore(unittest.TestCase):
    KEY = b"\x01" * 32

    def test_roundtrip(self):
        for size in (0, 1, 100, 65536, 300_000):
            with self.subTest(size=size):
                data = os.urandom(size)
                blob = crypto.encrypt_bytes(data, self.KEY, "a/b.txt")
                back = crypto.decrypt_bytes(blob, self.KEY)
                self.assertEqual(back, data)

    def test_header_is_readable_without_key(self):
        """不加密的头部是设计的一部分：二十年后要能看出这是什么。"""
        blob = crypto.encrypt_bytes(b"hello", self.KEY, "x/y.md")
        header, _ = crypto.read_header(blob)
        self.assertEqual(header["format"], "lifecollect-encrypted")
        self.assertEqual(header["algorithm"], "AES-256-GCM")
        self.assertEqual(header["path"], "x/y.md")
        self.assertEqual(header["plaintext_size"], 5)
        self.assertIn("kdf", header)

    def test_ciphertext_differs_each_time(self):
        """同样的明文两次加密必须不同——nonce 随机化的证明。"""
        a = crypto.encrypt_bytes(b"same content", self.KEY, "p")
        b = crypto.encrypt_bytes(b"same content", self.KEY, "p")
        self.assertNotEqual(a, b)
        self.assertEqual(crypto.decrypt_bytes(a, self.KEY),
                         crypto.decrypt_bytes(b, self.KEY))

    def test_tamper_ciphertext_detected(self):
        blob = bytearray(crypto.encrypt_bytes(b"important data", self.KEY, "p"))
        # 改最后一个字节（在 tag 里）
        blob[-1] ^= 0xFF
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(bytes(blob), self.KEY)

    def test_tamper_any_byte_detected(self):
        """逐字节翻转，必须全部被发现。漏一个就是认证失败。"""
        original = crypto.encrypt_bytes(b"0123456789" * 10, self.KEY, "p")
        start = len(original) - 30   # 只看密文和 tag 区
        for i in range(start, len(original)):
            for mask in (0x01, 0x80, 0xFF):
                blob = bytearray(original)
                blob[i] ^= mask
                with self.subTest(i=i, mask=mask):
                    with self.assertRaises(crypto.CryptoError):
                        crypto.decrypt_bytes(bytes(blob), self.KEY)

    def test_wrong_key_rejected(self):
        blob = crypto.encrypt_bytes(b"secret", self.KEY, "p")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(blob, b"\x02" * 32)

    def test_path_mismatch_rejected(self):
        """A 文件的密文不能被当作 B 文件解开。"""
        blob = crypto.encrypt_bytes(b"data", self.KEY, "fileA.txt")
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(blob, self.KEY, rel_path="fileB.txt")

    def test_truncation_detected(self):
        blob = crypto.encrypt_bytes(b"x" * 100_000, self.KEY, "p",
                                    chunk_size=1000)
        for cut in (len(blob) - 1, len(blob) // 2, 20):
            with self.subTest(cut=cut):
                with self.assertRaises(crypto.CryptoError):
                    crypto.decrypt_bytes(blob[:cut], self.KEY)

    def test_empty_file_roundtrip(self):
        blob = crypto.encrypt_bytes(b"", self.KEY, "empty")
        self.assertEqual(crypto.decrypt_bytes(blob, self.KEY), b"")

    def test_record_reordering_detected(self):
        """把两个密文记录调换位置，必须被发现（块序号进 AAD）。"""
        data = b"A" * 1000 + b"B" * 1000 + b"C" * 1000
        blob = crypto.encrypt_bytes(data, self.KEY, "p", chunk_size=1000)
        header, pos = crypto.read_header(blob)
        recs = []
        p = pos
        while p < len(blob):
            (n,) = struct.unpack("<I", blob[p:p + 4])
            recs.append(blob[p:p + 4 + n])
            p += 4 + n
        self.assertEqual(len(recs), 3)
        swapped = blob[:pos] + recs[1] + recs[0] + recs[2]
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(swapped, self.KEY)

    def test_record_dropped_detected(self):
        """删掉一条记录，长度校验必须拦住。"""
        data = b"A" * 1000 + b"B" * 1000
        blob = crypto.encrypt_bytes(data, self.KEY, "p", chunk_size=1000)
        header, pos = crypto.read_header(blob)
        p = pos
        (n,) = struct.unpack("<I", blob[p:p + 4])
        dropped = blob[:pos] + blob[p + 4 + n:]
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(dropped, self.KEY)

    def test_bad_magic_rejected(self):
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(b"XXXX" + b"\x00" * 100, self.KEY)

    def test_unknown_version_rejected(self):
        blob = bytearray(crypto.encrypt_bytes(b"x", self.KEY, "p"))
        header, pos = crypto.read_header(bytes(blob))
        header["version"] = 999
        hb = json.dumps(header, sort_keys=True).encode()
        new = bytearray()
        new += crypto.MAGIC
        new += struct.pack("<I", len(hb))
        new += hb
        new += bytes(blob[pos:])
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_bytes(bytes(new), self.KEY)

    def test_file_key_is_per_path(self):
        k1 = crypto.file_key(self.KEY, "a.txt")
        k2 = crypto.file_key(self.KEY, "b.txt")
        self.assertNotEqual(k1, k2)
        self.assertEqual(len(k1), 32)

    def test_production_kdf_params_work(self):
        """生产参数必须真的能跑——本机实测 maxmem 不够会直接抛错。"""
        salt = crypto.new_salt()
        key = crypto.derive_key("password", salt)
        self.assertEqual(len(key), 32)
        # 同样的输入必须得到同样的键
        self.assertEqual(key, crypto.derive_key("password", salt))
        # 不同 salt 必须不同
        self.assertNotEqual(key, crypto.derive_key("password", crypto.new_salt()))

    def test_scrypt_maxmem_guarded(self):
        """记录一个实测事实：maxmem=0 时 OpenSSL 默认上限会拒绝 n=2^15。

        所以生产代码必须显式传 maxmem——不传的话用户在大档案上会
        突然遇到 "memory limit exceeded"，而且看不出原因。
        """
        import hashlib
        with self.assertRaises(ValueError):
            hashlib.scrypt(b"pw", salt=b"\x00" * 16, n=2 ** 15, r=8, p=1,
                           dklen=32, maxmem=0)
        # 显式给足 maxmem 就应该成功
        hashlib.scrypt(b"pw", salt=b"\x00" * 16, n=2 ** 15, r=8, p=1,
                       dklen=32, maxmem=64 << 20)

    def test_is_encrypted_by_magic(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        enc = tmp / "a.txt"
        enc.write_bytes(crypto.encrypt_bytes(b"x", self.KEY, "a.txt"))
        plain = tmp / "b.txt"
        plain.write_text("x", encoding="utf-8")
        fake_ext = tmp / "c.lfc"
        fake_ext.write_text("not encrypted", encoding="utf-8")
        self.assertTrue(crypto.is_encrypted(enc))
        self.assertFalse(crypto.is_encrypted(plain))
        self.assertFalse(crypto.is_encrypted(fake_ext))


class TestVault(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_envelope_has_password_way_always(self):
        master = crypto.random_key()
        env = vault.create_envelope(master, "pw12345678", "standard")
        ways = vault.describe_ways(env)
        self.assertIn(vault.WAY_PASSWORD, ways)

    def test_envelope_format_is_documented(self):
        master = crypto.random_key()
        env = vault.create_envelope(master, "pw12345678", "standard")
        self.assertEqual(env["format"], vault.ENVELOPE_FORMAT)
        self.assertIn("how_to_open_without_this_program", env["recovery"])
        self.assertIn("永久不可恢复", env["recovery"]["warning"])
        self.assertEqual(env["key_fingerprint"], crypto.key_fingerprint(master))

    def test_paranoid_has_no_dpapi_way(self):
        master = crypto.random_key()
        env = vault.create_envelope(master, "pw12345678", "paranoid")
        self.assertNotIn(vault.WAY_DPAPI, vault.describe_ways(env))

    def test_password_unlock_roundtrip_for_real(self):
        """用生产 KDF 参数真实打一次包再解开。比较慢，但必须测一次。"""
        master = crypto.random_key()
        wrapped = vault._wrap_with_password(master, "real password 123")
        key = vault._unwrap_with_password(wrapped, "real password 123")
        self.assertEqual(key, master)

    def test_wrong_password_rejected(self):
        master = crypto.random_key()
        wrapped = vault._wrap_with_password(master, "right password")
        with self.assertRaises(vault.VaultError):
            vault._unwrap_with_password(wrapped, "wrong password")

    def test_empty_password_refused(self):
        master = crypto.random_key()
        with self.assertRaises(vault.VaultError):
            vault.create_envelope(master, "", "standard")

    def test_fingerprint_mismatch_detected(self):
        """信封被篡改成记录另一个指纹时，必须拒绝。"""
        master = crypto.random_key()
        env = vault.create_envelope(master, "pw12345678", "standard")
        env["key_fingerprint"] = crypto.key_fingerprint(crypto.random_key())
        # 模拟：口令方式能解开，但指纹不符
        wrapped = env["ways"][0]
        key = vault._unwrap_with_password(wrapped, "pw12345678")
        with self.assertRaises(vault.VaultError):
            vault._verify_fingerprint(env, key)

    def test_save_and_load(self):
        master = crypto.random_key()
        env = vault.create_envelope(master, "pw12345678", "standard")
        vault.save_envelope(self.tmp, env)
        loaded = vault.load_envelope(self.tmp)
        self.assertTrue(vault.envelope_exists(self.tmp))
        self.assertEqual(loaded["key_fingerprint"], env["key_fingerprint"])

    def test_corrupt_envelope_reports_clearly(self):
        (self.tmp / vault.ENVELOPE_NAME).write_text("{not json", encoding="utf-8")
        with self.assertRaises(vault.VaultError):
            vault.load_envelope(self.tmp)

    def test_missing_envelope_reports_clearly(self):
        with self.assertRaises(vault.VaultError):
            vault.load_envelope(self.tmp)

    def test_change_password_keeps_master_key(self):
        master = crypto.random_key()
        env = vault.create_envelope(master, "old password", "standard")
        vault.save_envelope(self.tmp, env)
        vault.change_password(self.tmp, "old password", "new password")
        loaded = vault.load_envelope(self.tmp)
        key = vault.unlock_key(loaded, "new password", allow_dpapi=False)
        self.assertEqual(key, master)
        with self.assertRaises(vault.VaultError):
            vault.unlock_key(loaded, "old password", allow_dpapi=False)

    def test_dpapi_way_can_be_removed(self):
        if not vault.dpapi_available():
            self.skipTest("非 Windows")
        master = crypto.random_key()
        env = vault.create_envelope(master, "pw12345678", "standard")
        vault.save_envelope(self.tmp, env)
        self.assertIn(vault.WAY_DPAPI, vault.describe_ways(env))
        self.assertTrue(vault.remove_dpapi_way(self.tmp))
        loaded = vault.load_envelope(self.tmp)
        self.assertNotIn(vault.WAY_DPAPI, vault.describe_ways(loaded))
        # 已删的记录要留档
        self.assertTrue(loaded.get("manually_removed_ways"))


class TestArchiveEncryption(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "arch"
        self.key = crypto.random_key()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_archive(self, level="standard"):
        src = self.tmp / "src"
        src.mkdir(exist_ok=True)
        (src / "c.csv").write_text(
            "时间,发送者,内容\n"
            "2024-03-01 10:02:11,老王,我觉得这个方案其实没必要改，关键是成本\n"
            "2024-03-01 10:03:40,我,我不同意，本质上问题在于排期不是成本\n",
            encoding="utf-8")
        (src / "n.txt").write_text("关于要不要换工作\n我一直在想这件事。\n",
                                   encoding="utf-8")
        a = Archive(self.root)
        a.init(security_level=level, subject="测试")
        pipeline.import_paths(a, [src / "c.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.import_paths(a, [src / "n.txt"], R.SOURCE_DOCUMENTS,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        return a

    def _attach_envelope(self, a, password="pw12345678", level="standard"):
        env = vault.create_envelope(self.key, password, level)
        vault.save_envelope(a.root, env)

    def test_lock_encrypts_and_verifies(self):
        a = self._make_archive()
        self._attach_envelope(a)
        before = {}
        for dirname in encrypt.ENCRYPT_DIRS:
            base = a.root / dirname
            if base.exists():
                for p in base.rglob("*"):
                    if p.is_file():
                        before[a.rel(p)] = p.read_bytes()

        rep = encrypt.lock(a, self.key, verbose=False)
        self.assertEqual(rep["files"], len(before))
        self.assertEqual(rep["verified"], rep["files"])

        # 明文必须消失，密文必须出现
        for rel in before:
            plain = a.root / rel
            self.assertFalse(plain.exists(), f"明文未删除: {rel}")
            self.assertTrue((a.root / (rel + encrypt.ENCRYPTED_SUFFIX)).exists(),
                            f"密文不存在: {rel}")

    def test_always_clear_files_stay_plain(self):
        """说明文件和授权目录永远明文——这是设计约束，不是遗漏。"""
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)

        self.assertTrue(a.doc_path.exists(), "00-说明.md 被加密了——严重设计错误")
        self.assertFalse((a.root / "00-说明.md.lfc").exists())
        for f in a.consent_dir.rglob("*"):
            if f.is_file():
                self.assertFalse(f.name.endswith(".lfc"),
                                 f"授权文件被加密了: {f}")
        self.assertTrue((a.root / vault.ENVELOPE_NAME).exists())
        self.assertTrue((a.root / "密钥信封.json").exists())

    def test_doc_and_consent_readable_after_lock(self):
        """锁定后，没有密钥也能知道这是什么、谁能用。"""
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        doc = a.doc_path.read_text(encoding="utf-8")
        self.assertIn("这是什么", doc)
        consent = (a.consent_dir / "授权声明.md").read_text(encoding="utf-8")
        self.assertIn("授权声明", consent)

    def test_unlock_restores_byte_identical(self):
        a = self._make_archive()
        self._attach_envelope(a)
        before = {}
        for dirname in encrypt.ENCRYPT_DIRS:
            base = a.root / dirname
            if base.exists():
                for p in base.rglob("*"):
                    if p.is_file():
                        before[a.rel(p)] = p.read_bytes()
        encrypt.lock(a, self.key, verbose=False)
        rep = encrypt.unlock(a, self.key, verbose=False)
        self.assertEqual(rep["failed"], 0)
        for rel, data in before.items():
            self.assertEqual((a.root / rel).read_bytes(), data, f"内容不符: {rel}")

    def test_unlock_keeps_ciphertext_by_default(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        encrypt.unlock(a, self.key, verbose=False)
        enc = list(a.root.rglob("*" + encrypt.ENCRYPTED_SUFFIX))
        self.assertTrue(enc, "默认不该删除密文")

    def test_unlock_can_remove_ciphertext(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        encrypt.unlock(a, self.key, verbose=False, remove_ciphertext=True)
        enc = [p for p in a.root.rglob("*" + encrypt.ENCRYPTED_SUFFIX)]
        self.assertFalse(enc, "指定 --remove-ciphertext 后密文应被删除")

    def test_wrong_key_cannot_unlock(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        with self.assertRaises(ValueError):
            encrypt.unlock(a, crypto.random_key(), verbose=False)

    def test_verify_passes_on_good_archive(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        rep = encrypt.verify(a, self.key, verbose=False)
        self.assertEqual(rep["failed"], 0)
        self.assertEqual(rep["missing"], 0)
        self.assertGreater(rep["ok"], 0)

    def test_verify_detects_tampered_file(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        # 从清单里挑一个密文文件改一个字节
        manifest = encrypt.read_manifest(a)
        entry = manifest["entries"][0]
        p = a.root / entry["enc_path"]
        data = bytearray(p.read_bytes())
        data[-1] ^= 0xFF
        p.write_bytes(bytes(data))
        rep = encrypt.verify(a, self.key, verbose=False)
        self.assertEqual(rep["failed"], 1)
        self.assertTrue(rep["problems"])

    def test_verify_detects_missing_ciphertext(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        manifest = encrypt.read_manifest(a)
        (a.root / manifest["entries"][0]["enc_path"]).unlink()
        rep = encrypt.verify(a, self.key, verbose=False)
        self.assertEqual(rep["missing"], 1)

    def test_lock_is_idempotent(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        rep2 = encrypt.lock(a, self.key, verbose=False)
        self.assertEqual(rep2["files"], 0)

    def test_status_without_key(self):
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        st = encrypt.status(a)
        self.assertTrue(st["locked"])
        self.assertGreater(st["encrypted_files"], 0)
        self.assertEqual(st["plain_files"], 0)
        self.assertIn("00-说明.md", st["always_clear"])
        self.assertEqual(st["key_fingerprint"], crypto.key_fingerprint(self.key))

    def test_unlock_then_search_still_works(self):
        """解密之后，整条既有链路必须还能用。"""
        from lifecollect import store
        a = self._make_archive()
        self._attach_envelope(a)
        encrypt.lock(a, self.key, verbose=False)
        encrypt.unlock(a, self.key, verbose=False)
        store.rebuild(a)
        rows = store.search(a, "成本")
        self.assertTrue(rows)


class TestEncryptionCli(unittest.TestCase):
    """加密命令的 CLI 端到端。口令走 --password-stdin。"""

    PW = "test-password-123"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "arch"
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "c.csv").write_text(
            "时间,发送者,内容\n"
            "2024-03-01 10:02:11,老王,我觉得这个方案没必要改，关键是成本\n",
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, stdin: str | None = None):
        import subprocess
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, "-m", "lifecollect", *[str(a) for a in args]],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env,
            input=stdin if stdin is not None else "",
            timeout=180)
        return r.returncode, r.stdout, r.stderr

    def _setup_locked(self):
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "standard", "--password-stdin", stdin=self.PW + "\n" + self.PW + "\n")
        self.assertEqual(code, 0, err)
        self.cli("import", "--root", self.root, "--source", "wechat",
                 "--self", "我", "--yes", self.src / "c.csv")
        code, out, err = self.cli("lock", "--root", self.root,
                                  "--password-stdin", stdin=self.PW + "\n")
        self.assertEqual(code, 0, err + out)
        return code, out, err

    def test_init_creates_envelope(self):
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "standard", "--password-stdin", stdin=self.PW + "\n" + self.PW + "\n")
        self.assertEqual(code, 0, err)
        self.assertIn("密钥指纹", out)
        self.assertTrue((self.root / vault.ENVELOPE_NAME).exists())

    def test_init_basic_has_no_envelope(self):
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "basic")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.root / vault.ENVELOPE_NAME).exists())
        self.assertIn("basic", out)

    def test_init_refuses_double_init(self):
        self.cli("init", "--root", self.root, "--security-level", "basic")
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "basic")
        self.assertEqual(code, 2)
        self.assertIn("不能重复初始化", err + out)

    def test_password_mismatch_on_init(self):
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "standard", "--password-stdin", stdin="password1\npassword2\n")
        self.assertEqual(code, 2)
        self.assertIn("不一致", err + out)

    def test_short_password_refused(self):
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "standard", "--password-stdin", stdin="short\n")
        self.assertEqual(code, 2)
        self.assertIn("太短", err + out)

    def test_lock_unlock_verify_status(self):
        self._setup_locked()
        # status 不需要口令
        code, out, err = self.cli("status", "--root", self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("锁定状态：是", out)
        self.assertIn("00-说明.md", out)

        # verify
        code, out, err = self.cli("verify", "--root", self.root,
                                  "--password-stdin", "--quiet",
                                  stdin=self.PW + "\n")
        self.assertEqual(code, 0, err)
        self.assertIn("坏 0", out)

        # unlock
        code, out, err = self.cli("unlock", "--root", self.root,
                                  "--password-stdin", stdin=self.PW + "\n")
        self.assertEqual(code, 0, err)

    def test_lock_with_basic_archive_fails_clearly(self):
        self.cli("init", "--root", self.root, "--security-level", "basic")
        code, out, err = self.cli("lock", "--root", self.root)
        self.assertEqual(code, 2)
        self.assertIn("没有密钥", err + out)

    def test_wrong_password_via_stdin_is_actually_rejected(self):
        """--password-stdin 必须**只走口令路径**，不能用 DPAPI 绕过。

        这个坑真的踩过：standard 档案在同一台机器上 DPAPI 能免密解开，
        于是 verify --password-stdin 传错口令也返回 0，所有自动化验证形同虚设。
        错误口令必须失败。
        """
        self._setup_locked()
        code, out, err = self.cli("verify", "--root", self.root,
                                  "--password-stdin", "--quiet",
                                  stdin="definitely-wrong-password\n")
        self.assertNotEqual(code, 0, "错误口令居然通过了——DPAPI 绕过了口令验证")
        self.assertIn("口令错误", err + out)

    def test_passwd_via_stdin(self):
        """换口令：stdin 依次给 当前口令 / 新口令 / 确认。"""
        self._setup_locked()
        new_pw = "brand-new-password-789"
        code, out, err = self.cli(
            "passwd", "--root", self.root, "--password-stdin",
            stdin=self.PW + "\n" + new_pw + "\n" + new_pw + "\n")
        self.assertEqual(code, 0, err)

        # 旧口令必须失效
        code, out, err = self.cli("verify", "--root", self.root,
                                  "--password-stdin", "--quiet",
                                  stdin=self.PW + "\n")
        self.assertNotEqual(code, 0, "旧口令换完之后居然还能用")

        # 新口令必须能用
        code, out, err = self.cli("verify", "--root", self.root,
                                  "--password-stdin", "--quiet",
                                  stdin=new_pw + "\n")
        self.assertEqual(code, 0, err + out)
        self.assertIn("坏 0", out)

    def test_noninteractive_init_without_stdin_fails_clearly(self):
        """非交互环境不设口令时必须明确报错，不能卡住。

        这个坑真的踩过：测试卡死过一次——getpass 一直在等终端。
        子进程必须显式传 stdin，否则继承父进程的终端就会阻塞。
        """
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "standard",
                                  "--password-stdin", stdin="")
        self.assertEqual(code, 2)
        self.assertIn("空口令", err + out)

    def test_doc_left_clear_after_lock(self):
        self._setup_locked()
        doc = (self.root / "00-说明.md").read_text(encoding="utf-8")
        self.assertIn("这是什么", doc)
        self.assertIn("当前安全级别", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
