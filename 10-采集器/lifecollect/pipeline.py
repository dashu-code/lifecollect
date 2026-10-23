"""采集流程：文件 → 归一化 → 标注 → 写成档案记录。

设计要点：
    1. **导入不删除、不修改用户原文件**，只复制。
    2. **不假设时间**。抠不出时间就写 None，并在统计里单列，
       不拿"导入时间"冒充"消息时间"。
    3. **不假设发送者是谁**。用户必须自己指定 --self，
       否则 direction 一律 unknown —— 猜错会让下游把别人的话当成档案主人的。
    4. 重跑同一个会话是幂等的：按 conv_id 覆盖，不产生重复。
"""

from __future__ import annotations

from pathlib import Path

from . import records as R
from . import tagging
from .archive import Archive, read_jsonl, write_jsonl, now_iso
from .ingest import parse_file, iter_media_files, SUPPORTED_EXTENSIONS


def make_record_id(conv_id: str, idx: int) -> str:
    return f"{conv_id}#{idx:06d}"


def sniff_self_candidates(conv: R.Conversation, top: int = 8) -> list[tuple[str, int]]:
    """列出会话里出现最多的发送者，帮用户确定 --self。"""
    counts: dict[str, int] = {}
    for m in conv.messages:
        s = (m.sender or "").strip()
        if s:
            counts[s] = counts.get(s, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:top]


def _build_records(conv: R.Conversation, source: str, source_level: int,
                   rel_raw_path: str | None) -> list[dict]:
    out: list[dict] = []
    is_ai = source == R.SOURCE_AI_ASSISTED
    for idx, msg in enumerate(conv.messages):
        extra = dict(msg.extra or {})
        if is_ai:
            # 逐条打标记，而不是只靠源文件的标题行。
            # 一条记录被单独抽出来用时，这个标记必须跟着它走——
            # 否则下游拿到一段 AI 文字，看不出它不是本人写的。
            extra["ai_drafted"] = True
        out.append({
            "record_id": make_record_id(conv.conv_id, idx),
            "conv_id": conv.conv_id,
            "source": source,
            "source_level": source_level,
            "captured_at": msg.ts,
            "imported_at": now_iso(),
            "kind": R.KIND_UNKNOWN,          # 由 tag 阶段填写
            "tag_confidence": "",
            "tag_rule": "",
            "sender": msg.sender or "",
            "direction": msg.direction,
            "text": msg.text,
            "media_hint": msg.media_hint or "",
            "sensitivity": "S2_CONFIDENTIAL",
            # 聊天记录天然包含第三方内容，默认标 1；自撰文本可在后续修正
            "third_party_present": source not in (R.SOURCE_DOCUMENTS, R.SOURCE_MANUAL),
            # 这一整份材料是不是 AI 起草的。逐条冗余存储，是为了让
            # 单条记录脱离上下文也知道自己的身份。
            "ai_drafted": is_ai,
            "sha256": R.sha256_text(msg.text) if msg.text else "",
            "raw_path": rel_raw_path or "",
            "derived_path": "",
            "extra": extra,
        })
    return out


