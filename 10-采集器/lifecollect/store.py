"""索引层：SQLite + FTS5 全文检索。

核心约束：**这一层可以整个删掉重建，不影响档案完整性。**
权威数据在 10-原始/ 和 20-派生/records.jsonl，这里只是加速查询。

实现上做了两件事保证上面这句不是空话：
    1. 重建走临时库 + 原子替换，重建失败不会破坏已有索引。
    2. schema 里没有任何"只在数据库里"的字段——所有列都来自 records.jsonl。
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

from . import records as R
from .archive import Archive, read_jsonl

SCHEMA = """
CREATE TABLE conversations (
    conv_id     TEXT PRIMARY KEY,
    title       TEXT,
    source      TEXT,
    record_count INTEGER,
    first_ts    TEXT,
    last_ts     TEXT
);

CREATE TABLE records (
    rec_id          TEXT PRIMARY KEY,
    conv_id         TEXT NOT NULL,
    source          TEXT NOT NULL,
    source_level    INTEGER NOT NULL,
    captured_at     TEXT,
    imported_at     TEXT,
    kind            TEXT NOT NULL,
    tag_confidence  TEXT NOT NULL,
    tag_rule        TEXT,
    sender          TEXT,
    direction       TEXT,
    text            TEXT,
    media_hint      TEXT,
    sensitivity     TEXT,
    third_party     INTEGER NOT NULL DEFAULT 0,
    ai_drafted      INTEGER NOT NULL DEFAULT 0,
    sha256          TEXT,
    raw_path        TEXT,
    derived_path    TEXT
);

CREATE INDEX idx_records_conv  ON records(conv_id);
CREATE INDEX idx_records_kind  ON records(kind);
CREATE INDEX idx_records_time  ON records(captured_at);
CREATE INDEX idx_records_level ON records(source_level);
CREATE INDEX idx_records_dir   ON records(direction);

CREATE VIRTUAL TABLE records_fts USING fts5(
    rec_id UNINDEXED,
    text,
    char_index,
    tokenize='unicode61'
);
"""

# --- 中文检索为什么要这么绕 -----------------------------------------------
# 实测（SQLite 3.53.1）：
#   unicode61  → 把标点之间的整段当一个 token（"关键是成本"），
#                中文没有空格，所以整句变一个词，任何词都查不到。
#                separators 参数对 CJK 无效——CJK 在 Unicode 里属字母类。
#   trigram    → 只建 3 字滑窗（"是成本"），查询至少要 3 字，
#                中文双字词（"成本""方案"）全部查不到。
#
# 所以中文必须自己处理：额外存一份"字符级索引"——
# 把 "关键是成本" 存成 "关 键 是 成 本"，查询时同样拆成字符并用
# 短语匹配（连续 token 序列），这样 1 字的 "本" 和 2 字的 "成本"
# 都能正确命中，且不会误命中乱序字符。
# 代价：索引体积约等于文本体积的两倍（每个字符一个 token + 空格）。
# 这是索引层，可以随时重建，换体积换正确性是划算的。

_CJK_RANGES = (
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本区
    (0xF900, 0xFAFF),    # 兼容表意
    (0x3040, 0x30FF),    # 日文假名
    (0xAC00, 0xD7AF),    # 韩文音节
    (0x20000, 0x2FA1F),  # 扩展 B~
)


def _is_cjk_char(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def to_char_index(text: str) -> str:
    """把文本转成"字符级索引"字符串。

    只对 CJK 逐字拆开；拉丁字母和数字保持原样（否则英文单词会被拆碎）。

    两处归一化，都是实测踩出来的：
      1. **NFKC**：全角字母 'Ａ' 的 isalnum() 为 True，会走拉丁分支；
         若不在入口统一归一化，索引里留下的是全角小写 'ａ'，
         而用户敲的是半角 'A'，两边永远对不上。NFKC 把全角折成半角，
         且与 unicode61 自身的 NFKC 行为一致。
      2. **丢弃非打印字符**：\\x00 这类控制字符既不是 alnum 也不在
         CJK 范围内，不丢的话会被原样拼进 FTS5 查询串，把引号变成
         未闭合，直接抛 OperationalError。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    buf: list[str] = []

    def flush():
        if buf:
            # lower() 必须在收尾统一做，不能只对单个字符做——
            # 全角大写走的是同一条分支，漏掉就与查询侧不一致。
            out.append("".join(buf).lower())
            buf.clear()

    for ch in text:
        if not ch.isprintable():
            flush()
            continue
        if _is_cjk_char(ch):
            flush()
            out.append(ch)
        elif ch.isalnum():
            buf.append(ch)
        else:
            flush()
    flush()
    return " ".join(out)


