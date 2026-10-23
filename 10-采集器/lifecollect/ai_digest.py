# =======================================================================
# 批量整理：把已有记录归纳成结构化摘要
# =======================================================================

from dataclasses import dataclass

from .aiassist import AIError, call_chat

DIGEST_SYSTEM = """你在帮一个人整理他自己的档案材料。这些材料会被长期保存，
将来可能被他的家人读到，也可能被用来训练一个像他的模型。

你的任务是**归纳已有的材料**，不是创作。规则：

1. 你只能使用用户提供的记录。记录里没有的信息，一律不许写。
2. **每一条结论后面必须标注出处**，格式是 [记录ID]。例如：
   「他 2019 年搬到了成都 [聊天记录#000123]」
3. 如果多条记录之间有矛盾、或者信息明显缺失，把它们列进「存疑与缺口」，
   不要自己调和、不要推测真相。
4. 不要写评价性的话（"他是一个热爱生活的人"这种）。只写材料里能直接支撑的事实。
5. 如果材料太少，不足以归纳出某一类内容，就直接写「材料不足以归纳」。

输出格式（严格的 Markdown，用小标题分隔）：

## 时间线
- 事件描述 [记录ID]

## 出现的人物
- 名字：从材料里能看出的关系或角色 [记录ID]

## 反复出现的主题
- 主题描述 [记录ID]

## 存疑与缺口
- 哪里矛盾、哪里缺信息，需要本人补充什么"""


@dataclass
class Chunk:
    """一批要送进模型的记录。分批是为了控制 token 成本和上下文长度。"""
    index: int
    ids: list[str]
    text: str
    chars: int


def make_chunks(records: list, max_chars: int = 6000,
                max_records: int = 60) -> list:
    """把记录切成批。按字符数和条数双上限切，先到先切。"""
    chunks = []
    cur_ids = []
    cur_lines = []
    cur_chars = 0

    for r in records:
        rid = str(r.get("record_id") or "")
        text = (r.get("text") or "").strip()
        if not rid or not text:
            continue
        ts = (r.get("captured_at") or "").replace("T", " ")[:19]
        line = "[" + rid + "]" + (" (" + ts + ")" if ts else "") + ": " + text
        n = len(line) + 1
        if cur_lines and (cur_chars + n > max_chars
                          or len(cur_lines) >= max_records):
            chunks.append(Chunk(len(chunks), cur_ids,
                                "\n".join(cur_lines), cur_chars))
            cur_ids, cur_lines, cur_chars = [], [], 0
        cur_ids.append(rid)
        cur_lines.append(line)
        cur_chars += n

    if cur_lines:
        chunks.append(Chunk(len(chunks), cur_ids,
                            "\n".join(cur_lines), cur_chars))
    return chunks


def build_digest_messages(chunk) -> list:
    user = (
        "这是档案里的第 " + str(chunk.index + 1) + " 批记录（共 "
        + str(len(chunk.ids)) + " 条）。请按系统提示的格式归纳。\n\n"
        + "=== 记录开始 ===\n" + chunk.text + "\n=== 记录结束 ===\n\n"
        + "再次提醒：每条结论必须带 [记录ID] 出处；"
        + "材料里没有的不要写；矛盾的地方列进「存疑与缺口」。"
    )
    return [
        {"role": "system", "content": DIGEST_SYSTEM},
        {"role": "user", "content": user},
    ]


def estimate_tokens(text: str) -> int:
    """粗估 token 数。中文约 1.5 字符/token，英文约 4 字符/token。

    这是给用户做成本预估，不是计费。所以宁可保守。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk / 1.5 + other / 4) + 1


def estimate_digest_cost(records: list, provider: dict,
                         max_chars: int = 6000) -> dict:
    """估算批量整理要花多少钱。**不确定就说不确定，不给假数字。**"""
    chunks = make_chunks(records, max_chars=max_chars)
    in_tokens = 0
    for c in chunks:
        in_tokens += estimate_tokens(c.text)
        # 每批的系统提示词和格式要求约 800 tokens
        in_tokens += 800
    # 输出大约是输入的 25%（归纳比原文短，但每批有固定格式开销）
    out_tokens = int(in_tokens * 0.25) + 400 * len(chunks)

    price_in = provider.get("price_in")
    price_out = provider.get("price_out")
    if price_in is None or price_out is None:
        return {
            "chunks": len(chunks),
            "records": sum(len(c.ids) for c in chunks),
            "in_tokens": in_tokens,
            "out_tokens": out_tokens,
            "cost_usd": None,
            "cost_note": provider.get("price_note", "价格未收录"),
        }
    cost = (in_tokens / 1_000_000 * price_in
            + out_tokens / 1_000_000 * price_out)
    return {
        "chunks": len(chunks),
        "records": sum(len(c.ids) for c in chunks),
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": round(cost, 4),
        "cost_note": provider.get("price_note", ""),
    }


def digest_chunks(chunks: list, base_url: str, api_key: str,
                  model: str, on_progress=None,
                  timeout: int = 120) -> dict:
    """逐批调用模型。返回合并后的结果。

    每批失败不中断整批——把失败信息一起返回，让用户知道哪一段没整理。
    这比"全部失败"或"假装成功"都诚实。
    """
    parts = []
    failed = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    for ch in chunks:
        if on_progress:
            on_progress(ch.index, len(chunks))
        try:
            res = call_chat(base_url, api_key, model,
                            build_digest_messages(ch), timeout=timeout)
        except AIError as exc:
            failed.append({"chunk": ch.index, "ids": ch.ids,
                           "error": str(exc)})
            continue
        parts.append({"chunk": ch.index, "ids": ch.ids, "text": res.text})
        u = res.usage or {}
        total_usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        total_usage["completion_tokens"] += int(u.get("completion_tokens") or 0)

    return {"parts": parts, "failed": failed, "usage": total_usage,
            "total_chunks": len(chunks)}


def merge_digest(parts: list) -> str:
    """把多批结果拼成一份可读的摘要。不重新调模型——那会再花一次钱。"""
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]["text"]

    out = [
        "# 材料归纳摘要",
        "",
        "> 本摘要由 AI 分批归纳 " + str(len(parts))
        + " 批记录生成，每条结论标注了出处记录 ID。",
        "",
        "---",
        "",
    ]
    for p in parts:
        out.append("## 第 " + str(p["chunk"] + 1) + " 批（"
                   + str(len(p["ids"])) + " 条记录）")
        out.append("")
        out.append(p["text"])
        out.append("")
    return "\n".join(out)
