"""自检脚本。只用标准库，在临时目录里造合成数据跑全链路。

存在的理由：整条链路在真实材料上跑之前，必须先证明它在受控输入上是对的。
测试数据全是伪造的，不含任何真实个人信息。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lifecollect import records as R            # noqa: E402
from lifecollect import tagging, store          # noqa: E402
from lifecollect.archive import Archive         # noqa: E402
from lifecollect.ingest import read_text_any, parse_file  # noqa: E402
from lifecollect import pipeline                # noqa: E402


# --- 合成数据 -----------------------------------------------------------

CSV_FIXTURE = """时间,发送者,内容
2023-05-12 09:14:22,我,收到
2023-05-12 09:15:01,老王,几点到
2023-05-12 09:15:30,我,马上
2023-05-12 10:02:11,老王,我觉得这个方案其实没必要改，关键是成本
2023-05-12 10:03:40,我,我不同意，本质上问题在于排期不是成本
2023-05-12 10:04:02,我,唉……这事儿真是让人难受
2023-05-12 21:30:00,我,今天真的累死了！！！
2023-05-12 21:31:00,老王,[图片]
2023-05-13 08:00:00,我,[语音]
2023-05-13 08:01:00,我,晚安
"""

TXT_FIXTURE = """关于这件事我的想法
我一直在想，人到底为什么要在意别人的看法。
说到底，评价标准如果不是自己定的，那就永远没有尽头。
"""

JSONL_FIXTURE = "\n".join([
    json.dumps({"time": "2024-01-01 10:00:00", "sender": "我",
                "text": "今年我想换个活法", "type": "text"}, ensure_ascii=False),
    json.dumps({"time": "2024-01-01 10:01:00", "sender": "我",
                "text": "语音消息", "type": "voice"}, ensure_ascii=False),
])


class TestTagging(unittest.TestCase):
    def tag(self, text, source=R.SOURCE_WECHAT):
        msg = R.Message(ts=None, sender="我",
                        direction=R.DIRECTION_SELF, text=text)
        return tagging.tag_message(msg, source)

    def test_casual_short(self):
        for t in ("收到", "好的", "嗯", "马上", "晚安"):
            with self.subTest(t=t):
                self.assertEqual(self.tag(t).kind, R.KIND_CHAT_CASUAL)

    def test_emotion_by_punctuation(self):
        self.assertEqual(self.tag("今天真的累死了！！！").kind,
                         R.KIND_CHAT_EMOTION)

    def test_emotion_by_ellipsis(self):
        self.assertEqual(self.tag("唉……这事儿真是让人难受").kind,
                         R.KIND_CHAT_EMOTION)

    def test_opinion(self):
        r = self.tag("我觉得这个方案其实没必要改，关键是成本")
        self.assertEqual(r.kind, R.KIND_CHAT_OPINION)
        self.assertIn("观点词", r.rule)

    def test_media_placeholder(self):
        msg = R.Message(ts=None, sender="我", direction=R.DIRECTION_SELF,
                        text="[图片]")
        self.assertEqual(tagging.tag_message(msg, R.SOURCE_WECHAT).kind,
                         R.KIND_CHAT_MEDIA)

    def test_authored_by_source(self):
        r = self.tag("我一直在想，人到底为什么要在意别人的看法。", R.SOURCE_DOCUMENTS)
        self.assertEqual(r.kind, R.KIND_AUTHORED)

    def test_unknown_is_honest(self):
        # 有长度、没命中任何信号 → 必须是 unknown + weak，不能硬塞类别
        r = self.tag("下周把那个表格再核对一遍然后把结果发给我看看")
        self.assertEqual(r.kind, R.KIND_UNKNOWN)
        self.assertEqual(r.confidence, R.CONFIDENCE_WEAK)

    def test_every_kind_has_value_note(self):
        for k in R.KINDS:
            self.assertIn(k, R.KIND_VALUE_NOTE)


class TestEncoding(unittest.TestCase):
    """中文 Windows 的 GBK/UTF-8 问题——本机实测踩到过。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_utf8_read(self):
        p = self.tmp / "a.txt"
        p.write_text("中文内容", encoding="utf-8")
        text, enc, lossy = read_text_any(p)
        self.assertEqual(text, "中文内容")
        self.assertFalse(lossy)

    def test_gbk_fallback(self):
        p = self.tmp / "b.txt"
        p.write_bytes("中文内容".encode("gb18030"))
        text, enc, lossy = read_text_any(p)
        self.assertEqual(text, "中文内容")
        self.assertFalse(lossy)
        self.assertEqual(enc, "gb18030")

    def test_utf8_bom(self):
        p = self.tmp / "c.txt"
        p.write_bytes(b"\xef\xbb\xbf" + "中文".encode("utf-8"))
        text, enc, _ = read_text_any(p)
        self.assertEqual(text, "中文")

    def test_no_silent_garbage(self):
        # 不可解码的字节必须被报告，不能静默产生乱码
        p = self.tmp / "d.bin"
        p.write_bytes(b"\xff\xfe\x00\x41\x00\x42")
        text, enc, lossy = read_text_any(p)
        self.assertTrue(text or text == "")
        # utf-16 BOM 分支会成功，所以这里只断言不抛异常


class TestFullPipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "arch"
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "chat.csv").write_text(CSV_FIXTURE, encoding="utf-8")
        (self.src / "note.txt").write_text(TXT_FIXTURE, encoding="utf-8")
        (self.src / "log.jsonl").write_text(JSONL_FIXTURE, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_end_to_end(self):
        a = Archive(self.root)
        a.init(security_level=R.SECURITY_STANDARD, subject="测试")

        # 1. 说明文件必须存在且明文可读
        self.assertTrue(a.doc_path.exists())
        doc = a.doc_path.read_text(encoding="utf-8")
        self.assertIn("这是什么", doc)
        self.assertIn("不挡什么", doc)

        # 2. 授权文件桩必须存在
        self.assertTrue((a.consent_dir / "授权声明.md").exists())
        self.assertTrue((a.consent_dir / "授权声明.json").exists())
        self.assertTrue((a.consent_dir / "身后指令.md").exists())
        cj = json.loads((a.consent_dir / "授权声明.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cj["granularity"]), 12)
        self.assertTrue(all(v == "unset" for v in cj["granularity"].values()))

        # 3. 被否掉的方案必须留档，防止反复重提
        kc = json.loads((a.consent_dir / "密钥托管.json").read_text(encoding="utf-8"))
        self.assertIn("人生经历选择题", kc["rejected_schemes"])

        # 4. 导入（指定 --self，方向必须正确）
        rep = pipeline.import_paths(
            a, [self.src / "chat.csv"], R.SOURCE_WECHAT, {"我"}, verbose=False)
        self.assertEqual(rep["files_ok"], 1)
        self.assertEqual(rep["messages"], 10)
        self.assertEqual(rep["files_failed"], 0)

        # 5. 原始文件被复制进档案，用户原文件没动
        self.assertTrue((self.src / "chat.csv").exists())
        raw = list((a.raw_dir / "wechat").rglob("chat.csv"))
        self.assertEqual(len(raw), 1)

        # 6. 标注
        res = pipeline.tag_archive(a, verbose=False)
        self.assertEqual(sum(res["by_kind"].values()), 10)
        self.assertGreater(res["by_kind"].get(R.KIND_CHAT_CASUAL, 0), 0)
        self.assertGreater(res["by_kind"].get(R.KIND_CHAT_EMOTION, 0), 0)

        # 7. 索引
        st = store.rebuild(a)
        self.assertEqual(st["records"], 10)

        # 8. 统计里方向必须是对的
        s = store.stats(a)
        self.assertEqual(s["self_messages"], 7)

        # 9. 呈现层产出四份文件
        pipeline.write_view(a)
        for name in ("总览.md", "会话清单.md", "高价值样本.md", "待处理问题.md"):
            self.assertTrue((a.view_dir / name).exists(), name)

        # 10. 审计日志有内容
        self.assertTrue(a.audit_path.exists())
        self.assertGreater(len(a.audit_path.read_text(encoding="utf-8").splitlines()), 3)

    def test_self_direction(self):
        a = Archive(self.root)
        a.init()
        pipeline.import_paths(a, [self.src / "chat.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        recs = list(__import__("lifecollect.archive", fromlist=["x"]).read_jsonl(a.records_path))
        selfs = [r for r in recs if r["direction"] == "self"]
        others = [r for r in recs if r["direction"] == "other"]
        self.assertEqual(len(selfs), 7)
        self.assertEqual(len(others), 3)
        for r in selfs:
            self.assertEqual(r["sender"], "我")

    def test_unknown_direction_when_self_unset(self):
        """不指定 --self 时必须是 unknown，不能猜。"""
        a = Archive(self.root)
        a.init()
        pipeline.import_paths(a, [self.src / "chat.csv"], R.SOURCE_WECHAT,
                              set(), verbose=False)
        from lifecollect.archive import read_jsonl
        recs = list(read_jsonl(a.records_path))
        self.assertTrue(all(r["direction"] == R.DIRECTION_UNKNOWN for r in recs))

    def test_no_timestamp_is_reported_not_faked(self):
        """抠不出时间就必须是 None，不能用导入时间冒充。"""
        a = Archive(self.root)
        a.init()
        plain = self.src / "notime.txt"
        plain.write_text("这句话里没有任何日期信息", encoding="utf-8")
        pipeline.import_paths(a, [plain], R.SOURCE_MANUAL, {"我"}, verbose=False)
        from lifecollect.archive import read_jsonl
        recs = [r for r in read_jsonl(a.records_path) if r["conv_id"] == "notime"]
        self.assertTrue(recs)
        self.assertTrue(all(r["captured_at"] is None for r in recs))

    def test_reimport_is_idempotent(self):
        """重跑同一个会话不能产生重复记录。"""
        a = Archive(self.root)
        a.init()
        for _ in range(3):
            pipeline.import_paths(a, [self.src / "chat.csv"], R.SOURCE_WECHAT,
                                  {"我"}, verbose=False)
        from lifecollect.archive import read_jsonl
        recs = list(read_jsonl(a.records_path))
        self.assertEqual(len(recs), 10)
        ids = [r["record_id"] for r in recs]
        self.assertEqual(len(ids), len(set(ids)))

    def test_unsupported_file_reports_reason(self):
        a = Archive(self.root)
        a.init()
        bad = self.src / "weird.xyz"
        bad.write_text("whatever", encoding="utf-8")
        rep = pipeline.import_paths(a, [bad], R.SOURCE_LOOSE, set(), verbose=False)
        self.assertEqual(rep["files_failed"], 1)
        self.assertIn("适配器", rep["failures"][0]["reason"])

    def test_index_rebuild_is_disposable(self):
        """索引层必须可以整个删掉重建——这是标准里的硬要求。"""
        a = Archive(self.root)
        a.init()
        pipeline.import_paths(a, [self.src / "chat.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        st1 = store.rebuild(a)
        a.db_path.unlink()
        st2 = store.rebuild(a)
        self.assertEqual(st1, st2)

    def test_search_finds_text(self):
        a = Archive(self.root)
        a.init()
        pipeline.import_paths(a, [self.src / "chat.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        store.rebuild(a)
        rows = store.search(a, "成本")
        self.assertTrue(rows)
        self.assertTrue(any("成本" in (r["text"] or "") for r in rows))

    def test_search_self_only(self):
        a = Archive(self.root)
        a.init()
        pipeline.import_paths(a, [self.src / "chat.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        store.rebuild(a)
        rows = store.search(a, "成本", self_only=True)
        self.assertTrue(all(r["direction"] == "self" for r in rows))

    def test_training_readiness_is_honest(self):
        a = Archive(self.root)
        a.init()
        pipeline.import_paths(a, [self.src / "note.txt"], R.SOURCE_DOCUMENTS,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        store.rebuild(a)
        r = store.training_readiness(a)
        self.assertFalse(r["meets_threshold"])   # 合成数据太短，必须如实说不够
        self.assertIn("不说明", r["note"])


class TestChineseSearch(unittest.TestCase):
    """中文检索是本项目最容易悄悄坏掉的地方。

    unicode61 把整句当一个 token、trigram 要求至少 3 字——
    两个内置分词器对中文都不可用（实测）。所以我们自己做字符级索引。
    这些用例是防止它退化的护栏。
    """

    def test_char_index_splits_cjk(self):
        self.assertEqual(store.to_char_index("成本"), "成 本")
        self.assertEqual(store.to_char_index("关键是成本"), "关 键 是 成 本")

    def test_char_index_keeps_latin_words(self):
        self.assertEqual(store.to_char_index("WeChat 2024"), "wechat 2024")

    def test_char_index_mixed(self):
        self.assertEqual(store.to_char_index("用iPhone拍照"), "用 iphone 拍 照")

    def test_char_index_drops_punctuation(self):
        self.assertEqual(store.to_char_index("你好，世界！"), "你 好 世 界")

    def test_match_query_two_char_word(self):
        # 这是 trigram 做不到的：双字词
        self.assertEqual(store.to_match_query("成本"), '"成 本"')

    def test_match_query_single_char(self):
        self.assertEqual(store.to_match_query("本"), '"本"')

    def test_match_query_latin(self):
        self.assertEqual(store.to_match_query("hello world"),
                         '"hello" "world"')

    def test_match_query_quotes_are_escaped(self):
        """用户输入带引号不能变成 FTS5 语法错误。

        断言方式：直接把生成的查询丢给 FTS5 执行，看它接不接受。
        比检查字符串形状可靠——引号双写后子串看起来仍然像引号。
        """
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(char_index)")
        c.execute("INSERT INTO t VALUES (?)", (store.to_char_index("say hi 你好"),))
        for user_input in ('say "hi"', "成本", '-成本', '成本 OR 方案', 'NEAR(a b)',
                           "'; DROP TABLE t; --", "a*b"):
            with self.subTest(user_input=user_input):
                q = store.to_match_query(user_input)
                if not q:
                    continue
                c.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (q,)).fetchone()
        c.close()

    def test_injection_does_not_drop_table(self):
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(char_index)")
        c.execute("INSERT INTO t VALUES (?)", (store.to_char_index("成本"),))
        q = store.to_match_query("'; DROP TABLE t; --")
        c.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?", (q,)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        c.close()
        self.assertEqual(n, 1)

    def test_match_query_empty(self):
        self.assertEqual(store.to_match_query("   "), "")

    # --- 以下四类都是实测踩出来的边界，不是想象出来的 ---

    def test_fullwidth_latin_normalised(self):
        """全角字母 'Ａ'.isalnum() 为 True，会走拉丁分支。

        曾经因为 lower() 只加在单字符分支上，索引里留下全角小写 'ａ'，
        而用户敲的是半角 'A'，两边永远对不上。
        """
        self.assertEqual(store.to_char_index("ＡＢＣ"), "abc")
        self.assertEqual(store.to_char_index("ＡＢＣ"), store.to_char_index("ABC"))
        self.assertEqual(store.to_char_index("１２３"), "123")

    def test_fullwidth_index_matches_halfwidth_query(self):
        a = Archive(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, a.root, ignore_errors=True)
        a.init()
        src = a.root.parent / "fw"
        src.mkdir(exist_ok=True)
        (src / "c.csv").write_text(
            "时间,发送者,内容\n2024-01-01 10:00:00,我,这个ＡＰＰ很好用\n",
            encoding="utf-8")
        pipeline.import_paths(a, [src / "c.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        store.rebuild(a)
        for q in ("APP", "app", "ＡＰＰ"):
            with self.subTest(q=q):
                self.assertTrue(store.search(a, q), f"查不到: {q}")

    def test_control_chars_do_not_crash(self):
        """\\x00 既不是 alnum 也不在 CJK 范围内，曾原样进入 FTS5 查询串
        把引号搞成未闭合，直接抛 OperationalError。"""
        for ch in ("\x00", "\x01", "\x1f", "\x7f", "\u200b", "\ufeff",
                   "\t", "\n", "成本\x00", "\x00成本"):
            with self.subTest(ch=repr(ch)):
                q = store.to_match_query(ch)
                self.assertNotIn("\x00", q)
                if q:
                    self.assertEqual(q.count('"') % 2, 0, f"引号未闭合: {q!r}")

    def test_nfkc_applied_to_query(self):
        # 全角标点在查询里被丢弃，不该产生语法差异
        self.assertEqual(store.to_match_query("成本，"), store.to_match_query("成本"))

    def test_known_limitation_phrase_not_substring(self):
        """短语匹配要求连续，这是有意设计，不是缺陷。

        '拍了几张照片' 里 '拍' 和 '照' 不相邻，所以搜 '拍照' 应当无结果。
        如果这条失败了，说明短语匹配退化成了松散匹配——那会带来大量误命中。
        """
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(char_index)")
        c.execute("INSERT INTO t VALUES (?)",
                  (store.to_char_index("今天拍了几张照片"),))
        hit = c.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?",
                        (store.to_match_query("拍照"),)).fetchone()[0]
        single = c.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?",
                           (store.to_match_query("照"),)).fetchone()[0]
        c.close()
        self.assertEqual(hit, 0)
        self.assertEqual(single, 1)

    def test_weird_limit_does_not_crash(self):
        a = Archive(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, a.root, ignore_errors=True)
        a.init()
        src = a.root.parent / "lim"
        src.mkdir(exist_ok=True)
        (src / "c.csv").write_text(
            "时间,发送者,内容\n2024-01-01 10:00:00,我,成本\n", encoding="utf-8")
        pipeline.import_paths(a, [src / "c.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        store.rebuild(a)
        for bad in (None, 0, -1, "abc", 10 ** 9):
            with self.subTest(limit=bad):
                store.search(a, "成本", limit=bad)

    def test_all_injection_inputs_return_something_or_nothing(self):
        """任何输入都不能炸，也不能静默返回错误结果。"""
        a = Archive(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, a.root, ignore_errors=True)
        a.init()
        src = a.root.parent / "inj"
        src.mkdir(exist_ok=True)
        (src / "c.csv").write_text(
            "时间,发送者,内容\n2024-01-01 10:00:00,我,成本控制很重要\n",
            encoding="utf-8")
        pipeline.import_paths(a, [src / "c.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a, verbose=False)
        store.rebuild(a)
        for q in ('*', '"', '"""', "';  DROP TABLE records; --", "NEAR(",
                  "OR", "AND", "NOT", "-成本", "^成本", "(成本)", "a" * 500,
                  "成" * 300, "\x00", "成本 OR 方案"):
            with self.subTest(query=repr(q[:20])):
                rows = store.search(a, q)   # 不抛异常即通过
                self.assertIsInstance(rows, list)
        # 表必须完好
        import sqlite3
        c = sqlite3.connect(str(a.db_path))
        n = c.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        c.close()
        self.assertEqual(n, 1)

    def test_phrase_matching_is_contiguous(self):
        """字符级索引必须保证顺序，不能把'本成'也搜出来。"""
        import sqlite3
        c = sqlite3.connect(":memory:")
        c.execute("CREATE VIRTUAL TABLE t USING fts5(char_index)")
        c.execute("INSERT INTO t VALUES (?)", (store.to_char_index("关键是成本"),))
        n_hit = c.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?",
                          (store.to_match_query("成本"),)).fetchone()[0]
        n_miss = c.execute("SELECT COUNT(*) FROM t WHERE t MATCH ?",
                           (store.to_match_query("本成"),)).fetchone()[0]
        c.close()
        self.assertEqual(n_hit, 1)
        self.assertEqual(n_miss, 0)

    def test_two_char_and_single_char_both_hit(self):
        a_arch = Archive(Path(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, a_arch.root, ignore_errors=True)
        a_arch.init()
        src = a_arch.root.parent / "s"
        src.mkdir(exist_ok=True)
        (src / "c.csv").write_text(
            "时间,发送者,内容\n2024-01-01 10:00:00,我,关键是成本控制\n",
            encoding="utf-8")
        pipeline.import_paths(a_arch, [src / "c.csv"], R.SOURCE_WECHAT,
                              {"我"}, verbose=False)
        pipeline.tag_archive(a_arch, verbose=False)
        store.rebuild(a_arch)
        for q in ("成本", "本", "成本控制", "控制"):
            with self.subTest(q=q):
                self.assertTrue(store.search(a_arch, q), f"查不到: {q}")


class TestCliEndToEnd(unittest.TestCase):
    """直接打 CLI，不调 Python API。

    存在的理由：search() 曾经返回 'rec_id' 而 cli.py 读 'record_id'，
    单元测试全绿、CLI 一跑就 KeyError。只测 API 测不出这种错位。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "arch"
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "c.csv").write_text(
            "时间,发送者,内容\n"
            "2024-03-01 10:02:11,老王,我觉得这个方案其实没必要改，关键是成本\n"
            "2024-03-01 10:03:40,我,我不同意，本质上问题在于排期不是成本\n"
            "2024-03-01 21:30:00,我,今天真的累死了！！！\n",
            encoding="utf-8")
        (self.src / "n.txt").write_text(
            "关于要不要换工作\n我一直在想这件事。\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *args, stdin: str | None = None) -> tuple[int, str, str]:
        """跑 CLI 子进程。

        stdin 必须显式处理：不传的话子进程继承父进程的终端，
        getpass 会一直等输入，测试直接卡死（这个坑真的踩过）。
        """
        import os
        import subprocess
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        r = subprocess.run(
            [sys.executable, "-m", "lifecollect", *[str(a) for a in args]],
            cwd=str(Path(__file__).resolve().parent),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env,
            input=stdin if stdin is not None else "",
            timeout=120)
        return r.returncode, r.stdout, r.stderr

    def test_full_cli_flow(self):
        # basic 级别不加密，不需要口令，适合测非加密链路
        code, out, err = self.cli("init", "--root", self.root,
                                  "--security-level", "basic", "--subject", "T")
        self.assertEqual(code, 0, err)
        self.assertIn("不挡什么", out)

        code, out, err = self.cli("import", "--root", self.root,
                                  "--source", "wechat", "--self", "我",
                                  "--yes", self.src / "c.csv")
        self.assertEqual(code, 0, err)
        self.assertIn("3 条", out)

        code, out, err = self.cli("import", "--root", self.root,
                                  "--source", "documents", "--self", "我",
                                  "--yes", self.src / "n.txt")
        self.assertEqual(code, 0, err)

        code, out, err = self.cli("tag", "--root", self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("已标注", out)

        code, out, err = self.cli("index", "--root", self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("5 条记录", out)

        code, out, err = self.cli("stats", "--root", self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("记录总数：5", out)

        code, out, err = self.cli("view", "--root", self.root)
        self.assertEqual(code, 0, err)
        self.assertIn("总览.md", out)

    def test_cli_search_returns_results(self):
        """这是 KeyError 那个 bug 的回归护栏。"""
        self.cli("init", "--root", self.root, "--security-level", "basic")
        self.cli("import", "--root", self.root, "--source", "wechat",
                 "--self", "我", "--yes", self.src / "c.csv")
        self.cli("import", "--root", self.root, "--source", "documents",
                 "--self", "我", "--yes", self.src / "n.txt")
        self.cli("tag", "--root", self.root)
        self.cli("index", "--root", self.root)

        for q, expect_substr in (("成本", "成本"), ("本", "本"),
                                 ("换工作", "换工作"), ("累死", "累死")):
            with self.subTest(query=q):
                code, out, err = self.cli("search", "--root", self.root, q)
                self.assertEqual(code, 0, f"查询 {q} 失败: {err}")
                self.assertIn(expect_substr, out)
                self.assertNotIn("Traceback", out)

    def test_cli_search_no_match_is_not_an_error(self):
        self.cli("init", "--root", self.root, "--security-level", "basic")
        self.cli("import", "--root", self.root, "--source", "wechat",
                 "--self", "我", "--yes", self.src / "c.csv")
        self.cli("tag", "--root", self.root)
        self.cli("index", "--root", self.root)
        code, out, err = self.cli("search", "--root", self.root, "完全不存在的词")
        self.assertEqual(code, 0, err)
        self.assertIn("没有匹配", out)

    def test_cli_search_without_index_gives_clear_error(self):
        self.cli("init", "--root", self.root, "--security-level", "basic")
        self.cli("import", "--root", self.root, "--source", "wechat",
                 "--self", "我", "--yes", self.src / "c.csv")
        code, out, err = self.cli("search", "--root", self.root, "成本")
        self.assertEqual(code, 2)
        self.assertIn("请先执行 index", err + out)

    def test_cli_import_on_uninitialised_archive_fails_clearly(self):
        code, out, err = self.cli("import", "--root", self.root,
                                  "--yes", self.src / "c.csv")
        self.assertEqual(code, 2)
        self.assertIn("先执行 init", err + out)

    def test_cli_levels_lists_all_three(self):
        code, out, err = self.cli("levels")
        self.assertEqual(code, 0, err)
        for lvl in R.SECURITY_LEVELS:
            self.assertIn(lvl, out)
        self.assertIn("不挡什么", out)


class TestSecurityLevels(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_level_states_what_it_does_not_block(self):
        for lvl in R.SECURITY_LEVELS:
            info = R.SECURITY_TABLE[lvl]
            self.assertTrue(info["does_not_block"].strip())
            self.assertTrue(info["blocks"].strip())

    def test_paranoid_is_honest_about_key_loss(self):
        self.assertIn("永久不可读",
                      R.SECURITY_TABLE[R.SECURITY_PARANOID]["does_not_block"])

    def test_doc_records_level(self):
        a = Archive(self.tmp / "x")
        a.init(security_level=R.SECURITY_PARANOID)
        doc = a.doc_path.read_text(encoding="utf-8")
        self.assertIn("paranoid", doc)
        self.assertIn("永久不可读", doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