def import_paths(archive: Archive, paths: list[Path], source: str,
                 self_identifiers: set[str], source_level: int = 2,
                 media_subdir: str | None = None, verbose: bool = True) -> dict:
    """导入一批文件。返回统计。"""
    if source not in R.SOURCES:
        raise ValueError(f"未知来源: {source}（可选: {', '.join(R.SOURCES)}）")

    convs_path = archive.conversations_path
    recs_path = archive.records_path

    existing_convs = {c["conv_id"]: c for c in read_jsonl(convs_path)}
    existing_recs = list(read_jsonl(recs_path))
    replaced_conv_ids: set[str] = set()

    report = {"files_ok": 0, "files_failed": 0, "messages": 0,
              "media_registered": 0, "failures": [], "media": []}

    for p in paths:
        p = Path(p)
        # 目录：登记媒体文件 + 尝试解析里面的可解析文件
        if p.is_dir():
            for mf, mkind in iter_media_files(p):
                dst = archive.import_file(mf, source, media_subdir or "media")
                archive.audit("media_registered", {
                    "media_kind": mkind, "raw_path": archive.rel(dst),
                    "sha256": R.sha256_file(dst),
                })
                report["media_registered"] += 1
                report["media"].append({"kind": mkind, "path": archive.rel(dst)})
            candidates = [q for q in sorted(p.rglob("*"))
                          if q.is_file() and q.suffix.lower() in SUPPORTED_EXTENSIONS]
            if verbose and candidates:
                print(f"  目录 {p.name}: 登记媒体 {report['media_registered']} 个，"
                      f"待解析文件 {len(candidates)} 个")
            paths_to_parse = candidates
        else:
            paths_to_parse = [p]

        for f in paths_to_parse:
            conv, note = parse_file(f, self_identifiers, source_level)
            if conv is None:
                report["files_failed"] += 1
                report["failures"].append({"file": str(f), "reason": note})
                if verbose:
                    print(f"  [失败] {f.name}: {note}")
                continue

            if len(conv) == 0:
                report["files_failed"] += 1
                report["failures"].append(
                    {"file": str(f), "reason": "解析成功但没有任何消息"})
                if verbose:
                    print(f"  [空]   {f.name}: 解析器没读出内容")
                continue

            dst = archive.import_file(f, source, media_subdir)
            rel = archive.rel(dst)
            recs = _build_records(conv, source, source_level, rel)

            if conv.conv_id in existing_convs:
                replaced_conv_ids.add(conv.conv_id)
                report["replaced"] = report.get("replaced", 0) + 1
            existing_recs = [r for r in existing_recs
                             if r.get("conv_id") != conv.conv_id]
            existing_recs.extend(recs)

            times = [m.ts for m in conv.messages if m.ts]
            ts_known = len(times)
            existing_convs[conv.conv_id] = {
                "conv_id": conv.conv_id,
                "title": conv.title,
                "source": source,
                "source_level": source_level,
                "record_count": len(recs),
                "first_ts": min(times) if times else None,
                "last_ts": max(times) if times else None,
                "raw_path": rel,
                "imported_at": now_iso(),
                "timestamps_known": ts_known,
                "timestamps_missing": len(recs) - ts_known,
                "adapter_note": note,
            }

            report["files_ok"] += 1
            report["messages"] += len(recs)
            if verbose:
                warn = ""
                if ts_known < len(recs):
                    warn = f"  ⚠ {len(recs) - ts_known} 条没有时间"
                print(f"  [OK]   {f.name}: {len(recs)} 条{warn}")

            archive.audit("imported", {
                "file": rel, "source": source, "source_level": source_level,
                "messages": len(recs), "timestamps_missing": len(recs) - ts_known,
                "adapter_note": note,
            })

    write_jsonl(convs_path, existing_convs.values())
    write_jsonl(recs_path, existing_recs)
    archive.update_meta()
    archive.audit("import_finished", report)
    return report


