"""档案的目录结构与读写。

设计约束（来自 00-标准/档案标准-v0.1.md）：
    1. 权威性在文件上，不在数据库上。索引层坏了能重建。
    2. 00-说明.md 和 01-授权/ 永远明文 —— 否则会做出一个
       没人知道是什么、也没人知道谁能用的加密包，比不加密更糟。
    3. 所有文本 IO 显式 encoding="utf-8"。
       中文 Windows 默认 GBK，这一条是本机实测踩到的坑。
    4. 内部路径一律用正斜杠存储，避免 DLRS 那个
       "Windows 反斜杠 vs 斜杠前缀校验" 的错误。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import records as R

LIFECOL_VERSION = "0.1.0"
STANDARD_VERSION = "档案标准-v0.1"

DIR_DOC = "00-说明.md"
DIR_CONSENT = "01-授权"
DIR_RAW = "10-原始"
DIR_DERIVED = "20-派生"
DIR_INDEX = "30-索引"
DIR_VIEW = "40-呈现"
DIR_AUDIT = "90-审计"

SUBDIRS = (DIR_CONSENT, DIR_RAW, DIR_DERIVED, DIR_INDEX, DIR_VIEW, DIR_AUDIT)

RECORDS_NAME = "records.jsonl"
CONVERSATIONS_NAME = "conversations.jsonl"
INDEX_DB = "archive.db"
AUDIT_LOG = "events.jsonl"
META_NAME = "_meta.json"
CONSENT_MD = "授权声明.md"
CONSENT_JSON = "授权声明.json"
AFTER_DEATH_MD = "身后指令.md"
KEY_CUSTODY_JSON = "密钥托管.json"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写：先写临时文件再替换，避免中途崩溃留下半个文件。"""
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


def _atomic_write_json(path: Path, obj) -> None:
    _atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def append_jsonl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path):
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                n += 1
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return n


class Archive:
    """一个档案根目录。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    # --- 路径 -----------------------------------------------------------
    @property
    def doc_path(self) -> Path:
        return self.root / DIR_DOC

    @property
    def consent_dir(self) -> Path:
        return self.root / DIR_CONSENT

    @property
    def raw_dir(self) -> Path:
        return self.root / DIR_RAW

    @property
    def derived_dir(self) -> Path:
        return self.root / DIR_DERIVED

    @property
    def index_dir(self) -> Path:
        return self.root / DIR_INDEX

    @property
    def view_dir(self) -> Path:
        return self.root / DIR_VIEW

    @property
    def audit_dir(self) -> Path:
        return self.root / DIR_AUDIT

    @property
    def records_path(self) -> Path:
        return self.derived_dir / RECORDS_NAME

    @property
    def conversations_path(self) -> Path:
        return self.derived_dir / CONVERSATIONS_NAME

    @property
    def db_path(self) -> Path:
        return self.index_dir / INDEX_DB

    @property
    def audit_path(self) -> Path:
        return self.audit_dir / AUDIT_LOG

    @property
    def meta_path(self) -> Path:
        return self.root / META_NAME

    def source_dir(self, source: str) -> Path:
        return self.raw_dir / source

    def rel(self, path: Path) -> str:
        """转成相对档案根目录的 POSIX 路径，用于元数据存储。"""
        return path.resolve().relative_to(self.root).as_posix()

    def exists(self) -> bool:
        return self.meta_path.exists()

    # --- 初始化 ---------------------------------------------------------
    def init(self, security_level: str = R.SECURITY_DEFAULT,
             subject: str = "", owner: str = "") -> None:
        if security_level not in R.SECURITY_LEVELS:
            raise ValueError(f"未知安全级别: {security_level}")
        self.root.mkdir(parents=True, exist_ok=True)
        for d in SUBDIRS:
            (self.root / d).mkdir(parents=True, exist_ok=True)
        (self.raw_dir / R.SOURCE_LOOSE).mkdir(parents=True, exist_ok=True)

        existed = self.meta_path.exists()
        meta = read_json(self.meta_path, default={}) or {}
        meta.update({
            "archive_format": "lifecollect",
            "format_version": LIFECOL_VERSION,
            "standard": STANDARD_VERSION,
            "security_level": security_level,
            "subject": subject,
            "owner": owner,
            "created_at": meta.get("created_at") or now_iso(),
            "updated_at": now_iso(),
            "tool_versions": meta.get("tool_versions", {}),
        })
        _atomic_write_json(self.meta_path, meta)

        self._write_doc()
        self._ensure_consent_stubs()
        if not existed:
            self.audit("archive_created", {"security_level": security_level})
        else:
            self.audit("archive_updated", {"security_level": security_level})

    # --- 说明文件：永远明文 -------------------------------------------------
    def _write_doc(self) -> None:
        meta = read_json(self.meta_path, default={}) or {}
        level = meta.get("security_level", R.SECURITY_DEFAULT)
        info = R.SECURITY_TABLE.get(level, {})
        subject = meta.get("subject") or "（未填写）"
        created = meta.get("created_at", "")

        doc = f"""# 这是什么