def to_match_query(user_query: str) -> str:
    """把用户输入转成 FTS5 查询。

    一律加引号做字面短语匹配——用户输入里的 FTS5 语法字符
    （* " OR AND NOT NEAR - ^ 括号等）因此全部被中和，
    不会变成语法。本工具是"子串搜索"，不是查询语言。

    中文/混合输入走字符级短语匹配；纯拉丁输入按空格拆词。
    """
    q = unicodedata.normalize("NFKC", user_query or "").strip()
    if not q:
        return ""
    q = "".join(ch for ch in q if ch.isprintable())
    if not q.strip():
        return ""

    if any(_is_cjk_char(c) for c in q):
        parts = to_char_index(q).split()
        if not parts:
            return ""
        return '"' + " ".join(parts) + '"'

    # 纯拉丁：按空格拆词，各自加引号防止被当语法
    words = [w for w in q.split() if w]
    if not words:
        return ""
    return " ".join('"' + w.replace('"', '""') + '"' for w in words)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def rebuild(archive: Archive, quiet: bool = False) -> dict:
    """从 20-派生/ 重建索引。返回统计。"""
    records_path = archive.records_path
    convs_path = archive.conversations_path
    if not records_path.exists() and not convs_path.exists():
        raise FileNotFoundError(
            f"找不到派生记录: {records_path}\n"
            f"请先执行 import。索引层不能凭空生成——它只从派生层重建。"
        )

    archive.index_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(archive.index_dir), suffix=".tmpdb")
    os.close(fd)
    tmp_path = Path(tmp)

    n_rec = 0
    n_conv = 0
    try:
        conn = _connect(tmp_path)
        try:
            conn.executescript(SCHEMA)
            conn.commit()

            for conv in read_jsonl(convs_path):
                conn.execute(
                    "INSERT OR REPLACE INTO conversations"
                    " (conv_id, title, source, record_count, first_ts, last_ts)"
                    " VALUES (?,?,?,?,?,?)",
                    (conv.get("conv_id"), conv.get("title"), conv.get("source"),
                     conv.get("record_count"), conv.get("first_ts"),
                     conv.get("last_ts")),
                )
                n_conv += 1

            rows = []
            fts_rows = []
            for r in read_jsonl(records_path):
                rows.append((
                    r.get("record_id"), r.get("conv_id"), r.get("source"),
                    r.get("source_level", 2), r.get("captured_at"),
                    r.get("imported_at"), r.get("kind", R.KIND_UNKNOWN),
                    r.get("tag_confidence", R.CONFIDENCE_WEAK),
                    r.get("tag_rule"), r.get("sender"), r.get("direction"),
                    r.get("text"), r.get("media_hint"), r.get("sensitivity"),
                    1 if r.get("third_party_present") else 0,
                    1 if r.get("ai_drafted") else 0,
                    r.get("sha256"), r.get("raw_path"), r.get("derived_path"),
                ))
                text = r.get("text") or ""
                fts_rows.append((
                    r.get("record_id"), text, to_char_index(text),
                ))
                if len(rows) >= 2000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO records VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                    conn.executemany(
                        "INSERT INTO records_fts(rec_id, text, char_index)"
                        " VALUES (?,?,?)", fts_rows)
                    n_rec += len(rows)
                    rows.clear()
                    fts_rows.clear()
            if rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO records VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                conn.executemany(
                    "INSERT INTO records_fts(rec_id, text, char_index)"
                    " VALUES (?,?,?)", fts_rows)
                n_rec += len(rows)

            conn.commit()
        finally:
            conn.close()

        # 原子替换：重建失败时旧索引保持可用
        target = archive.db_path
        for suffix in ("-wal", "-shm"):
            stale = Path(str(target) + suffix)
            if stale.exists():
                stale.unlink()
        os.replace(tmp_path, target)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise

    return {"records": n_rec, "conversations": n_conv}


def open_index(archive: Archive) -> sqlite3.Connection:
    if not archive.db_path.exists():
        raise FileNotFoundError(
            f"索引不存在: {archive.db_path}\n请先执行 index。")
    return _connect(archive.db_path)


