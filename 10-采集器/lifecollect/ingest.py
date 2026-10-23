"""把各种来源的文件读成统一的 Conversation / Message。

第一版的适配器覆盖"用户手上已经有导出文件"的情况——这是绝大多数人的
真实处境。直接读微信数据库那条路（级别 1）不在这里，因为它依赖读进程
内存，会被杀毒软件拦，需要单独的模块和用户明确同意。

这个模块的全部职责：文件 → Message 列表。不写档案，不做标注。
"""

from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import Path

from . import records as R

# --- 编码探测 -----------------------------------------------------------
# 中文 Windows 上的现实：导出文件可能是 UTF-8、带 BOM 的 UTF-8、
# GB18030（兼容 GBK/GB2312）、或 UTF-16。UTF-8 优先，因为它是新工具的默认。
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16")


def read_text_any(path: Path) -> tuple[str, str, bool]:
    """返回 (文本, 使用的编码, 是否有替换字符)。

    宁可报告"编码不确定"，也不静默产生乱码。
    """
    raw = Path(path).read_bytes()
    if not raw:
        return "", "utf-8", False
    # BOM 优先
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16", False
        except UnicodeDecodeError:
            pass
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc, False
        except (UnicodeDecodeError, LookupError):
            continue
    text = raw.decode("utf-8", errors="replace")
    return text, "utf-8(replace)", True


# --- 列名候选（微信导出工具各不相同，所以做自动识别）--------------------
COL_TIME = ("时间", "日期", "time", "timestamp", "date", "datetime", "发送时间", "时刻")
COL_SENDER = ("发送者", "昵称", "用户名", "sender", "from", "name", "说话人", "发件人", "who")
COL_TEXT = ("内容", "消息", "文本", "text", "message", "content", "msg", "正文", "body")
COL_TYPE = ("类型", "type", "msgtype", "消息类型")


def _pick(header: list[str], candidates) -> int | None:
    lowered = [h.strip().lower() for h in header]
    for cand in candidates:
        c = cand.lower()
        for i, h in enumerate(lowered):
            if h == c:
                return i
    for cand in candidates:
        c = cand.lower()
        for i, h in enumerate(lowered):
            if c in h:
                return i
    return None


# --- 适配器基类 ---------------------------------------------------------