def tag_archive(archive: Archive, verbose: bool = True) -> dict:
    """对全部记录重新标注。标注是可重跑的——规则改了直接重跑。"""
    recs = list(read_jsonl(archive.records_path))
    if not recs:
        raise FileNotFoundError(
            f"没有记录可标注: {archive.records_path}\n请先执行 import。")

    counts: dict[str, int] = {}
    conf_counts: dict[str, int] = {}
    for r in recs:
        msg = R.Message(
            ts=r.get("captured_at"),
            sender=r.get("sender", ""),
            direction=r.get("direction", R.DIRECTION_UNKNOWN),
            text=r.get("text", ""),
            media_hint=r.get("media_hint") or None,
        )
        res = tagging.tag_message(msg, r.get("source", ""))
        r["kind"] = res.kind
        r["tag_confidence"] = res.confidence
        r["tag_rule"] = res.rule
        counts[res.kind] = counts.get(res.kind, 0) + 1
        conf_counts[res.confidence] = conf_counts.get(res.confidence, 0) + 1

    write_jsonl(archive.records_path, recs)
    archive.update_meta()
    archive.audit("tagged", {"by_kind": counts, "by_confidence": conf_counts})

    if verbose:
        total = len(recs)
        print(f"已标注 {total} 条：")
        for k in R.KINDS:
            if counts.get(k):
                pct = counts[k] / total * 100
                note = R.KIND_VALUE_NOTE.get(k, "")
                print(f"  {k:<16} {counts[k]:>7}  {pct:>5.1f}%   对下游价值: {note}")
        weak = conf_counts.get(R.CONFIDENCE_WEAK, 0)
        if weak:
            print(f"\n  其中 {weak} 条（{weak/total*100:.1f}%）是兜底判定，"
                  f"置信度 weak —— 标成 unknown 而不是硬塞进某一类。")
    return {"by_kind": counts, "by_confidence": conf_counts}


# --- 呈现层 -------------------------------------------------------------

