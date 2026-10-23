"""档案记录的数据结构与取值定义。

这个模块只定义"一条材料长什么样"，不做任何 IO。
字段命名与取值严格对齐 00-标准/档案标准-v0.1.md。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# --- 来源 ---------------------------------------------------------------

SOURCE_WECHAT = "wechat"
SOURCE_PHOTOS = "photos"
SOURCE_EMAIL = "email"
SOURCE_DOCUMENTS = "documents"
SOURCE_MANUAL = "manual"
SOURCE_LOOSE = "loose"
SOURCE_AI_ASSISTED = "ai_assisted"

SOURCES = (
    SOURCE_WECHAT,
    SOURCE_PHOTOS,
    SOURCE_EMAIL,
    SOURCE_DOCUMENTS,
    SOURCE_MANUAL,
    SOURCE_LOOSE,
    SOURCE_AI_ASSISTED,
)

# 来源的中文名。界面用这个，不要到处散落硬编码。
SOURCE_LABELS = {
    SOURCE_WECHAT: "微信聊天记录",
    SOURCE_PHOTOS: "照片与视频",
    SOURCE_EMAIL: "邮件",
    SOURCE_DOCUMENTS: "笔记与文章",
    SOURCE_MANUAL: "手工录入",
    SOURCE_LOOSE: "其他文件",
    SOURCE_AI_ASSISTED: "AI 协助起草",
}

# AI 起草的记录打这个标记。它要一路传到下游——
# 将来用这份档案训练模型的人，有权知道哪些段落不是本人写的。
AI_ASSISTED_FLAG = "ai_drafted"

# --- 内容类型（标准 §3.2）------------------------------------------------
# 这一列是整份标准里最有价值的部分：原始素材里没有这个信息，
# 而下游工具（微调 / 克隆）都需要它。取值顺序即优先级，高者不被低者覆盖。

KIND_AUTHORED = "authored"
KIND_CHAT_OPINION = "chat_opinion"
KIND_CHAT_EMOTION = "chat_emotion"
KIND_CHAT_MEDIA = "chat_media"
KIND_CHAT_CASUAL = "chat_casual"
KIND_THIRD_PARTY = "third_party"
KIND_AI_DRAFTED = "ai_drafted"
KIND_UNKNOWN = "unknown"

KINDS = (
    KIND_AUTHORED,
    KIND_CHAT_OPINION,
    KIND_CHAT_EMOTION,
    KIND_CHAT_MEDIA,
    KIND_CHAT_CASUAL,
    KIND_THIRD_PARTY,
    KIND_AI_DRAFTED,
    KIND_UNKNOWN,
)

# 对下游的价值排序，用于统计与筛选
KIND_VALUE_NOTE = {
    KIND_AUTHORED: "最高",
    KIND_CHAT_OPINION: "高",
    KIND_CHAT_EMOTION: "高",
    KIND_CHAT_MEDIA: "高（音色来源）",
    KIND_CHAT_CASUAL: "低",
    KIND_THIRD_PARTY: "零",
    KIND_AI_DRAFTED: "不可当本人语料",
    KIND_UNKNOWN: "未知",
}

# --- 标注置信度 ----------------------------------------------------------
# 诚实标注：我们只有启发式规则，没有 ground truth。
CONFIDENCE_RULE = "rule"      # 命中明确规则
CONFIDENCE_WEAK = "weak"      # 默认兜底，不完全可信
CONFIDENCE_MANUAL = "manual"  # 用户手工确认过

# --- 安全级别（标准 §5）--------------------------------------------------

SECURITY_BASIC = "basic"
SECURITY_STANDARD = "standard"
SECURITY_PARANOID = "paranoid"

SECURITY_LEVELS = (SECURITY_BASIC, SECURITY_STANDARD, SECURITY_PARANOID)
SECURITY_DEFAULT = SECURITY_STANDARD

# 用户在选择时必须同时看到"不挡什么"。这里是唯一的事实来源。
SECURITY_TABLE = {
    SECURITY_BASIC: {
        "blocks": "顺手翻看、误删",
        "does_not_block": "拿到文件的人。等于没有加密保护",
        "key_protection": "不加密，或仅混淆",
    },
    SECURITY_STANDARD: {
        "blocks": "磁盘被单独拿走",
        "does_not_block": "能接触已解锁机器的人；运行中的进程",
        "key_protection": "密钥由 Windows DPAPI 保护",
    },
    SECURITY_PARANOID: {
        "blocks": "上述 + 无密钥者离线暴力破解",
        "does_not_block": "密钥丢失即永久不可读，没有任何人（包括作者）能恢复",
        "key_protection": "主密码派生（Argon2id），可配秘密共享",
    },
}

# --- 敏感度分级（沿用 DLRS，不另造）--------------------------------------

SENSITIVITY_LEVELS = (
    "S0_PUBLIC",
    "S1_INTERNAL",
    "S2_CONFIDENTIAL",
    "S3_BIOMETRIC",
    "S4_IDENTITY",
)

# --- 来源可信级别（标准 §4，降级链）--------------------------------------

SOURCE_LEVEL_DESCRIPTIONS = {
    1: "直接读取数据库（可提取语音、类型、精确时间）",
    2: "手机端整体备份 / 直接导出（同上，视机型权限）",
    3: "截图 + OCR（仅文字，无语音，时间精度差）",
    4: "手工录入",
}

# --- 消息方向 -----------------------------------------------------------

DIRECTION_SELF = "self"
DIRECTION_OTHER = "other"
DIRECTION_UNKNOWN = "unknown"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_ISO_RE = re.compile(
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?"
)


def parse_time_loose(text: str) -> str | None:
    """从任意文本里尽力抠出一个 ISO 时间。抠不到返回 None。

    这里故意不用 dateutil —— 保持零依赖。宁可不解析，也不猜错时间。
    """
    if not text:
        return None
    m = _ISO_RE.search(text)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    hh = m.group(4) or "00"
    mm = m.group(5) or "00"
    ss = m.group(6) or "00"
    try:
        yi, moi, di = int(y), int(mo), int(d)
        hhi, mmi, ssi = int(hh), int(mm), int(ss)
    except ValueError:
        return None
    if not (1970 <= yi <= 2200 and 1 <= moi <= 12 and 1 <= di <= 31):
        return None
    if not (0 <= hhi <= 23 and 0 <= mmi <= 59 and 0 <= ssi <= 59):
        return None
    return f"{yi:04d}-{moi:02d}-{di:02d}T{hhi:02d}:{mmi:02d}:{ssi:02d}"


@dataclass
class Message:
    """归一化后的单条消息。所有适配器的输出都必须是这个形状。"""

    ts: str | None
    sender: str
    direction: str
    text: str
    media_hint: str | None = None  # 'voice' / 'image' / 'video' / 'file' / None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Conversation:
    """一次导入产生的会话。一个文件 = 一个会话。"""

    conv_id: str
    title: str
    messages: list[Message] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.messages)


def normalize_whitespace(text: str) -> str:
    """统一空白，但保留换行语义（对话里换行有意义）。"""
    if text is None:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    # 折叠三个以上连续空行
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln.strip() == "":
            blank += 1
            if blank > 2:
                continue
        else:
            blank = 0
        out.append(ln)
    return "\n".join(out).strip()


def as_dict(obj) -> dict:
    return asdict(obj)


def iter_kinds() -> Iterable[str]:
    return KINDS
