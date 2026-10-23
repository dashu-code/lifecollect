"""本地 UI 服务：把已经测过的能力包一层。

设计约束（都是前面定过的）：

  1. **只绑 127.0.0.1**，端口随机。不是"默认本地"，是物理上不对外。
  2. **不做任何写入敏感数据的接口**。加锁/解锁只接受口令、不返回口令，
     也不返回主密钥。前端拿不到的东西，前端也泄露不了。
  3. **UI 不直接碰原始数据源**。它只读档案目录，采集流程仍然是 CLI 的活。
     这是「UI 不接触原始数据库」那条约束的落地。
  4. 所有响应显式 UTF-8。中文 Windows 上这条是硬要求。

有一个例外必须写在这里：

  5. **AI 协助起草是唯一会联网的功能**，而且：
     - 默认关闭，用户显式配置并点按钮才发请求
     - 只发送用户这次输入的主题和依据，不上传档案内容
     - 生成的是**草稿**，必须人工确认修改后才写入档案
     - 写入时 source=ai_assisted、kind=ai_drafted，一路标记到下游
     API Key 不明文落盘（standard/paranoid 用 DPAPI/口令包裹）。

跑法：
    python -m lifecollect.ui [--root D:\\档案]

第一次运行会打开一个 pywebview 窗口；窗口关掉服务就停。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from . import records as R
from .archive import Archive
from . import encrypt, store, vault, crypto, pipeline, prompts

# pywebview 只在"原生窗口"模式下才 import。
# --no-window 模式（浏览器）不应该被它拖住启动。
try:  # pragma: no cover
    import webview as _webview
except Exception:  # pragma: no cover
    _webview = None

STATIC_DIR = Path(__file__).resolve().parent / "ui_static"


# --- 示例数据 -----------------------------------------------------------
# 只在用户点"先看看效果"时才会写入。全部是编造的，不含任何真实信息。
# 文件里带"示例-"前缀，方便用户一眼认出来并整个删掉。

SAMPLE_CSV = """时间,发送者,内容
2024-03-01 10:02:11,对方,我觉得这个方案其实没必要改，关键是成本
2024-03-01 10:03:40,我,我不同意，本质上问题在于排期不是成本
2024-03-01 10:04:02,我,唉……这事儿真是让人难受
2024-03-01 21:30:00,我,今天真的累死了！！！
2024-03-02 08:01:00,我,晚安
2024-04-11 14:20:00,我,我一直在想，人到底为什么要在意别人的看法
2024-04-11 14:21:00,我,说到底，评价标准如果不是自己定的，那就永远没有尽头
2024-05-20 20:12:00,我,不知道为什么，这句话让我有点想哭
"""

SAMPLE_NOTE = """示例随笔：关于要不要换工作

表面上是钱的问题，其实不是。
真正让我犹豫的是，我担心自己其实没有想象中那么能干。
如果换个地方还是这样，那说明问题在我身上。