class Adapter:
    name = "base"
    extensions: tuple[str, ...] = ()

    def sniff(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def parse(self, path: Path, self_identifiers: set[str],
              default_source_level: int = 2) -> R.Conversation:
        raise NotImplementedError


# --- 纯文本 -------------------------------------------------------------

class TextAdapter(Adapter):
    name = "text"
    extensions = (".txt", ".log", ".md", ".text")

    def parse(self, path: Path, self_identifiers: set[str],
              default_source_level: int = 2) -> R.Conversation:
        text, enc, replaced = read_text_any(path)
        conv_id = path.stem
        msgs: list[R.Message] = []
        for line in text.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            msgs.append(R.Message(
                ts=R.parse_time_loose(stripped),
                sender="",
                direction=R.DIRECTION_UNKNOWN,
                text=R.normalize_whitespace(stripped),
                extra={"encoding": enc, "encoding_lossy": replaced},
            ))
        return R.Conversation(conv_id=conv_id, title=path.stem, messages=msgs)


# --- CSV / TSV ----------------------------------------------------------

class CsvAdapter(Adapter):
    name = "csv"
    extensions = (".csv", ".tsv")

    def parse(self, path: Path, self_identifiers: set[str],
              default_source_level: int = 2) -> R.Conversation:
        text, enc, replaced = read_text_any(path)
        delim = "\t" if path.suffix.lower() == ".tsv" else None
        if delim is None:
            # 嗅探分隔符：取前几行，看逗号/制表符/分号哪个更像
            head = "\n".join(text.split("\n")[:5])
            counts = {d: head.count(d) for d in (",", "\t", ";")}
            delim = max(counts, key=counts.get) if max(counts.values()) > 0 else ","

        reader = csv.reader(io.StringIO(text), delimiter=delim)
        rows = list(reader)
        if not rows:
            return R.Conversation(conv_id=path.stem, title=path.stem, messages=[])

        header = rows[0]
        i_time = _pick(header, COL_TIME)
        i_sender = _pick(header, COL_SENDER)
        i_text = _pick(header, COL_TEXT)

        if i_text is None:
            # 没有表头可识别 → 当作无表头，取最长列作为正文
            header = [f"c{i}" for i in range(len(rows[0]))]
            widths = [0] * len(header)
            for r in rows[:200]:
                for i, cell in enumerate(r):
                    if i < len(widths):
                        widths[i] = max(widths[i], len(cell or ""))
            i_text = widths.index(max(widths)) if widths else 0
            i_time = None
            i_sender = None
        else:
            rows = rows[1:]

        msgs: list[R.Message] = []
        for r in rows:
            if not r:
                continue

            def cell(idx):
                if idx is None or idx >= len(r):
                    return ""
                return (r[idx] or "").strip()

            sender = cell(i_sender)
            body = R.normalize_whitespace(cell(i_text))
            if not body and not sender:
                continue
            direction = R.DIRECTION_UNKNOWN
            if self_identifiers:
                direction = (R.DIRECTION_SELF if sender in self_identifiers
                             else R.DIRECTION_OTHER)
            msgs.append(R.Message(
                ts=R.parse_time_loose(cell(i_time)),
                sender=sender,
                direction=direction,
                text=body,
                extra={"encoding": enc, "encoding_lossy": replaced,
                       "media_hint": None},
            ))
        return R.Conversation(conv_id=path.stem, title=path.stem, messages=msgs)


# --- HTML（截图导出/网页导出常见）---------------------------------------

class _TableExtractor(HTMLParser):
    """把 HTML 里的文本按块提取。表格行用 | 连接，保留结构线索。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[str] = []
        self._cur: list[str] = []
        self._in_td = False
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip_depth += 1
        if tag == "tr":
            self._cur = []
        if tag in ("td", "th"):
            self._in_td = True

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip_depth:
            self._skip_depth -= 1
        if tag in ("td", "th"):
            self._in_td = False
        if tag == "tr":
            cells = [c.strip() for c in self._cur if c.strip()]
            if cells:
                self.rows.append(" | ".join(cells))
            self._cur = []
        if tag in ("p", "div", "br", "li"):
            self._cur.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        d = data.strip()
        if d:
            self._cur.append(d)


class HtmlAdapter(Adapter):
    name = "html"
    extensions = (".html", ".htm", ".xhtml")

    def parse(self, path: Path, self_identifiers: set[str],
              default_source_level: int = 2) -> R.Conversation:
        text, enc, replaced = read_text_any(path)
        parser = _TableExtractor()
        parser.feed(text)
        msgs: list[R.Message] = []
        for raw in parser.rows:
            line = R.normalize_whitespace(raw)
            if not line:
                continue
            # 表格行里常见 "时间 | 发送者 | 内容"
            parts = [p.strip() for p in line.split("|")]
            sender = ""
            direction = R.DIRECTION_UNKNOWN
            if len(parts) >= 2:
                # 找出第一个不等于时间的字段，姑且当发送者
                for p in parts[:-1]:
                    if p and R.parse_time_loose(p) is None:
                        sender = p
                        break
            if self_identifiers:
                direction = (R.DIRECTION_SELF if sender in self_identifiers
                             else R.DIRECTION_OTHER)
            msgs.append(R.Message(
                ts=R.parse_time_loose(line),
                sender=sender,
                direction=direction,
                text=line,
                extra={"encoding": enc, "encoding_lossy": replaced,
                       "extracted_from": "html"},
            ))
        return R.Conversation(conv_id=path.stem, title=path.stem, messages=msgs)


# --- JSON / JSONL -------------------------------------------------------

class JsonlAdapter(Adapter):
    name = "jsonl"
    extensions = (".jsonl", ".ndjson")

    def parse(self, path: Path, self_identifiers: set[str],
              default_source_level: int = 2) -> R.Conversation:
        text, enc, replaced = read_text_any(path)
        msgs: list[R.Message] = []
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            msgs.append(_obj_to_message(obj, self_identifiers, enc, replaced))
        return R.Conversation(conv_id=path.stem, title=path.stem,
                              messages=[m for m in msgs if m])


class JsonAdapter(Adapter):
    name = "json"
    extensions = (".json",)

    def parse(self, path: Path, self_identifiers: set[str],
              default_source_level: int = 2) -> R.Conversation:
        text, enc, replaced = read_text_any(path)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return R.Conversation(conv_id=path.stem, title=path.stem, messages=[])

        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("messages", "data", "records", "items", "list", "chat"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            if not items:
                items = [data]

        msgs = []
        for obj in items:
            if isinstance(obj, dict):
                m = _obj_to_message(obj, self_identifiers, enc, replaced)
                if m:
                    msgs.append(m)
        return R.Conversation(conv_id=path.stem, title=path.stem, messages=msgs)


def _obj_to_message(obj: dict, self_identifiers: set[str],
                    enc: str, replaced: bool) -> R.Message | None:
    def get(*keys):
        for k in keys:
            if k in obj and obj[k] not in (None, ""):
                return str(obj[k])
        return ""

    ts_raw = get("ts", "time", "timestamp", "date", "时间", "日期")
    sender = get("sender", "msg_sender", "from", "name", "昵称", "发送者",
                 "用户名", "talker", "speaker")
    body = get("text", "message", "msg", "content", "内容", "消息", "文本")
    if not body and not sender and not ts_raw:
        return None
    direction = R.DIRECTION_UNKNOWN
    if self_identifiers:
        direction = (R.DIRECTION_SELF if sender in self_identifiers
                     else R.DIRECTION_OTHER)
    ts = R.parse_time_loose(ts_raw) if ts_raw else None
    media = get("media", "media_hint", "type")
    media = media.lower() if media else None
    if media not in ("voice", "image", "video", "file"):
        media = None
    return R.Message(
        ts=ts,
        sender=sender,
        direction=direction,
        text=R.normalize_whitespace(body),
        media_hint=media,
        extra={"encoding": enc, "encoding_lossy": replaced},
    )


# --- 注册表 -------------------------------------------------------------

ADAPTERS: tuple[Adapter, ...] = (
    CsvAdapter(),
    JsonlAdapter(),
    JsonAdapter(),
    HtmlAdapter(),
    TextAdapter(),
)

SUPPORTED_EXTENSIONS = tuple(
    ext for a in ADAPTERS for ext in a.extensions
)


def pick_adapter(path: Path) -> Adapter | None:
    for a in ADAPTERS:
        if a.sniff(path):
            return a
    return None


def parse_file(path: Path, self_identifiers: set[str],
               default_source_level: int = 2) -> tuple[R.Conversation | None, str]:
    """返回 (会话, 说明)。说明用于向用户解释为什么没解析出来。"""
    path = Path(path)
    if not path.exists():
        return None, f"文件不存在: {path}"
    if path.is_dir():
        return None, f"这是一个目录，不是文件: {path}（第一版暂不支持目录递归）"
    adapter = pick_adapter(path)
    if adapter is None:
        return None, (f"没有能处理 {path.suffix} 的适配器。"
                      f"当前支持: {', '.join(SUPPORTED_EXTENSIONS)}")
    try:
        conv = adapter.parse(path, self_identifiers, default_source_level)
    except Exception as exc:  # 解析失败不能炸掉整批导入
        return None, f"解析失败({adapter.name}): {type(exc).__name__}: {exc}"
    return conv, f"适配器={adapter.name} 消息数={len(conv)}"


def iter_media_files(root: Path):
    """列出目录下的语音/图片/视频原始文件。第一版只登记，不解码。"""
    audio = {".silk", ".amr", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    image = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".heic"}
    video = {".mp4", ".mov", ".avi", ".mkv", ".wmv"}
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file():
            continue
        suf = p.suffix.lower()
        if suf in audio:
            yield p, "voice"
        elif suf in image:
            yield p, "image"
        elif suf in video:
            yield p, "video"