这是一个**数字生命档案**。它按 `{STANDARD_VERSION}` 组织，由 lifecollect
工具采集生成。

- **档案主人**：{subject}
- **档案创建时间**：{created}
- **档案格式版本**：lifecollect {LIFECOL_VERSION}
- **遵循标准**：{STANDARD_VERSION}

## 怎么读

这份档案不依赖任何软件。用文件管理器打开，用记事本就能看。

| 目录 | 里面是什么 |
|---|---|
| `{DIR_CONSENT}/` | **授权与身后指令。任何使用本档案的人必须先读这里。** |
| `{DIR_RAW}/` | 原始素材。**永不修改、永不删除**——它是唯一的权威来源。 |
| `{DIR_DERIVED}/` | 从原始素材派生的可读文本和记录。**坏了可以重新生成。** |
| `{DIR_INDEX}/` | 检索用的数据库。**可以整个删掉，不影响档案完整性。** |
| `{DIR_VIEW}/` | 给人看的清单、时间线、样本。 |
| `{DIR_AUDIT}/` | 操作日志，只追加不修改。 |

**关键区别**：`{DIR_RAW}/` 是权威，`{DIR_INDEX}/` 是可抛弃的缓存。
即使数据库损坏、即使生成它的软件已经不存在，原始素材和授权文件依然完整。

## 使用须知

1. **先读 `{DIR_CONSENT}/`**，确认档案主人在授权声明里允许了什么、禁止了什么。
2. 用本档案生成的一切内容，**必须显著标明为 AI 合成内容**。
3. 原始素材里包含**第三方的私人内容**（聊天记录天然如此）。
   任何对外发布或上传云端前必须脱敏。

## 加密状态

**当前安全级别：`{level}`**

- **挡什么**：{info.get("blocks", "（未知）")}
- **不挡什么**：{info.get("does_not_block", "（未知）")}
- **密钥保护方式**：{info.get("key_protection", "（未知）")}

### 永远明文的文件（这是设计，不是遗漏）

| 文件 | 为什么必须明文 |
|---|---|
| `00-说明.md`（本文件） | 否则没人知道这个档案是什么 |
| `01-授权/` | 否则没人知道谁有权使用它 |
| `90-审计/` | 锁操作本身要写日志，加密它会导致流程不收敛 |
| `密钥信封.json` | 解密所需的公开参数（不包含密钥本身） |
| `加密清单.json` | 记录哪些文件被加密了，解谜索引用 |
| `_meta.json` | 档案元数据 |

> 本文件本身**永远是明文**。否则没有人能知道这个档案是什么、
> 也没人知道谁有权使用它——那比不加密更糟。

### 加密后的文件长什么样

被加密的文件带 `.lfc` 后缀，例如 `records.jsonl.lfc`。
每个 `.lfc` 文件的开头是一段**明文 JSON 头**，写着加密算法、KDF 参数、
原始文件路径和长度。这段头不需要密钥就能读——二十年后没有这个程序，
一个懂密码学的人也能照着它把文件解开。

## 如果打不开

如果除了上表列的明文文件之外，其他文件都带 `.lfc` 后缀且无法读取，
说明档案处于锁定状态，需要密钥。**密钥的恢复方式见
`{DIR_CONSENT}/{KEY_CUSTODY_JSON}`。**

如果连 `01-授权/` 都打不开，说明档案结构被破坏——请勿尝试用其他工具
"修复"。原始层一旦被改写，档案的可信度就永久受损。
"""
        _atomic_write_text(self.doc_path, doc)

    # --- 授权文件桩 -------------------------------------------------------
    def _ensure_consent_stubs(self) -> None:
        cj = self.consent_dir / CONSENT_JSON
        if not cj.exists():
            _atomic_write_json(cj, {
                "standard": STANDARD_VERSION,
                "generated_at": now_iso(),
                "note": "本文件是模板。请逐项填写后使用。未填写的项一律按 deny 处理。",
                "granularity": {
                    "存储": "unset",
                    "结构化处理": "unset",
                    "文本人格构建": "unset",
                    "语音克隆": "unset",
                    "视频头像克隆": "unset",
                    "3D_avatar构建": "unset",
                    "模型微调": "unset",
                    "公开索引": "unset",
                    "商业使用": "unset",
                    "跨境处理": "unset",
                    "研究使用": "unset",
                    "纪念化运行": "unset",
                },
                "allowed_values": ["allow", "deny", "unset"],
                "unset_policy": "unset 一律按 deny 处理",
            })

        cm = self.consent_dir / CONSENT_MD
        if not cm.exists():
            _atomic_write_text(cm, f"""# 授权声明

> **本文件是模板。在填写完成前，本档案不应被用于任何目的。**

档案主人：____________________

## 一、允许与禁止

下表逐项标明允许范围。留空视为**禁止**。