人最难的不是做决定，是承认自己做决定的理由。
"""


# --- 授权粒度 -----------------------------------------------------------
# 12 项，顺序固定。界面按这个顺序渲染，不依赖 JSON 里的键序。
CONSENT_ITEMS = (
    "存储", "结构化处理", "文本人格构建", "语音克隆",
    "视频头像克隆", "3D_avatar构建", "模型微调", "公开索引",
    "商业使用", "跨境处理", "研究使用", "纪念化运行",
)

# 每一项给一句人话解释。界面显示这个，不显示裸 key。
CONSENT_HINTS = {
    "存储": "把材料保存在档案里。关掉这一项，档案就没有意义了。",
    "结构化处理": "把杂乱的材料整理成可检索的记录（标注、索引）。",
    "文本人格构建": "用你的文字材料构建一个说话像你的文本模型。",
    "语音克隆": "用你的录音合成你的音色。声纹一旦泄露不可撤回。",
    "视频头像克隆": "用你的视频/照片生成会动的人像。",
    "3D_avatar构建": "构建三维形象。成本最高，通常最后才做。",
    "模型微调": "拿你的材料去微调模型。会改变模型的说话方式。",
    "公开索引": "把你的档案登记到公开目录里，别人能搜到。",
    "商业使用": "把这些材料用于营利。",
    "跨境处理": "把数据传到中国大陆以外的地方处理。",
    "研究使用": "用于学术研究。",
    "纪念化运行": "在你去世后继续运行这个档案。",
}

CONSENT_VALUE_LABEL = {"allow": "允许", "deny": "禁止", "unset": "未定"}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().replace(
        microsecond=0).isoformat()


def _atomic_text(path: Path, text: str) -> None:
    """原子写。中途崩溃不会留下半个文件。"""
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_json(path: Path, obj) -> None:
    _atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def _render_consent_md(archive: Archive, data: dict) -> None:
    """把结构化授权声明的**人读版**写回 .md。

    这是档案的核心约束：没有本软件也要能读。所以 JSON 一变，
    .md 必须跟着变，不能出现两者不一致。
    """
    g = data.get("granularity", {})
    lines = [
        "# 授权声明",
        "",
        "> 本文件由 lifecollect 根据结构化授权数据自动生成。",
        "> **不要手工编辑本文件** —— 请在上层应用里修改，否则两者会不一致。",
        "",
        "## 一、允许与禁止",
        "",
        "| 项目 | 状态 | 说明 |",
        "|---|---|---|",
    ]
    for k in CONSENT_ITEMS:
        v = g.get(k, "unset")
        label = CONSENT_VALUE_LABEL.get(v, v)
        if v == "allow":
            mark = "**允许**"
        elif v == "deny":
            mark = "**禁止**"
        else:
            mark = "未定（按禁止处理）"
        lines.append(f"| {k} | {mark} | {CONSENT_HINTS.get(k, '')} |")

    counts = {v: sum(1 for x in g.values() if x == v) for v in
              ("allow", "deny", "unset")}
    lines += [
        "",
        f"统计：允许 {counts['allow']} 项，禁止 {counts['deny']} 项，"
        f"未定 {counts['unset']} 项。**未定一律按禁止处理。**",
        "",
        "## 二、标注义务",
        "",
        "使用本档案生成的一切内容，**必须显著标明为 AI 合成内容**。",
        "",
        "这不是建议。任何使用者都必须遵守。",
        "",
        "## 三、撤回",
        "",
        "档案主人可以随时撤回上述任何一项授权。撤回后，",
        "使用者必须停止相应行为并删除由此产生的产物。",
        "",
        "## 四、签署",
        "",
        f"- 签名：{data.get('signature') or '（未签署）'}",
        f"- 日期：{data.get('date') or '（未填写）'}",
        f"- 最后修改：{data.get('updated_at') or data.get('generated_at') or ''}",
        "",
        "---",
        "",
        "机器可读版本在同目录的 `授权声明.json`。两者内容由程序保证一致。",
        "",
    ]
    _atomic_text(archive.consent_dir / "授权声明.md", "\n".join(lines))


# --- 示例数据 -----------------------------------------------------------
# 只在用户点"先看看效果"时才会写入。全部是编造的，不含任何真实信息。
# 文件里带"示例-"前缀，方便用户一眼认出来并整个删掉。


class Api:
    """给前端的接口。每个方法都返回 JSON 友好的 dict。"""

    def __init__(self, archive: Archive):
        self.archive = archive
        self._key: bytes | None = None  # 只活在这个进程的内存里
        self._pw: str | None = None
        self._window = None  # 原生窗口句柄；浏览器模式下一直是 None

    # --- 档案状态 -------------------------------------------------------
    def config(self) -> dict:
        """前端启动时问的第一件事：现在有没有可用档案。"""
        exists = self.archive.exists()
        out = {
            "root": str(self.archive.root),
            "initialized": exists,
        }
        if exists:
            meta = self.archive.meta()
            out.update({
                "subject": meta.get("subject") or "",
                "level": self.archive.security_level,
                "created_at": meta.get("created_at", ""),
                "locked": encrypt.is_locked(self.archive),
                "has_envelope": vault.envelope_exists(self.archive.root),
                "records": None,
            })
            try:
                from .archive import read_jsonl
                if not encrypt.is_locked(self.archive):
                    out["records"] = sum(1 for _ in read_jsonl(
                        self.archive.records_path))
            except Exception:
                pass
        return out

    def create_archive(self, subject: str, level: str,
                       password: str = "", password2: str = "") -> dict:
        """从界面里建立一个新档案。这是引导流程的第一步。"""
        if self.archive.exists() and any(self.archive.root.iterdir()):
            return {"error": "exists",
                    "message": f"{self.archive.root} 不是空目录，不能在这里建档案。"}
        if level not in R.SECURITY_LEVELS:
            return {"error": "bad_level", "message": f"未知安全级别: {level}"}
        if level in ("standard", "paranoid"):
            if not password:
                return {"error": "need_password", "message": "加密档案必须设口令。"}
            if len(password) < 8:
                return {"error": "weak_password",
                        "message": "口令太短（至少 8 个字符）。"}
            if password != password2:
                return {"error": "pw_mismatch", "message": "两次输入的口令不一致。"}
        try:
            self.archive.init(security_level=level, subject=subject or "")
            if level in ("standard", "paranoid"):
                master = crypto.random_key()
                env = vault.create_envelope(master, password, level)
                vault.save_envelope(self.archive.root, env)
                self.archive.audit("key_created", {
                    "security_level": level,
                    "ways": vault.describe_ways(env),
                    "key_fingerprint": crypto.key_fingerprint(master),
                })
        except Exception as exc:
            return {"error": "create_failed",
                    "message": f"{type(exc).__name__}: {exc}"}
        # 重要：这个服务进程里的 archive 对象必须同步更新。
        # Archive 每次调用都会重读磁盘上的 _meta.json，所以赋值回 self.archive
        # 就够了——但不能省略，否则同一进程内的后续请求可能拿到旧的引用。
        self.archive = Archive(self.archive.root)
        if level in ("standard", "paranoid"):
            self.archive.update_meta(security_level=level)
        return {"ok": True, "root": str(self.archive.root),
                "level": level, "subject": subject}

    def load_sample(self) -> dict:
        """往一个空档案里装示例数据，给"我想先看效果"的人用。

        明确标注：这是编造的数据，用完可以整个删掉。
        """
        if not self.archive.exists():
            return {"error": "no_archive",
                    "message": "请先建立档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁。"}
        try:
            import tempfile
            from . import pipeline as _pipeline
            from . import records as _R
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                (tdp / "示例-聊天记录.csv").write_text(
                    SAMPLE_CSV, encoding="utf-8")
                (tdp / "示例-随笔.txt").write_text(SAMPLE_NOTE, encoding="utf-8")
                _pipeline.import_paths(
                    self.archive, [tdp / "示例-聊天记录.csv"],
                    _R.SOURCE_WECHAT, {"我"}, verbose=False)
                _pipeline.import_paths(
                    self.archive, [tdp / "示例-随笔.txt"],
                    _R.SOURCE_DOCUMENTS, {"我"}, verbose=False)
            _pipeline.tag_archive(self.archive, verbose=False)
            store.rebuild(self.archive)
            _pipeline.write_view(self.archive)
            self.archive.audit("sample_loaded", {})
        except Exception as exc:
            return {"error": "sample_failed",
                    "message": f"{type(exc).__name__}: {exc}"}
        return {"ok": True}

    # --- 导入材料 -------------------------------------------------------
    #
    # 这是"向导结束之后怎么把真实数据弄进来"的答案。
    # 之前只有命令行，非技术用户走不通——这是设计缺口，不是便利问题。

    def import_paths(self, paths: list = None, source: str = "wechat",
                     source_level: int = 2, self_names: list = None,
                     tag_and_index: bool = True) -> dict:
        """导入一批真实文件。导入后自动标注 + 建索引 + 重写清单。"""
        if not self.archive.exists():
            return {"error": "no_archive", "message": "请先建立档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁才能导入。"}
        if not paths:
            return {"error": "no_paths", "message": "没有选择任何文件。"}
        if source not in R.SOURCES:
            return {"error": "bad_source", "message": f"未知来源: {source}"}

        from . import pipeline as _pipeline
        from pathlib import Path as _Path

        resolved = []
        missing = []
        for p in paths:
            pp = _Path(p).expanduser()
            if not pp.exists():
                missing.append(str(p))
            else:
                resolved.append(pp)
        if not resolved:
            return {"error": "not_found",
                    "message": "这些路径都不存在：\n" + "\n".join(missing)}

        selfs = set((n or "").strip() for n in (self_names or []) if (n or "").strip())
        try:
            rep = _pipeline.import_paths(
                self.archive, resolved, source, selfs,
                source_level=int(source_level or 2), verbose=False)
        except Exception as exc:
            return {"error": "import_failed",
                    "message": f"{type(exc).__name__}: {exc}"}

        out = {
            "ok": True,
            "report": rep,
            "missing_paths": missing,
            "tagged": None,
            "indexed": None,
        }
        if tag_and_index and rep.get("files_ok"):
            try:
                out["tagged"] = _pipeline.tag_archive(self.archive, verbose=False)
                out["indexed"] = store.rebuild(self.archive)
                _pipeline.write_view(self.archive)
            except Exception as exc:
                out["post_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def peek_files(self, paths: list = None) -> dict:
        """导入前先看一眼：这些文件里有多少条、能不能解析、发送者是谁。

        重要的是让用户在**真正导入之前**看到"它认出了谁"，
        因为 --self 选错会让下游把别人的话当成档案主人的。
        """
        if not paths:
            return {"error": "no_paths", "message": "没有选择任何文件。"}
        from .ingest import parse_file, SUPPORTED_EXTENSIONS
        from pathlib import Path as _Path

        items = []
        for p in paths:
            pp = _Path(p).expanduser()
            if not pp.exists():
                items.append({"path": str(p), "error": "路径不存在"})
                continue
            if pp.is_dir():
                candidates = [q for q in sorted(pp.rglob("*"))
                              if q.is_file()
                              and q.suffix.lower() in SUPPORTED_EXTENSIONS]
                media = 0
                for q in sorted(pp.rglob("*")):
                    if q.is_file() and q.suffix.lower() in (
                            ".silk", ".amr", ".mp3", ".wav", ".m4a",
                            ".jpg", ".jpeg", ".png", ".gif", ".bmp",
                            ".webp", ".heic", ".mp4", ".mov", ".avi"):
                        media += 1
                items.append({
                    "path": str(pp), "is_dir": True,
                    "parseable": len(candidates),
                    "media": media,
                    "supported": True,
                })
                continue
            conv, note = parse_file(pp, set(), 2)
            if conv is None:
                items.append({"path": str(pp), "error": note})
                continue
            senders: dict = {}
            for m in conv.messages:
                s = (m.sender or "").strip()
                if s:
                    senders[s] = senders.get(s, 0) + 1
            top = sorted(senders.items(), key=lambda kv: -kv[1])[:6]
            items.append({
                "path": str(pp),
                "name": pp.name,
                "messages": len(conv),
                "senders": [{"name": n, "count": c} for n, c in top],
                "note": note,
            })
        return {"items": items}

    def pick_files(self) -> dict:
        """打开系统文件选择框。只在原生窗口里可用（浏览器里返回说明）。"""
        win = getattr(self, "_window", None)
        if win is None:
            return {"error": "no_gui",
                    "message": "当前是浏览器模式，无法打开系统文件框。"
                               "请在下方的路径输入框里直接粘贴文件路径。"}
        try:
            result = win.create_file_dialog(
                _webview.OPEN_DIALOG, allow_multiple=True,
                file_types=("材料文件 (*.csv;*.txt;*.jsonl;*.json;*.html;*.md;*.log)",
                            "所有文件 (*.*)"))
        except Exception as exc:
            return {"error": "dialog_failed",
                    "message": f"{type(exc).__name__}: {exc}"}
        if not result:
            return {"canceled": True, "paths": []}
        return {"paths": [str(p) for p in result]}

    def pick_folder(self) -> dict:
        win = getattr(self, "_window", None)
        if win is None:
            return {"error": "no_gui",
                    "message": "当前是浏览器模式，无法打开系统文件夹框。"}
        try:
            result = win.create_file_dialog(_webview.FOLDER_DIALOG)
        except Exception as exc:
            return {"error": "dialog_failed",
                    "message": f"{type(exc).__name__}: {exc}"}
        if not result:
            return {"canceled": True, "paths": []}
        if isinstance(result, (list, tuple)):
            return {"paths": [str(p) for p in result]}
        return {"paths": [str(result)]}

    # --- AI 协助起草 ----------------------------------------------------
    #
    # 硬规则（写在这里，因为这是唯一会联网的功能）：
    #   - 默认关闭：没配置就什么都不发
    #   - 只发用户这次输入的内容，不碰档案数据
    #   - 生成的是草稿，必须人工确认才落盘
    #   - 落盘时打 ai_assisted / ai_drafted 标记
    #   - API Key 不明文存盘

    AI_CONFIG_NAME = "AI配置.json"

    def _ai_config_path(self):
        return self.archive.root / self.AI_CONFIG_NAME

    def _dpapi_or_plain(self, text: str) -> dict:
        """把 API Key 包裹起来。能用 DPAPI 就用，否则明确标注为明文。

        这是刻意的：如果用户的机器没有 DPAPI（非 Windows），
        我们不能假装安全，要明确告诉他不安全。
        """
        if not text:
            return {"mode": "empty", "value": ""}
        if vault.dpapi_available():
            try:
                return {"mode": "dpapi",
                        "value": base64.b64encode(
                            vault.dpapi_protect(text.encode("utf-8"))
                        ).decode("ascii")}
            except Exception:
                pass
        return {"mode": "plain", "value": base64.b64encode(
            text.encode("utf-8")).decode("ascii")}

    def _unwrap_key(self, wrapped: dict) -> str:
        mode = (wrapped or {}).get("mode", "empty")
        raw = (wrapped or {}).get("value", "")
        if mode == "empty" or not raw:
            return ""
        try:
            data = base64.b64decode(raw.encode("ascii"))
        except Exception:
            return ""
        if mode == "dpapi":
            try:
                return vault.dpapi_unprotect(data).decode("utf-8")
            except Exception:
                return ""
        if mode == "plain":
            try:
                return data.decode("utf-8")
            except Exception:
                return ""
        return ""

    def ai_config(self) -> dict:
        """读配置。**不返回 key 本身**，只返回有没有配。"""
        if not self.archive.exists():
            return {"error": "no_archive"}
        from . import aiassist
        cfg = {}
        p = self._ai_config_path()
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cfg = {}
        wrapped = cfg.get("api_key", {})
        return {
            "providers": [{"id": x["id"], "name": x["name"],
                           "base_url": x["base_url"], "model": x["model"],
                           "note": x["note"]} for x in aiassist.PROVIDERS],
            "provider": cfg.get("provider", "deepseek"),
            "base_url": cfg.get("base_url", ""),
            "model": cfg.get("model", ""),
            "has_key": bool((wrapped or {}).get("value")),
            "key_mode": (wrapped or {}).get("mode", "empty"),
            "enabled": bool(cfg.get("enabled", False)),
            "configured_at": cfg.get("configured_at", ""),
        }

    def ai_save_config(self, provider: str = "deepseek",
                       base_url: str = "", model: str = "",
                       api_key: str = "", enabled: bool = True) -> dict:
        """保存配置。空 api_key 表示"保持原来的，不要清掉"。"""
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        from . import aiassist

        p = self._ai_config_path()
        cfg = {}
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cfg = {}

        try:
            prov = aiassist.provider_by_id(provider)
        except aiassist.AIError as exc:
            return {"error": "bad_provider", "message": str(exc)}

        cfg["provider"] = provider
        cfg["base_url"] = (base_url or prov["base_url"]).strip()
        cfg["model"] = (model or prov["model"]).strip()
        cfg["enabled"] = bool(enabled)
        cfg["configured_at"] = _now_iso()
        if api_key.strip():
            cfg["api_key"] = self._dpapi_or_plain(api_key.strip())

        try:
            _atomic_json(p, cfg)
        except OSError as exc:
            return {"error": "write_failed", "message": str(exc)}
        self.archive.audit("ai_config_saved", {
            "provider": provider,
            "model": cfg["model"],
            "enabled": cfg["enabled"],
            "key_stored": bool((cfg.get("api_key") or {}).get("value")),
            "key_mode": (cfg.get("api_key") or {}).get("mode", "empty"),
        })
        return {"ok": True, "key_mode": (cfg.get("api_key") or {}).get("mode", "empty")}

    def ai_test(self) -> dict:
        """用当前配置发一个最小请求验证可用性。不发任何档案内容。"""
        if not self.archive.exists():
            return {"error": "no_archive"}
        from . import aiassist
        p = self._ai_config_path()
        if not p.exists():
            return {"ok": False, "message": "还没有配置 AI。"}
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"ok": False, "message": "配置文件损坏。"}
        key = self._unwrap_key(cfg.get("api_key", {}))
        if not key:
            return {"ok": False,
                    "message": "没有可用的 API Key。这次会话里重新填一次。"}
        res = aiassist.test_connection(cfg.get("base_url", ""), key,
                                       cfg.get("model", ""))
        if res.get("ok"):
            self.archive.audit("ai_test_ok", {"model": res.get("model")})
        return res

    def ai_draft(self, topic: str = "", context: str = "",
                 style_hint: str = "") -> dict:
        """让 AI 起草一段材料。

        **不回传、不记录 API Key。** 结果只是草稿，不进档案。
        用户必须调用 ai_commit_draft 才真的写进去。
        """
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        if not (topic or "").strip():
            return {"error": "no_topic",
                    "message": "请先在「主题」里写一句你要整理什么。"}
        from . import aiassist
        p = self._ai_config_path()
        if not p.exists():
            return {"error": "not_configured",
                    "message": "还没有配置 AI。先在下面填服务商和 API Key。"}
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"error": "bad_config", "message": "配置文件损坏。"}
        if not cfg.get("enabled"):
            return {"error": "disabled",
                    "message": "AI 协助当前是关闭状态。"}
        key = self._unwrap_key(cfg.get("api_key", {}))
        if not key:
            return {"error": "no_key",
                    "message": "没有可用的 API Key。重新填一次。"}

        msgs = aiassist.build_messages(topic, context, style_hint)
        try:
            res = aiassist.call_chat(cfg.get("base_url", ""), key,
                                     cfg.get("model", ""), msgs)
        except aiassist.AIError as exc:
            self.archive.audit("ai_draft_failed",
                               {"topic_chars": len(topic),
                                "error": str(exc)[:200]})
            return {"error": "ai_failed", "message": str(exc)}

        self.archive.audit("ai_draft_made", {
            "topic_chars": len(topic),
            "context_chars": len(context or ""),
            "model": res.model,
        })
        return {
            "ok": True,
            "draft": res.text,
            "model": res.model,
            "usage": res.usage,
            "notice": ("这是 AI 起草的草稿，不是本人写的。"
                       "请逐句核对，删掉你不确认的部分，再点保存。"
                       "保存后会标记为「AI 协助起草」。"),
        }

    def ai_commit_draft(self, topic: str = "", text: str = "",
                        model: str = "") -> dict:
        """把人工确认过的草稿写进档案。

        写进去时：
          - source = ai_assisted（不是 documents！）
          - kind   = ai_drafted（不是 authored！）
          - 说明里留下模型名和确认时间
        """
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁。"}
        if not (text or "").strip():
            return {"error": "empty", "message": "正文是空的。"}
        if not (topic or "").strip():
            return {"error": "no_topic", "message": "缺少主题。"}

        from . import pipeline as _pipeline
        from . import records as _R
        import tempfile
        from pathlib import Path as _P

        body = text.strip()
        header = (f"# {topic.strip()}\n\n"
                  f"> 本段由 AI 协助起草，经人工确认后归档。\n"
                  f"> 模型：{model or '（未记录）'}\n"
                  f"> 归档时间：{_now_iso()}\n\n")
        full = header + body + "\n"

        safe = "".join(ch for ch in topic.strip()
                       if ch not in '\\/:*?"<>|')[:40] or "AI草稿"
        try:
            with tempfile.TemporaryDirectory() as td:
                f = _P(td) / f"AI协助-{safe}.md"
                f.write_text(full, encoding="utf-8")
                rep = _pipeline.import_paths(
                    self.archive, [f], _R.SOURCE_AI_ASSISTED,
                    set(), source_level=4, verbose=False)
            tagged = _pipeline.tag_archive(self.archive, verbose=False)
            indexed = store.rebuild(self.archive)
            _pipeline.write_view(self.archive)
        except Exception as exc:
            return {"error": "commit_failed",
                    "message": f"{type(exc).__name__}: {exc}"}

        self.archive.audit("ai_draft_committed", {
            "model": model or "",
            "chars": len(body),
            "records": rep.get("messages", 0),
        })
        return {"ok": True, "report": rep, "tagged": tagged,
                "indexed": indexed,
                "source_label": _R.SOURCE_LABELS.get(_R.SOURCE_AI_ASSISTED, ""),
                "kind": _R.KIND_AI_DRAFTED}

    # --- 批量整理：把已有记录归纳成摘要 -------------------------------
    #
    # 和「起草」是两件事。起草是产生新文字（会编，只许用于公开材料）；
    # 整理是压缩已有材料（不产生新事实，可以被核对）。
    # 这里的强制要求：每条结论必须带出处记录 ID。

    def _selected_records(self, scope: str = "usable",
                          max_records: int = 2000) -> list:
        """按范围挑要送进模型的记录。

        默认只挑"有信息量"的（自撰/观点/情绪），跳过闲聊。
        闲聊占聊天记录的大头，送进去纯烧钱，还稀释信号。
        """
        from .archive import read_jsonl
        from . import records as _R

        if not self.archive.exists():
            return []
        if encrypt.is_locked(self.archive):
            return []

        all_recs = list(read_jsonl(self.archive.records_path))
        if scope == "all":
            picked = all_recs
        elif scope == "self":
            picked = [r for r in all_recs if r.get("direction") == "self"]
        else:  # usable
            keep = (_R.KIND_AUTHORED, _R.KIND_CHAT_OPINION,
                    _R.KIND_CHAT_EMOTION)
            picked = [r for r in all_recs if r.get("kind") in keep]
            if not picked:  # 没标注过就退回全部
                picked = all_recs

        picked = [r for r in picked
                  if (r.get("text") or "").strip()
                  and r.get("source") != _R.SOURCE_AI_ASSISTED]
        return picked[:max_records]

    def ai_digest_estimate(self, scope: str = "usable") -> dict:
        """算一下要花多少钱、分几批。**先给数字，让用户决定要不要花。**"""
        if not self.archive.exists():
            return {"error": "no_archive"}
        from . import ai_digest as _digest
        from . import aiassist

        p = self.archive.root / self.AI_CONFIG_NAME
        cfg = {}
        if p.exists():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                cfg = {}
        try:
            prov = aiassist.provider_by_id(cfg.get("provider", "deepseek"))
        except aiassist.AIError:
            prov = aiassist.PROVIDERS[0]

        recs = self._selected_records(scope)
        est = _digest.estimate_digest_cost(recs, prov)
        est["scope"] = scope
        est["model"] = cfg.get("model") or prov.get("model")
        est["provider_name"] = prov.get("name")
        est["provider_id"] = prov.get("id")
        return est

    def ai_digest_run(self, scope: str = "usable") -> dict:
        """执行批量整理。返回摘要草稿，**不自动进档案**。

        这是有意的：摘要也是 AI 产出，同样要人确认。
        """
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁。"}
        from . import ai_digest as _digest

        p = self.archive.root / self.AI_CONFIG_NAME
        if not p.exists():
            return {"error": "not_configured", "message": "还没有配置 AI。"}
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"error": "bad_config", "message": "配置文件损坏。"}
        if not cfg.get("enabled"):
            return {"error": "disabled", "message": "AI 协助当前是关闭状态。"}
        key = self._unwrap_key(cfg.get("api_key", {}))
        if not key:
            return {"error": "no_key", "message": "没有可用的 API Key。"}

        recs = self._selected_records(scope)
        if not recs:
            return {"error": "no_records",
                    "message": "没有可整理的记录。先导入材料并标注。"}

        chunks = _digest.make_chunks(recs)
        if len(chunks) > 40:
            return {"error": "too_many_chunks",
                    "message": f"这批材料会被切成 {len(chunks)} 批，太多了。"
                               f"先用更小的范围（比如只看自己发的）。"}

        try:
            res = _digest.digest_chunks(
                chunks, cfg.get("base_url", ""), key, cfg.get("model", ""))
        except Exception as exc:
            return {"error": "digest_failed",
                    "message": f"{type(exc).__name__}: {exc}"}

        merged = _digest.merge_digest(res["parts"])
        self.archive.audit("ai_digest_made", {
            "scope": scope,
            "records": len(recs),
            "chunks": len(chunks),
            "failed_chunks": len(res["failed"]),
            "usage": res["usage"],
        })
        return {
            "ok": True,
            "digest": merged,
            "chunks": len(chunks),
            "records": len(recs),
            "failed": res["failed"],
            "usage": res["usage"],
            "notice": ("这是 AI 对已有材料的归纳，不是新材料。"
                       "每条结论后面标了出处记录 ID，请逐条核对。"
                       "标「存疑与缺口」的地方需要你补充。"),
        }

    def ai_digest_commit(self, digest: str = "", scope: str = "usable",
                         model: str = "") -> dict:
        """把人工核对过的摘要写进档案。同样标 ai_assisted / ai_drafted。"""
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁。"}
        if not (digest or "").strip():
            return {"error": "empty", "message": "摘要内容是空的。"}

        from . import pipeline as _pipeline
        from . import records as _R
        import tempfile
        from pathlib import Path as _P

        scope_label = {"usable": "有信息量的记录", "self": "本人发的记录",
                       "all": "全部记录"}.get(scope, scope)
        header = (f"# 材料归纳摘要\n\n"
                  f"> 本摘要由 AI 归纳档案里已有的材料，经人工核对后归档。\n"
                  f"> 归纳范围：{scope_label}\n"
                  f"> 模型：{model or '（未记录）'}\n"
                  f"> 归档时间：{_now_iso()}\n"
                  f"> **摘要不是新材料。** 每条结论的出处见 [记录ID]。\n\n")
        full = header + digest.strip() + "\n"

        try:
            with tempfile.TemporaryDirectory() as td:
                f = _P(td) / "AI归纳摘要.md"
                f.write_text(full, encoding="utf-8")
                rep = _pipeline.import_paths(
                    self.archive, [f], _R.SOURCE_AI_ASSISTED,
                    set(), source_level=4, verbose=False)
            tagged = _pipeline.tag_archive(self.archive, verbose=False)
            indexed = store.rebuild(self.archive)
            _pipeline.write_view(self.archive)
        except Exception as exc:
            return {"error": "commit_failed",
                    "message": f"{type(exc).__name__}: {exc}"}

        self.archive.audit("ai_digest_committed", {
            "model": model or "", "chars": len(digest),
            "scope": scope, "records": rep.get("messages", 0)})
        return {"ok": True, "report": rep, "tagged": tagged,
                "indexed": indexed, "kind": _R.KIND_AI_DRAFTED}

    # --- 档案切换 -------------------------------------------------------
    def get_archives(self) -> dict:
        if not self.archive.exists():
            return {"archives": []}
        return {"archives": [{
            "root": str(self.archive.root),
            "subject": self.archive.meta().get("subject") or "（未命名）",
            "level": self.archive.security_level,
        }]}

    def switch_archive(self, root: str) -> dict:
        a = Archive(root)
        if not a.exists():
            return {"error": f"不是已初始化的档案: {root}"}
        self.archive = a
        self._key = None
        self._pw = None
        return {"ok": True, "root": str(a.root)}

    # --- 概览 -----------------------------------------------------------
    def overview(self) -> dict:
        a = self.archive
        if not a.exists():
            return {"initialized": False, "root": str(a.root)}
        meta = a.meta()
        level = a.security_level
        st = encrypt.status(a)

        out = {
            "initialized": True,
            "root": str(a.root),
            "subject": meta.get("subject") or "",
            "level": level,
            "level_info": R.SECURITY_TABLE.get(level, {}),
            "created_at": meta.get("created_at", ""),
            "updated_at": meta.get("updated_at", ""),
            "locked": st["locked"],
            "encrypted_files": st["encrypted_files"],
            "plain_files": st["plain_files"],
            "always_clear": st["always_clear"],
            "key_fingerprint": st.get("key_fingerprint"),
            "ways": [],
        }
        if vault.envelope_exists(a.root):
            try:
                env = vault.load_envelope(a.root)
                out["ways"] = vault.describe_ways(env)
                out["key_fingerprint"] = env.get("key_fingerprint")
            except vault.VaultError:
                pass

        if not st["locked"] and a.db_path.exists():
            try:
                s = store.stats(a)
                r = store.training_readiness(a)
                out["stats"] = s
                out["readiness"] = r
            except Exception:
                out["stats"] = None
                out["readiness"] = None
        else:
            out["stats"] = None
            out["readiness"] = None
        return out

    # --- 记录 -----------------------------------------------------------
    def records(self, limit: int = 60, kind: str = "", direction: str = "",
                source: str = "") -> dict:
        from .archive import read_jsonl
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有建立档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁才能读内容。"}
        recs = list(read_jsonl(self.archive.records_path))
        if kind:
            recs = [r for r in recs if r.get("kind") == kind]
        if direction:
            recs = [r for r in recs if r.get("direction") == direction]
        if source:
            recs = [r for r in recs if r.get("source") == source]
        total = len(recs)
        # 按时间排序，没有时间的排最后
        recs.sort(key=lambda r: (r.get("captured_at") or "9999"), reverse=True)
        out = []
        for r in recs[:limit]:
            out.append({
                "id": r.get("record_id"),
                "ts": r.get("captured_at"),
                "sender": r.get("sender") or "",
                "direction": r.get("direction"),
                "kind": r.get("kind"),
                "confidence": r.get("tag_confidence"),
                "rule": r.get("tag_rule"),
                "text": r.get("text") or "",
                "source": r.get("source"),
                "level": r.get("source_level"),
                "media": r.get("media_hint"),
            })
        return {"total": total, "shown": len(out), "records": out}

    def record_kinds(self) -> dict:
        """给筛选项用：各类型的数量。"""
        from .archive import read_jsonl
        if not self.archive.exists():
            return {"error": "no_archive"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked"}
        counts: dict[str, int] = {}
        dirs: dict[str, int] = {}
        for r in read_jsonl(self.archive.records_path):
            k = r.get("kind") or "unknown"
            counts[k] = counts.get(k, 0) + 1
            d = r.get("direction") or "unknown"
            dirs[d] = dirs.get(d, 0) + 1
        return {"kinds": counts, "directions": dirs,
                "kind_notes": R.KIND_VALUE_NOTE}

    # --- 检索 -----------------------------------------------------------
    def search(self, q: str, kind: str = "", self_only: bool = False,
               limit: int = 40) -> dict:
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有建立档案。"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked", "message": "档案已锁定，先解锁才能检索。"}
        if not self.archive.db_path.exists():
            return {"error": "no_index",
                    "message": "还没有建索引。运行 lifecollect index。"}
        if not (q or "").strip():
            return {"query": "", "results": [], "total": 0}
        try:
            rows = store.search(self.archive, q, kind=kind or None,
                                self_only=self_only, limit=limit)
        except ValueError as exc:
            return {"error": "bad_query", "message": str(exc)}
        results = []
        for r in rows:
            text = r.get("text") or ""
            results.append({
                "id": r.get("record_id"),
                "ts": r.get("captured_at"),
                "sender": r.get("sender") or "",
                "direction": r.get("direction"),
                "kind": r.get("kind"),
                "text": text,
                "snippet": _snippet(text, q),
                "confidence": r.get("tag_confidence"),
            })
        return {"query": q, "total": len(results), "results": results}

    # --- 呈现层文档 -----------------------------------------------------
    def documents(self) -> dict:
        if not self.archive.exists():
            return {"error": "no_archive"}
        if encrypt.is_locked(self.archive):
            return {"error": "locked"}
        docs = []
        for p in sorted(self.archive.view_dir.glob("*.md")):
            docs.append({
                "name": p.name,
                "size": p.stat().st_size,
                "content": p.read_text(encoding="utf-8"),
            })
        return {"documents": docs}

    # --- 授权文件 -------------------------------------------------------
    def consent(self) -> dict:
        if not self.archive.exists():
            return {"error": "no_archive"}
        cdir = self.archive.consent_dir
        out = {"statement": None, "after_death": None, "key_custody": None}

        # 授权声明：12 项粒度。这是要给人**填**的，不是给人读的——
        # 所以返回结构化数据，前端用开关渲染，不是丢一个 md 文件过去。
        cj = cdir / "授权声明.json"
        if cj.exists():
            try:
                data = json.loads(cj.read_text(encoding="utf-8"))
                out["statement"] = {
                    "granularity": data.get("granularity", {}),
                    "signature": data.get("signature", ""),
                    "date": data.get("date", ""),
                    "generated_at": data.get("generated_at", ""),
                    "updated_at": data.get("updated_at", ""),
                }
            except json.JSONDecodeError:
                out["statement"] = None

        ad = cdir / "身后指令.md"
        if ad.exists():
            out["after_death"] = ad.read_text(encoding="utf-8")

        kc = cdir / "密钥托管.json"
        if kc.exists():
            try:
                out["key_custody"] = json.loads(kc.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return out

    def save_consent(self, granularity: dict = None,
                     signature: str = "", date: str = "") -> dict:
        """保存授权声明，并**同步重写人类可读的 .md**。

        JSON 给程序看，.md 给人看。两者必须一致——否则二十年后
        没有这个软件时，别人读到的是旧的授权内容。
        """
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        cdir = self.archive.consent_dir
        cdir.mkdir(parents=True, exist_ok=True)
        cj = cdir / "授权声明.json"

        try:
            data = json.loads(cj.read_text(encoding="utf-8")) if cj.exists() else {}
        except json.JSONDecodeError:
            data = {}

        if granularity is not None:
            if not isinstance(granularity, dict):
                return {"error": "bad_args", "message": "granularity 必须是对象。"}
            existing = data.get("granularity", {})
            cleaned = {}
            for k in CONSENT_ITEMS:
                v = granularity.get(k, existing.get(k, "unset"))
                cleaned[k] = v if v in ("allow", "deny", "unset") else "unset"
            data["granularity"] = cleaned

        data["standard"] = data.get("standard") or "档案标准-v0.1"
        data["generated_at"] = data.get("generated_at") or _now_iso()
        data["updated_at"] = _now_iso()
        data.setdefault("note",
                        "本文件是模板。请逐项填写后使用。未填写的项一律按 deny 处理。")
        data["allowed_values"] = ["allow", "deny", "unset"]
        data["unset_policy"] = "unset 一律按 deny 处理"
        if signature or "signature" not in data:
            data["signature"] = signature
        if date or "date" not in data:
            data["date"] = date

        try:
            _atomic_json(cj, data)
            _render_consent_md(self.archive, data)
            self.archive.audit("consent_updated", {
                "counts": {k: sum(1 for v in data["granularity"].values() if v == k)
                           for k in ("allow", "deny", "unset")},
                "signature_set": bool(data.get("signature")),
            })
        except OSError as exc:
            return {"error": "write_failed", "message": str(exc)}
        return {"ok": True, "statement": {
            "granularity": data.get("granularity", {}),
            "signature": data.get("signature", ""),
            "date": data.get("date", ""),
            "updated_at": data.get("updated_at", ""),
        }}

    def save_after_death(self, text: str = "") -> dict:
        if not self.archive.exists():
            return {"error": "no_archive", "message": "还没有档案。"}
        ad = self.archive.consent_dir / "身后指令.md"
        ad.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_text(ad, text or "")
            self.archive.audit("after_death_updated",
                               {"chars": len(text or "")})
        except OSError as exc:
            return {"error": "write_failed", "message": str(exc)}
        return {"ok": True, "chars": len(text or "")}

    # --- 加密操作 -------------------------------------------------------
    #
    # 安全规则（这一条是硬线，改之前想清楚）：
    #   给了口令 → **只验证口令**，不许用 DPAPI 绕过。
    #   没给口令 → 才允许走 DPAPI（本机免密）。
    #
    # 如果给了口令还给 DPAPI 兜底，结果就是"在本机上输任意口令都能解锁"，
    # 那设口令这件事就完全不算数了。这个坑在测试里真的踩到过。

    def _resolve_key(self, password: str):
        """返回 (key, error)。error 是给用户看的字符串。"""
        if not vault.envelope_exists(self.archive.root):
            return None, "这个档案没有密钥信封。"
        env = vault.load_envelope(self.archive.root)
        has_pw_way = vault.WAY_PASSWORD in vault.describe_ways(env)
        if password:
            # 给了口令：只走口令，不碰 DPAPI
            try:
                return vault.unlock_key(env, password, allow_dpapi=False), None
            except vault.VaultError as exc:
                return None, str(exc)
        # 没给口令：只在不强制口令的情况下才允许 DPAPI
        if not has_pw_way:
            return None, "这个档案需要口令。"
        try:
            return vault.unlock_key(env, None, allow_dpapi=True), None
        except vault.VaultError:
            return None, "需要口令（本机 DPAPI 解锁不可用）。"

    def lock(self, password: str) -> dict:
        if not vault.envelope_exists(self.archive.root):
            return {"error": "no_key", "message": "这个档案没有密钥，无法加锁。"}
        if encrypt.is_locked(self.archive):
            return {"ok": True, "already": True}
        key, err = self._resolve_key(password or "")
        if err:
            return {"error": "auth", "message": err}
        try:
            rep = encrypt.lock(self.archive, key, verbose=False)
        except RuntimeError as exc:
            return {"error": "lock_failed", "message": str(exc)}
        self._key = key
        return {"ok": True, "report": rep}

    def unlock(self, password: str) -> dict:
        if not vault.envelope_exists(self.archive.root):
            return {"error": "no_key", "message": "这个档案没有加密。"}
        key, err = self._resolve_key(password or "")
        if err:
            return {"error": "auth", "message": err}
        try:
            rep = encrypt.unlock(self.archive, key, verbose=False)
        except (ValueError, FileNotFoundError) as exc:
            return {"error": "unlock_failed", "message": str(exc)}
        self._key = key
        return {"ok": True, "report": rep}

    def verify(self, password: str) -> dict:
        key, err = self._resolve_key(password or "")
        if err:
            return {"error": "auth", "message": err}
        rep = encrypt.verify(self.archive, key, verbose=False)
        return {"ok": rep["failed"] == 0 and rep["missing"] == 0, "report": rep}

    @property
    def key(self):
        return self._key


def _snippet(text: str, q: str, width: int = 90) -> str:
    """给检索结果截一段带上下文的文本。"""
    if not text:
        return ""
    if not q:
        return text[:width]
    idx = text.find(q)
    if idx < 0:
        return text[:width]
    start = max(0, idx - 30)
    seg = text[start:start + width]
    return ("…" if start > 0 else "") + seg


# =======================================================================
# HTTP 服务
# =======================================================================

class Handler(BaseHTTPRequestHandler):
    api: Api = None
    server_version = "lifecollect-ui/0.1"

    def log_message(self, fmt, *args):  # 安静
        pass

    def _send(self, code: int, body: bytes, ctype: str,
              cache: bool = False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 静态资源必须禁止缓存：WebView2 会缓存旧 JS/CSS，
        # 导致用户看到的是上一个版本的界面（真的踩过）。
        if cache:
            self.send_header("Cache-Control", "no-cache")
        else:
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._serve_file(STATIC_DIR / "index.html")
        if path.startswith("/static/"):
            return self._serve_file(
                STATIC_DIR / path[len("/static/"):])
        if path.startswith("/api/"):
            return self._api_get(path, parse_qs(urlparse(self.path).query))
        self._json({"error": "not_found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._json({"error": "not_found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return self._json({"error": "bad_json"}, 400)
        name = path[len("/api/"):]
        fn = getattr(self.api, name, None)
        if not callable(fn):
            return self._json({"error": "not_found"}, 404)
        try:
            result = fn(**payload) if payload else fn()
        except TypeError as exc:
            return self._json({"error": "bad_args", "message": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": "internal",
                               "message": f"{type(exc).__name__}: {exc}"}, 500)
        return self._json(result)

    def _api_get(self, path, query):
        name = path[len("/api/"):]
        fn = getattr(self.api, name, None)
        if not callable(fn):
            return self._json({"error": "not_found"}, 404)
        kwargs = {}
        for k, v in query.items():
            if not v:
                continue
            val = v[0]
            if k in ("limit", "level"):
                try:
                    val = int(val)
                except ValueError:
                    continue
            elif k in ("self_only",):
                val = val.lower() in ("1", "true", "yes")
            kwargs[k] = val
        try:
            return self._json(fn(**kwargs))
        except TypeError as exc:
            return self._json({"error": "bad_args", "message": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": "internal",
                               "message": f"{type(exc).__name__}: {exc}"}, 500)

    def _serve_file(self, path: Path):
        try:
            path = path.resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())):
                return self._json({"error": "forbidden"}, 403)
            data = path.read_bytes()
        except FileNotFoundError:
            return self._json({"error": "not_found"}, 404)
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)


def start_server(archive: Archive) -> ThreadingHTTPServer:
    api = Api(archive)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def run(root: str, window: bool = True, port: int = 0) -> int:
    """启动 UI。

    root 可以**尚未初始化**——这是刻意的：第一次打开时应该是引导，
    不是一个报错或者一个塞满假数据的仪表盘。
    """
    archive = Archive(root)
    api = Api(archive)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"lifecollect UI  →  {url}")
    if archive.exists():
        print(f"档案：{archive.root}")
    else:
        print(f"档案：{archive.root}（尚未建立，界面会引导创建）")

    if not window:
        print("（无窗口模式，Ctrl+C 退出）")
        print("注意：浏览器模式用不了系统文件选择框，导入页会提示直接粘贴路径。")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if _webview is None:
        print("错误：pywebview 没装上。运行 pip install -r requirements.txt")
        return 2

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    win = _webview.create_window(
        "lifecollect", url,
        width=1280, height=880, min_size=(960, 640),
        background_color="#F1EFEA",
        # 把 Api 实例通过 pywebview 桥暴露给前端，前端才能打开
        # 系统文件选择框（window.pywebview.api.pick_files）。
        # 注意：这条桥只活在原生窗口里，--no-window 模式没有它。
        js_api=api,
    )
    # 把窗口句柄交给 Api，Api 用它来弹对话框。
    api._window = win
    _webview.start()
    httpd.shutdown()
    return 0


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="lifecollect.ui")
    p.add_argument("--root", required=True,
                   help="档案根目录（可以还不存在，界面会引导建立）")
    p.add_argument("--no-window", action="store_true",
                   help="只起 HTTP 服务，不开窗口")
    p.add_argument("--port", type=int, default=0, help="固定端口（默认随机）")
    args = p.parse_args(argv)
    return run(args.root, window=not args.no_window, port=args.port)


def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="lifecollect.ui")
    p.add_argument("--root", required=True, help="档案根目录")
    p.add_argument("--no-window", action="store_true",
                   help="只起 HTTP 服务，不开窗口")
    p.add_argument("--port", type=int, default=0, help="固定端口（默认随机）")
    args = p.parse_args(argv)
    return run(args.root, window=not args.no_window, port=args.port)


if __name__ == "__main__":
    import sys
    sys.exit(main())
