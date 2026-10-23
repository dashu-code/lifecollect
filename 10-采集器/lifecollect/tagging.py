"""把一条材料分类成标准 §3.2 定义的 kind。

设计立场（重要）：
    我们只有启发式规则，没有 ground truth。所以每条标注都同时记录
    **它是被哪条规则判定的**、以及**置信度**。宁可说"不确定"，
    也不要给用户一个看不出依据的分类结果。

规则按优先级从高到低排列，先命中的胜出。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import records as R


# --- 媒体标记 -----------------------------------------------------------
# 微信导出工具普遍用这些标记表示非文本消息。
MEDIA_MARKERS = {
    "voice": ("[语音]", "[Voice]", "[音频]", "[voice]"),
    "image": ("[图片]", "[Image]", "[照片]", "[image]", "[表情]"),
    "video": ("[视频]", "[Video]", "[video]"),
    "file": ("[文件]", "[File]", "[链接]", "[link]", "[file]"),
    "call": ("[语音通话]", "[视频通话]", "[通话]"),
    "location": ("[位置]", "[location]"),
    "transfer": ("[转账]", "[红包]", "[微信红包]"),
}


def detect_media_hint(text: str) -> str | None:
    if not text:
        return None
    for kind, markers in MEDIA_MARKERS.items():
        for mk in markers:
            if mk in text:
                return kind
    return None


# --- 情绪信号 -----------------------------------------------------------
EMOTION_WORDS = (
    "开心", "高兴", "难过", "伤心", "生气", "愤怒", "委屈", "失望", "害怕",
    "担心", "焦虑", "孤独", "想你", "想我", "舍不得", "后悔", "心疼",
    "感动", "崩溃", "烦", "累死", "撑不住", "谢谢", "对不起", "抱歉",
    "爱你", "喜欢你", "恨", "讨厌", "无聊", "郁闷", "难受", "幸福",
)

EMOTION_SUFFIX = ("啊", "呀", "哪", "啦", "嘛", "哦", "噢", "耶", "唉", "哎")

# 连续标点：多个感叹号/问号、波浪号——情绪外溢的可靠信号
_RE_PUNCT_BURST = re.compile(r"[!！?？~～]{2,}")
_RE_ELLIPSIS = re.compile(r"(\.{3,}|。{3,}|…{2,})")

# --- 观点信号 -----------------------------------------------------------
OPINION_PATTERNS = (
    "我觉得", "我认为", "我想说", "我不同意", "我赞成", "其实", "本质上",
    "关键是", "问题在于", "应该", "不应该", "必须", "没必要", "值得",
    "不值得", "说白了", "换句话说", "总的来说", "我倒是觉得",
)

# --- 闲聊信号 -----------------------------------------------------------
CASUAL_EXACT = {
    "收到", "好的", "好", "嗯", "嗯嗯", "哦", "噢", "哈哈", "哈哈哈", "呵呵",
    "在吗", "在", "不在", "到了", "马上", "等一下", "稍等", "谢谢", "不客气",
    "晚安", "早安", "早上好", "晚上好", "再见", "拜拜", "ok", "OK", "okay",
    "行", "可以", "没问题", "知道了", "明白", "对", "不对", "是的", "不是",
    "么么", "嘿嘿", "嘻嘻", "冲", "好的呢", "来了",
}

_RE_CASUAL_START = re.compile(
    r"^(好的|收到|嗯+|哦+|在吗|稍等|等一下|马上|行吧|可以|没问题)"
)

CASUAL_MAX_LEN = 6  # 短消息在对话数据里绝大多数是事务性的


@dataclass
class TagResult:
    kind: str
    confidence: str
    rule: str


def _is_casual(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    if len(t) <= CASUAL_MAX_LEN:
        return True
    if t.strip("。！？!?~～ ") in CASUAL_EXACT:
        return True
    if len(t) <= 20 and _RE_CASUAL_START.match(t):
        return True
    return False


def _has_emotion(text: str) -> str | None:
    if _RE_PUNCT_BURST.search(text):
        return "标点爆发"
    if _RE_ELLIPSIS.search(text):
        return "省略号"
    for w in EMOTION_WORDS:
        if w in text:
            return f"情绪词:{w}"
    stripped = text.strip()
    if stripped and stripped[-1] in EMOTION_SUFFIX and len(stripped) > 2:
        return f"语气词:{stripped[-1]}"
    return None


def _has_opinion(text: str) -> str | None:
    for p in OPINION_PATTERNS:
        if p in text:
            return f"观点词:{p}"
    return None


def tag_message(msg: R.Message, source: str) -> TagResult:
    """对单条消息分类。source 影响判定：自己写的文字优先级最高。"""
    text = msg.text or ""
    stripped = text.strip()

    # 0. AI 起草的内容 —— 必须排在最前面。
    #
    #    这一条是正确性问题，不是优先级偏好：AI 写出来的文字在结构上和
    #    "自撰"完全一样（通顺、有观点、有情绪），如果按一般规则走，
    #    它会被判成 authored——也就是"对下游价值最高"。
    #    那等于把机器写的字当成本人的语料喂给下游模型。这是最坏的一种错。
    if source == R.SOURCE_AI_ASSISTED:
        return TagResult(R.KIND_AI_DRAFTED, R.CONFIDENCE_RULE,
                         "来源:AI 协助起草，非本人所写")

    # 1. 自己撰写的（笔记 / 文章 / 邮件）—— 最高价值，先判
    if source in (R.SOURCE_DOCUMENTS, R.SOURCE_LOOSE, R.SOURCE_MANUAL):
        if not msg.media_hint:
            return TagResult(R.KIND_AUTHORED, R.CONFIDENCE_RULE, "来源:自撰文本")

    # 2. 语音 / 图片 / 视频消息 —— 音色来源
    if msg.media_hint and msg.media_hint in ("voice", "image", "video"):
        return TagResult(R.KIND_CHAT_MEDIA, R.CONFIDENCE_RULE, f"媒体:{msg.media_hint}")

    # 3. 空内容
    if not stripped:
        return TagResult(R.KIND_UNKNOWN, R.CONFIDENCE_WEAK, "空内容")

    # 4. 只是一条媒体占位符，没有实际文字
    hint = detect_media_hint(stripped)
    if hint and len(stripped) <= 12:
        if hint in ("voice", "image", "video"):
            return TagResult(R.KIND_CHAT_MEDIA, R.CONFIDENCE_RULE, f"占位符:{hint}")
        return TagResult(R.KIND_CHAT_CASUAL, R.CONFIDENCE_RULE, f"占位符:{hint}")

    # 5. 情绪
    emo = _has_emotion(stripped)
    if emo:
        return TagResult(R.KIND_CHAT_EMOTION, R.CONFIDENCE_RULE, emo)

    # 6. 观点（要求有一定长度，避免"应该"这种词误伤）
    if len(stripped) >= 8:
        op = _has_opinion(stripped)
        if op:
            return TagResult(R.KIND_CHAT_OPINION, R.CONFIDENCE_RULE, op)

    # 7. 闲聊
    if _is_casual(stripped):
        return TagResult(R.KIND_CHAT_CASUAL, R.CONFIDENCE_RULE, "短消息/常用语")

    # 8. 兜底：有长度但没命中任何信号。
    #    标成 weak，不装作知道。
    return TagResult(R.KIND_UNKNOWN, R.CONFIDENCE_WEAK, "未命中规则")