| 项目 | 允许 / 禁止 | 备注 |
|---|---|---|
| 存储 | | |
| 结构化处理 | | |
| 文本人格构建 | | |
| 语音克隆 | | |
| 视频头像克隆 | | |
| 3D avatar 构建 | | |
| 模型微调 | | |
| 公开索引 | | |
| 商业使用 | | |
| 跨境处理 | | |
| 研究使用 | | |
| 纪念化运行 | | |

## 二、标注义务

使用本档案生成的一切内容，**必须显著标明为 AI 合成内容**。

这不是建议。任何使用者都必须遵守。

## 三、撤回

档案主人可以随时撤回上述任何一项授权。撤回后，
使用者必须停止相应行为并删除由此产生的产物。

## 四、签名

签名：____________________

日期：____________________

---

机器可读版本见同目录的 `{CONSENT_JSON}`。两者内容必须一致。
""")

        ad = self.consent_dir / AFTER_DEATH_MD
        if not ad.exists():
            _atomic_write_text(ad, """# 身后指令

> **本文件是模板。它回答一个只有你能回答的问题：**
> **你去世以后，谁有权决定这份档案怎么被使用、什么时候关掉。**

如果你不写，答案就是"没有人有明确授权"——
使用者会各自解释，而你不会在场。

## 一、谁有权决定

| 角色 | 姓名 | 联系方式 | 权限范围 |
|---|---|---|---|
| 执行人 | | | 全权 |
| 备位执行人 | | | |
| 家属 | | | |

## 二、你去世后，档案默认怎么处理

（例如：允许家属只读访问；禁止生成新的对话；保留 N 年后关闭。）

## 三、运行终止条件

什么情况下必须停止运行这份档案？

## 四、争议时怎么办

出现争议时，优先冻结运行状态，而不是继续运行。

---

密钥的恢复方式见同目录的 `{KEY_CUSTODY_JSON}`。
""")

        kc = self.consent_dir / KEY_CUSTODY_JSON
        if not kc.exists():
            _atomic_write_json(kc, {
                "standard": STANDARD_VERSION,
                "generated_at": now_iso(),
                "note": "本文件是模板。密钥托管方式由档案主人选择。",
                "scheme": "unset",
                "available_schemes": {
                    "A": {
                        "name": "Shamir 秘密共享 + 制度托管",
                        "mechanism": "密钥拆成 N 份，任意 M 份可还原；份额分给律所/公证处/信任的人",
                        "consequence": "单一受托方背叛无效；需要凑够份额才能还原",
                        "tendency": "倾向默认",
                    },
                    "B": {
                        "name": "商业密码管理器的紧急访问",
                        "mechanism": "依赖第三方密码管理器的继承功能",
                        "consequence": "省事；但依赖第三方公司存续",
                    },
                    "C": {
                        "name": "单一受托人",
                        "mechanism": "把密钥交给一个人",
                        "consequence": "简单；但单一主体可背叛，且依赖该人",
                    },
                },
                "rejected_schemes": {
                    "人生经历选择题": (
                        "已评估并否掉。理由：(1) 答案存在于被保护的档案内部，"
                        "读完档案即可答对；(2) 把密钥安全降到'认识他的人'的水平；"
                        "(3) 正确率阈值无法同时满足'家人答得对'和'外人答不对'；"
                        "(4) '就一两次机会'不构成保护——攻击者会直接拿走加密文件"
                        "离线尝试，不会使用界面。"
                    ),
                },
                "shares": [],
                "min_shares_required": None,
                "recovery_instructions": "",
            })

    # --- 审计 ------------------------------------------------------------
    def audit(self, event: str, payload: dict | None = None) -> None:
        append_jsonl(self.audit_path, {
            "ts": now_iso(),
            "event": event,
            "tool": f"lifecollect/{LIFECOL_VERSION}",
            "payload": payload or {},
        })

    # --- 元数据 ----------------------------------------------------------
    def update_meta(self, **kwargs) -> None:
        meta = read_json(self.meta_path, default={}) or {}
        meta.update(kwargs)
        meta["updated_at"] = now_iso()
        _atomic_write_json(self.meta_path, meta)

    def meta(self) -> dict:
        return read_json(self.meta_path, default={}) or {}

    @property
    def security_level(self) -> str:
        return self.meta().get("security_level", R.SECURITY_DEFAULT)

    @property
    def envelope_path(self) -> Path:
        from .vault import ENVELOPE_NAME
        return self.root / ENVELOPE_NAME

    def import_file(self, src: Path, source: str, subdir: str | None = None) -> Path:
        """把原始文件复制进档案。返回目标路径。

        注意：复制而非移动。用户的原始文件不动。
        """
        src = Path(src)
        target_dir = self.source_dir(source)
        if subdir:
            target_dir = target_dir / subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / src.name
        if dst.exists():
            stem, suf = dst.stem, dst.suffix
            i = 2
            while dst.exists():
                dst = target_dir / f"{stem}__{i}{suf}"
                i += 1
        shutil.copy2(src, dst)
        return dst
