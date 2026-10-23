"""AI 协助起草与批量整理：接外部 API 处理材料。

这个模块存在的前提，和档案的核心承诺有一处**冲突**，必须设计上处理掉：

    档案的承诺是「全程离线、不出本机」。
    接 API 就必须联网。

所以这里的规则是硬的：

  1. **默认关闭**。用户不显式配置 API、不显式点按钮，就一个字都不发。
  2. **只发用户选中的内容**。授权、密钥、未选中的记录，一律不上传。
  3. **结果必须人工确认**。AI 产出的东西不会自动进档案。
  4. **写进档案时标记来源**。`source=ai_assisted`、`kind=ai_drafted`。
     将来读档案的人有权知道这些不是本人写的。

两个功能，性质完全不同：

  起草（draft）    —— 用户给主题和依据，AI 写一段**新文字**。
                      风险：会编。所以只允许用于外部公开材料。

  批量整理（digest）—— 用户选中**已有的记录**，AI 把它们**归纳**成结构化摘要。
                      风险低得多：它做的是压缩和归类，不是产生新事实。
                      但仍然强制要求每条结论标注出处记录 ID——
                      没有出处的不许写，这样用户能逐条核对。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class AIError(Exception):
    pass


# 预置常见的中文/国际服务商。都是 OpenAI 兼容的 chat/completions 接口。
#
# 价格用于给用户预估成本，单位是「每 100 万 tokens 的美元价」。
# 这些数字会变，所以每一条都标注了来源和核实日期。
# 不确定的价格写 None，界面会显示"未收录"，绝不给一个假装精确的数字。
PROVIDERS = (
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "note": "国内可直连，便宜。",
        # 来源：https://api-docs.deepseek.com/quick_start/pricing
        # 核实日期：2026-10-17
        # 注意：高峰时段（UTC 01-04、06-10 周一至周五）价格翻倍。
        "price_in": 0.15,    # 缓存未命中，非高峰
        "price_out": 0.60,   # 非高峰
        "price_note": "非高峰价；高峰期翻倍。deepseek-flash。",
    },
    {
        "id": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "note": "需要能访问 api.openai.com。",
        "price_in": None,    # 未核实，不编
        "price_out": None,
        "price_note": "价格未收录，请查官网。",
    },
    {
        "id": "moonshot",
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "note": "国内可直连。",
        "price_in": None,
        "price_out": None,
        "price_note": "价格未收录，请查官网。",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "note": "国内可直连，有免费额度。",
        "price_in": None,
        "price_out": None,
        "price_note": "价格未收录，请查官网。",
    },
    {
        "id": "custom",
        "name": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
        "note": "任何兼容 /chat/completions 的服务。",
        "price_in": None,
        "price_out": None,
        "price_note": "自定义服务，价格未知。",
    },
)


def provider_by_id(pid: str) -> dict:
    for p in PROVIDERS:
        if p["id"] == pid:
            return p
    raise AIError(f"未知服务商: {pid}")


# 系统提示词。刻意压低"创作"倾向，强调"不确定就说不确定"。
SYSTEM_PROMPT = """你在帮一个人整理归档用的材料。这些材料会被长期保存，将来可能被他的家人读到。
所以准确性比流畅重要得多。

规则：
1. 只写你能从用户提供的信息里确定的。不确定的地方明确写「不确定」或「需要核实」，
   不要用模糊措辞掩盖不确定。
2. 不要编造具体细节：日期、地点、人名、数字、引语。没有依据就不要写。
3. 不要写成宣传稿或抒情散文。用平实、具体的语言。
4. 如果用户要求的内容超出了你确知的范围，直接指出，并说明需要什么信息才能写。
5. 输出的正文用中文，结构用 Markdown 小标题。

你写出来的内容会作为草稿给人确认和修改，所以宁可短、宁可少、宁可标注存疑。"""


@dataclass
class DraftResult:
    ok: bool
    text: str
    model: str
    provider: str
    error: str = ""
    usage: dict | None = None


def build_messages(topic: str, context: str = "", style_hint: str = "") -> list[dict]:
    user = [f"请帮我起草一段归档材料。",
            f"主题：{topic.strip()}"]
    if context.strip():
        user.append(f"我提供的事实依据（只能基于这些写，不要自行补充）：\n{context.strip()}")
    else:
        user.append("我没有另外提供依据。请只写这个主题下确定无疑的常识性内容，"
                    "其余部分明确标注为「需要我补充」或「不确定」。")
    if style_hint.strip():
        user.append(f"写作风格要求：{style_hint.strip()}")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(user)},
    ]


def call_chat(base_url: str, api_key: str, model: str,
              messages: list[dict], timeout: int = 90) -> DraftResult:
    """调用 OpenAI 兼容的 /chat/completions。

    这里刻意不引入 requests 之类的依赖——urllib 够用，
    而且这个功能是可选路径，不该为它加包。
    """
    if not base_url:
        raise AIError("没有填接口地址。")
    if not api_key:
        raise AIError("没有填 API Key。")
    if not model:
        raise AIError("没有填模型名。")

    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.2,   # 低温度：档案材料要稳定，不要发挥
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:
            pass
        raise AIError(
            f"接口返回 HTTP {exc.code}。"
            + ("API Key 可能不对，或没有调用权限。" if exc.code in (401, 403)
               else "服务端拒绝了这个请求。" if exc.code >= 500
               else "请求被拒绝。")
            + (f"\n详情：{detail}" if detail else "")
        ) from exc
    except urllib.error.URLError as exc:
        raise AIError(
            f"连不上接口地址：{exc.reason}\n"
            "检查网络、代理，或者换一个能访问的服务商。") from exc
    except TimeoutError as exc:
        raise AIError("请求超时。可能网络太慢，或者模型响应太久。") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AIError(f"接口返回的不是 JSON：{raw[:200]}") from exc

    choices = data.get("choices") or []
    if not choices:
        raise AIError(f"接口没有返回内容：{raw[:300]}")
    msg = choices[0].get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text:
        raise AIError("模型返回了空内容。")

    return DraftResult(
        ok=True,
        text=text,
        model=data.get("model") or model,
        provider="",
        usage=data.get("usage"),
    )


def test_connection(base_url: str, api_key: str, model: str) -> dict:
    """用一句最小请求验证配置能不能用。不打印、不回传 key。"""
    try:
        res = call_chat(base_url, api_key, model, [
            {"role": "user", "content": "回复两个字：可用"}
        ], timeout=30)
    except AIError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "model": res.model,
            "sample": res.text[:40]}