def search(archive: Archive, query: str, kind: str | None = None,
           source: str | None = None, limit: int = 50,
           self_only: bool = False) -> list[dict]:
    conn = open_index(archive)
    try:
        match = to_match_query(query)
        if not match:
            return []
        # 防御性钳制：limit 为 None / 负数 / 非整数都不该让查询炸掉
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 10000))
        sql = """
            SELECT r.rec_id   AS record_id,
                   r.conv_id  AS conv_id,
                   r.source, r.source_level, r.captured_at, r.imported_at,
                   r.kind, r.tag_confidence, r.tag_rule, r.sender, r.direction,
                   r.text, r.media_hint, r.sensitivity, r.third_party,
                   r.sha256, r.raw_path, r.derived_path,
                   bm25(records_fts) AS rank
            FROM records_fts f
            JOIN records r ON r.rec_id = f.rec_id
            WHERE records_fts MATCH ?
        """
        params: list = [match]
        if kind:
            sql += " AND r.kind = ?"
            params.append(kind)
        if source:
            sql += " AND r.source = ?"
            params.append(source)
        if self_only:
            sql += " AND r.direction = 'self'"
        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            cur = conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            # 走到这里说明查询构造漏了某类输入——不能装作是"没有结果"，
            # 那会让用户以为数据里真的没有。
            raise ValueError(
                f"检索失败（查询={match!r}）: {exc}\n"
                f"这是采集器的 bug，不是你的输入问题，请记录原始输入后反馈。"
            ) from exc
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def stats(archive: Archive) -> dict:
    conn = open_index(archive)
    try:
        out: dict = {}
        out["total"] = conn.execute(
            "SELECT COUNT(*) FROM records").fetchone()[0]
        out["conversations"] = conn.execute(
            "SELECT COUNT(*) FROM conversations").fetchone()[0]

        out["by_kind"] = {
            row["kind"]: row["n"] for row in conn.execute(
                "SELECT kind, COUNT(*) AS n FROM records GROUP BY kind"
                " ORDER BY n DESC")
        }
        out["by_source"] = {
            row["source"]: row["n"] for row in conn.execute(
                "SELECT source, COUNT(*) AS n FROM records GROUP BY source"
                " ORDER BY n DESC")
        }
        out["by_level"] = {
            row["source_level"]: row["n"] for row in conn.execute(
                "SELECT source_level, COUNT(*) AS n FROM records"
                " GROUP BY source_level ORDER BY source_level")
        }
        out["by_direction"] = {
            row["direction"]: row["n"] for row in conn.execute(
                "SELECT direction, COUNT(*) AS n FROM records"
                " GROUP BY direction ORDER BY n DESC")
        }
        out["by_confidence"] = {
            row["tag_confidence"]: row["n"] for row in conn.execute(
                "SELECT tag_confidence, COUNT(*) AS n FROM records"
                " GROUP BY tag_confidence ORDER BY n DESC")
        }
        row = conn.execute(
            "SELECT MIN(captured_at) AS a, MAX(captured_at) AS b"
            " FROM records WHERE captured_at IS NOT NULL").fetchone()
        out["time_range"] = [row["a"], row["b"]]
        out["no_timestamp"] = conn.execute(
            "SELECT COUNT(*) FROM records WHERE captured_at IS NULL"
        ).fetchone()[0]
        out["third_party_present"] = conn.execute(
            "SELECT COUNT(*) FROM records WHERE third_party = 1").fetchone()[0]
        out["ai_drafted_records"] = conn.execute(
            "SELECT COUNT(*) FROM records WHERE ai_drafted = 1").fetchone()[0]
        out["self_messages"] = conn.execute(
            "SELECT COUNT(*) FROM records WHERE direction = 'self'"
        ).fetchone()[0]
        return out
    finally:
        conn.close()


# --- 训练价值评估 -------------------------------------------------------
# 标准 §3.2 的 kind 决定材料对下游的价值。这里给出一个**诚实的**估算：
# 它说的是"字符量够不够"，不是"训出来像不像"。

MIN_USEFUL_CHARS = 10000  # DLRS COLLECTION_STANDARD 对文本语料的最低要求


def training_readiness(archive: Archive) -> dict:
    conn = open_index(archive)
    try:
        rows = conn.execute(
            "SELECT kind, SUM(LENGTH(COALESCE(text,''))) AS chars,"
            "       COUNT(*) AS n"
            " FROM records GROUP BY kind").fetchall()
        by_kind = {r["kind"]: {"chars": r["chars"] or 0, "n": r["n"]}
                   for r in rows}

        self_chars = conn.execute(
            "SELECT SUM(LENGTH(COALESCE(text,''))) FROM records"
            " WHERE direction = 'self'").fetchone()[0] or 0

        usable_kinds = (R.KIND_AUTHORED, R.KIND_CHAT_OPINION,
                        R.KIND_CHAT_EMOTION)
        usable = sum(by_kind.get(k, {}).get("chars", 0) for k in usable_kinds)
        raw = sum(v["chars"] for v in by_kind.values())
        return {
            "total_chars": raw,
            "usable_chars": usable,
            "self_directed_chars": self_chars,
            "by_kind": by_kind,
            "threshold": MIN_USEFUL_CHARS,
            "meets_threshold": usable >= MIN_USEFUL_CHARS,
            "note": (
                "这个数字只说明'字符量够不够'，不说明'训出来像不像'。"
                "文本语料的最低要求取自 DLRS COLLECTION_STANDARD。"
            ),
        }
    finally:
        conn.close()