def write_view(archive: Archive) -> dict:
    """生成人类可读的清单。这些文件不需要工具就能看。"""
    from . import store

    st = store.stats(archive)
    ready = store.training_readiness(archive)
    recs = list(read_jsonl(archive.records_path))

    # 1. 总览
    lines = [
        "# 档案总览",
        "",
        f"生成时间：{now_iso()}",
        "",
        "## 数量",
        "",
        f"- 记录总数：{st['total']}",
        f"- 会话数：{st['conversations']}",
        f"- 时间范围：{st['time_range'][0] or '（无）'} ~ {st['time_range'][1] or '（无）'}",
        f"- **没有时间的记录：{st['no_timestamp']}** ← 时间精度影响下游可用性",
        f"- 本人发的消息：{st['self_messages']}",
        f"- 含第三方内容的记录：{st['third_party_present']} ← 外发前必须脱敏",
        "",
        "## 内容类型分布",
        "",
        "| 类型 | 数量 | 对下游价值 |",
        "|---|---|---|",
    ]
    for k, n in st["by_kind"].items():
        lines.append(f"| `{k}` | {n} | {R.KIND_VALUE_NOTE.get(k, '')} |")

    lines += [
        "",
        "## 来源可信级别分布",
        "",
        "| 级别 | 数量 | 含义 |",
        "|---|---|---|",
    ]
    for lvl, n in st["by_level"].items():
        lines.append(f"| {lvl} | {n} | {R.SOURCE_LEVEL_DESCRIPTIONS.get(lvl, '')} |")

    lines += [
        "",
        "## 标注置信度",
        "",
        "| 置信度 | 数量 |",
        "|---|---|",
    ]
    for c, n in st["by_confidence"].items():
        lines.append(f"| `{c}` | {n} |")

    lines += [
        "",
        "## 可用于训练的材料量估算",
        "",
        f"- 全部字符数：{ready['total_chars']}",
        f"- **高价值部分（自撰 + 观点 + 情绪）：{ready['usable_chars']}**",
        f"- 其中本人发出的：{ready['self_directed_chars']}",
        f"- 门槛（取自 DLRS COLLECTION_STANDARD）：{ready['threshold']}",
        f"- 是否达标：**{'是' if ready['meets_threshold'] else '否'}**",
        "",
        f"> {ready['note']}",
    ]
    (archive.view_dir / "总览.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 2. 会话清单
    conv_lines = ["# 会话清单", "", "| 会话 | 来源 | 级别 | 条数 | 时间范围 | 无时间 | 原始文件 |",
                  "|---|---|---|---|---|---|---|"]

    def _fmt_range(c: dict) -> str:
        """没时间的会话不要显示成 '~'——那看着像坏了。"""
        a = (c.get("first_ts") or "")[:10]
        b = (c.get("last_ts") or "")[:10]
        if not a and not b:
            return "—"
        if a and b and a != b:
            return f"{a} ~ {b}"
        return a or b

    for c in sorted(read_jsonl(archive.conversations_path),
                    key=lambda x: (x.get("last_ts") or ""), reverse=True):
        conv_lines.append(
            f"| {c.get('title','')} | {c.get('source','')} | "
            f"{c.get('source_level','')} | {c.get('record_count',0)} | "
            f"{_fmt_range(c)} | "
            f"{c.get('timestamps_missing',0)} | `{c.get('raw_path','')}` |")
    (archive.view_dir / "会话清单.md").write_text(
        "\n".join(conv_lines) + "\n", encoding="utf-8")

    # 3. 高价值样本（给未来的下游工具看，也给人看）
    high = [r for r in recs if r.get("kind") in
            (R.KIND_AUTHORED, R.KIND_CHAT_OPINION, R.KIND_CHAT_EMOTION)]
    high.sort(key=lambda r: len(r.get("text") or ""), reverse=True)
    sample_lines = [
        "# 高价值样本",
        "",
        "按长度排序的自撰 / 观点 / 情绪类材料。",
        "**这些是最能反映说话方式的段落。** 闲聊和事务性内容不在这里。",
        "",
        f"共 {len(high)} 条，下面列出前 100 条。",
        "",
        "---",
        "",
    ]
    for r in high[:100]:
        sample_lines.append(
            f"## {r.get('record_id')}  `{r.get('kind')}`")
        sample_lines.append("")
        sample_lines.append(f"- 时间：{r.get('captured_at') or '（无）'}")
        sample_lines.append(f"- 标注依据：{r.get('tag_rule')} "
                            f"（置信度 {r.get('tag_confidence')}）")
        sample_lines.append(f"- 来源：{r.get('source')} 级别 {r.get('source_level')}")
        sample_lines.append("")
        sample_lines.append("```text")
        sample_lines.append((r.get("text") or "").strip())
        sample_lines.append("```")
        sample_lines.append("")
    (archive.view_dir / "高价值样本.md").write_text(
        "\n".join(sample_lines) + "\n", encoding="utf-8")

    # 4. 待处理清单（诚实列出问题）
    issues = ["# 待处理问题", "",
              "这些问题不解决，档案的可用性会受限。", ""]
    if st["no_timestamp"]:
        issues += [f"## {st['no_timestamp']} 条记录没有时间", "",
                   "时间精度影响下游对材料的排序和取舍。",
                   "常见原因：导出文件里没有时间列，或时间格式无法识别。", ""]
    weak = st["by_confidence"].get(R.CONFIDENCE_WEAK, 0)
    if weak:
        issues += [f"## {weak} 条标注置信度为 weak", "",
                   "这些是兜底判定，没能命中任何明确规则。",
                   "它们被标为 `unknown` 而不是硬塞进某一类——"
                   "宁可不分类，也不给错误的分类。",
                   "可以人工复核后手工修正。", ""]
    if st["third_party_present"]:
        issues += [f"## {st['third_party_present']} 条含第三方内容", "",
                   "聊天记录天然包含对方的私人信息。",
                   "**任何对外发布或上传云端前必须脱敏。**", ""]
    if not ready["meets_threshold"]:
        issues += ["## 高价值材料不足", "",
                   f"当前 {ready['usable_chars']} 字符，"
                   f"门槛是 {ready['threshold']}。",
                   "继续采集，或把已有的长文/笔记导入。", ""]
    if not issues[3:]:
        issues.append("（暂无）")
    (archive.view_dir / "待处理问题.md").write_text(
        "\n".join(issues) + "\n", encoding="utf-8")

    archive.update_meta()
    return {"view_files": 4}
