"""命令行入口。

第一版做成 CLI 而不是 GUI，是有意的：
    先用合成数据把整条链路（导入 → 标注 → 索引 → 呈现）跑通并测试，
    再套 pywebview。UI 建在没测过的地基上只会把 bug 藏起来。

加密命令单独一组：init / lock / unlock / verify / status / passwd / forget-dpapi。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import records as R
from .archive import Archive
from . import pipeline, store
from . import crypto, encrypt, prompts, vault


def _print_security_table() -> None:
    print("安全级别（选择时请同时看两栏）：\n")
    for lvl in R.SECURITY_LEVELS:
        info = R.SECURITY_TABLE[lvl]
        default = "  ← 默认" if lvl == R.SECURITY_DEFAULT else ""
        print(f"  {lvl}{default}")
        print(f"    密钥保护：{info['key_protection']}")
        print(f"    挡什么：  {info['blocks']}")
        print(f"    不挡什么：{info['does_not_block']}")
        print()


def _needs_encryption(level: str) -> bool:
    return level in ("standard", "paranoid")


def _bind_key(archive: Archive, password: str | None,
              interactive_ok: bool) -> bytes | None:
    """拿到主密钥。已经初始化过就直接解锁，否则提示创建。"""
    if not vault.envelope_exists(archive.root):
        return None
    env = vault.load_envelope(archive.root)
    return vault.unlock_key(env, password)


def cmd_init(args) -> int:
    archive = Archive(args.root)
    level = args.security_level

    # 防止重复初始化：重复 init 会生成新的主密钥，让旧档案永久打不开。
    # 判断"已有内容"而不是"有信封"——basic 级别没有信封，但同样不能重复初始化。
    if archive.root.exists() and any(archive.root.iterdir()):
        print(f"错误：{archive.root} 不是空目录，不能重复初始化。", file=sys.stderr)
        print("（重复初始化会让旧档案永久打不开。要重建请换一个空目录。）",
              file=sys.stderr)
        return 2
    if vault.envelope_exists(archive.root):
        print(f"错误：{archive.root} 已经有密钥信封了，不能重复初始化。",
              file=sys.stderr)
        print("（重复初始化会让旧档案永久打不开。要重建请换一个目录。）",
              file=sys.stderr)
        return 2

    password = None
    if _needs_encryption(level):
        print(f"即将初始化一个 `{level}` 级别的加密档案。")
        print("这一步会生成主密钥并用口令保护它。")
        print()
        info = R.SECURITY_TABLE[level]
        print(f"  ⚠ 不挡什么：{info['does_not_block']}")
        print()
        print("口令是你换电脑后唯一能打开档案的凭据。")
        print("忘记口令且 Windows 账户已重建，档案将永久不可恢复——")
        print("没有任何人（包括本程序的作者）能帮你找回。")
        print()
        try:
            if getattr(args, "password_stdin", False):
                # 脚本模式：从 stdin 读两行（口令 + 确认），与交互模式语义一致
                import sys as _sys
                pw1 = _sys.stdin.readline().rstrip("\r\n")
                pw2 = _sys.stdin.readline().rstrip("\r\n")
                if not pw1:
                    raise prompts.PromptError("从标准输入读到空口令。")
                if len(pw1) < 8:
                    raise prompts.PromptError(
                        "口令太短（至少 8 个字符）。")
                if pw1 != pw2:
                    raise prompts.PromptError("两次输入不一致，请重来。")
                password = pw1
            else:
                password = prompts.ask_new_password()
        except prompts.PromptError as exc:
            print(f"\n错误：{exc}", file=sys.stderr)
            return 2

    archive.init(security_level=level,
                 subject=args.subject or "",
                 owner=args.owner or "")

    if password:
        master_key = crypto.random_key()
        env = vault.create_envelope(master_key, password, level)
        vault.save_envelope(archive.root, env)
        ways = ", ".join(vault.describe_ways(env))
        archive.audit("key_created", {
            "security_level": level,
            "ways": vault.describe_ways(env),
            "key_fingerprint": crypto.key_fingerprint(master_key),
        })
        print(f"\n档案已初始化：{archive.root}")
        print(f"安全级别：{level}（密钥已生成，可用方式：{ways}）")
        print(f"密钥指纹：{crypto.key_fingerprint(master_key)}")
        print("   ↑ 这个指纹不泄密。将来核对'是不是同一把钥匙'时用它。")
    else:
        print(f"\n档案已初始化：{archive.root}")
        print("安全级别：basic（不加密）")

    info = R.SECURITY_TABLE[level]
    print(f"  挡什么：  {info['blocks']}")
    print(f"  不挡什么：{info['does_not_block']}")
    print()
    print("接下来：")
    print(f"  1. 读 {archive.doc_path}")
    print(f"  2. 填写 {archive.consent_dir}\\授权声明.md —— 未填写的项一律按禁止处理")
    print(f"  3. 导入材料：lifecollect import --root \"{archive.root}\" --source wechat <文件>")
    if _needs_encryption(level):
        print(f"  4. 导入完成后加锁：lifecollect lock --root \"{archive.root}\"")
    return 0


def cmd_import(args) -> int:
    archive = Archive(args.root)
    if not archive.exists():
        print(f"错误：{archive.root} 不是一个已初始化的档案。先执行 init。",
              file=sys.stderr)
        return 2

    self_ids = set(args.self or [])
    paths = [Path(p) for p in args.paths]

    # 先嗅探发送者，帮用户确认 --self 有没有写对。
    # 猜错发送者会让下游把别人的话当成档案主人的——这是必须让用户确认的一步。
    if not self_ids and not args.yes:
        from .ingest import parse_file
        for p in paths:
            if p.is_dir() or not p.exists():
                continue
            conv, note = parse_file(p, set(), args.source_level)
            if conv and len(conv):
                cands = pipeline.sniff_self_candidates(conv)
                if cands:
                    print(f"\n{p.name} 里出现的发送者（按条数）：")
                    for name, n in cands:
                        print(f"    {name}  ({n} 条)")
                    print("\n没有指定 --self，所有消息的方向会被标成 unknown。")
                    print("unknown 不会污染数据，但下游无法区分谁说的话。")
                    print(f'指定方式：--self "{cands[0][0]}"')
                    print("继续导入？(y/N) ", end="")
                    if sys.stdin.isatty():
                        if input().strip().lower() not in ("y", "yes"):
                            print("已取消。")
                            return 1
                    else:
                        print("（非交互环境，跳过确认）")
                    break

    print(f"\n导入 {len(paths)} 个路径 → 来源 {args.source}，级别 {args.source_level}")
    report = pipeline.import_paths(
        archive, paths, args.source, self_ids,
        source_level=args.source_level, media_subdir=args.media_subdir)

    print(f"\n完成：成功 {report['files_ok']} 个文件，"
          f"失败 {report['files_failed']} 个，"
          f"共 {report['messages']} 条消息，"
          f"登记媒体 {report['media_registered']} 个。")
    if report["failures"]:
        print("\n失败详情：")
        for f in report["failures"]:
            print(f"  {Path(f['file']).name}: {f['reason']}")
    if report["files_ok"]:
        print("\n下一步：lifecollect tag  →  lifecollect index  →  lifecollect view")
    return 0


def cmd_tag(args) -> int:
    archive = Archive(args.root)
    pipeline.tag_archive(archive)
    return 0


def cmd_index(args) -> int:
    archive = Archive(args.root)
    st = store.rebuild(archive)
    print(f"索引已重建：{st['records']} 条记录，{st['conversations']} 个会话")
    print(f"位置：{archive.db_path}")
    print("（这一层可以随时删掉重建，不影响档案完整性）")
    return 0


def cmd_stats(args) -> int:
    archive = Archive(args.root)
    st = store.stats(archive)
    ready = store.training_readiness(archive)
    if args.json:
        print(json.dumps({"stats": st, "readiness": ready},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"记录总数：{st['total']}    会话数：{st['conversations']}")
    a, b = st["time_range"]
    print(f"时间范围：{a or '（无）'} ~ {b or '（无）'}")
    if st["no_timestamp"]:
        print(f"⚠ 没有时间的记录：{st['no_timestamp']}")
    print()
    print("内容类型：")
    for k, n in st["by_kind"].items():
        print(f"  {k:<16} {n:>7}   对下游价值: {R.KIND_VALUE_NOTE.get(k,'')}")
    print()
    print("来源可信级别：")
    for lvl, n in st["by_level"].items():
        print(f"  {lvl}  {n:>7}   {R.SOURCE_LEVEL_DESCRIPTIONS.get(lvl,'')}")
    print()
    print(f"高价值材料：{ready['usable_chars']} 字符"
          f"（门槛 {ready['threshold']}，"
          f"{'达标' if ready['meets_threshold'] else '未达标'}）")
    print(f"  {ready['note']}")
    return 0


def cmd_search(args) -> int:
    archive = Archive(args.root)
    try:
        rows = store.search(archive, args.query, kind=args.kind,
                            source=args.source, limit=args.limit,
                            self_only=args.self_only)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if not rows:
        print("没有匹配。")
        return 0
    for r in rows:
        ts = (r["captured_at"] or "无时间")[:19]
        direction = {"self": "我", "other": "对方", "unknown": "?"}.get(
            r["direction"], "?")
        print(f"[{ts}] [{direction}] [{r['kind']}] {r['record_id']}")
        text = (r["text"] or "").replace("\n", " ")
        print(f"    {text[:200]}")
    print(f"\n共 {len(rows)} 条。")
    return 0


def cmd_view(args) -> int:
    archive = Archive(args.root)
    pipeline.write_view(archive)
    print(f"已生成人类可读清单：{archive.view_dir}")
    for f in sorted(archive.view_dir.glob("*.md")):
        print(f"  {f.name}")
    return 0


def cmd_info(args) -> int:
    archive = Archive(args.root)
    meta = archive.meta()
    if not meta:
        print(f"错误：{archive.root} 不是已初始化的档案。", file=sys.stderr)
        return 2
    print(f"档案：{archive.root}")
    for k in ("archive_format", "format_version", "standard",
              "security_level", "subject", "owner", "created_at", "updated_at"):
        if k in meta:
            print(f"  {k}: {meta[k]}")
    info = R.SECURITY_TABLE.get(meta.get("security_level", ""), {})
    if info:
        print(f"  挡什么：  {info['blocks']}")
        print(f"  不挡什么：{info['does_not_block']}")
    return 0


def cmd_levels(args) -> int:
    _print_security_table()
    print("在 init 时用 --security-level 指定。默认 standard。")
    print()
    print("关于口令：standard 和 paranoid 都需要设一个口令。")
    print("  只靠 Windows 账户（DPAPI）保护是不够的——换电脑或重装系统后就")
    print("  再也打不开了。口令是跨设备恢复的唯一保证。")
    return 0


# =======================================================================
# 加密命令
# =======================================================================

def _open_vault(args, prompt: str = "口令: ") -> tuple[Archive, bytes] | None:
    """统一的开档案流程。返回 None 表示已经打印了错误。"""
    archive = Archive(args.root)
    if not archive.exists():
        print(f"错误：{archive.root} 不是一个已初始化的档案。", file=sys.stderr)
        return None
    if not vault.envelope_exists(archive.root):
        print(f"错误：这个档案没有加密（没有密钥信封）。\n"
              f"如果要加密一个 basic 档案，请新建一个 standard/paranoid 档案"
              f"重新导入。\n"
              f"（不要在原地给已有档案补加密——那会让所有中间产物都处在"
              f"明文和密文的混合状态，容易出错。）", file=sys.stderr)
        return None

    env = vault.load_envelope(archive.root)
    password = None
    if getattr(args, "password_stdin", False):
        # 脚本模式：**只走口令路径**，不偷偷用 DPAPI。
        # 否则传错口令也会"成功"，所有自动化验证都会形同虚设。
        # （这个坑真的踩过：verify 用旧口令返回了 0，因为 DPAPI 免密成功了。）
        try:
            password = prompts.read_from_stdin()
        except prompts.PromptError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return None
        try:
            key = vault.unlock_key(env, password, allow_dpapi=False)
        except vault.VaultError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return None
        return archive, key

    # 交互模式：先试 DPAPI（免输口令）；失败才问口令
    try:
        key = vault.unlock_key(env, None, allow_dpapi=True)
        return archive, key
    except vault.VaultError:
        pass
    try:
        password = prompts.ask_password(prompt)
    except prompts.PromptError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return None

    try:
        key = vault.unlock_key(env, password, allow_dpapi=False)
    except vault.VaultError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return None
    return archive, key


def cmd_lock(args) -> int:
    archive = Archive(args.root)
    if not archive.exists():
        print(f"错误：{archive.root} 不是已初始化的档案。", file=sys.stderr)
        return 2
    if not vault.envelope_exists(archive.root):
        print("错误：这个档案没有密钥，无法加锁。", file=sys.stderr)
        print("basic 级别不加密。要加密请在 init 时选 standard 或 paranoid。",
              file=sys.stderr)
        return 2
    if encrypt.is_locked(archive):
        print("档案已经是锁定状态。")
        print(f"运行 lifecollect status --root \"{archive.root}\" 查看详情。")
        return 0

    opened = _open_vault(args, "口令（加锁前验证）: ")
    if opened is None:
        return 2
    archive, master_key = opened

    print(f"\n开始加密：{archive.root}")
    print("顺序是「加密 → 验证能解开 → 才删明文」。中途失败不会丢数据。\n")
    try:
        report = encrypt.lock(archive, master_key)
    except (RuntimeError, OSError) as exc:
        print(f"\n错误：{exc}", file=sys.stderr)
        return 1

    print(f"\n完成。")
    print(f"  文件数：{report['files']}")
    print(f"  明文字节：{report['bytes_in']:,}")
    print(f"  密文字节：{report['bytes_out']:,}"
          f"（膨胀 {report['bytes_out']/max(1,report['bytes_in']):.2%}）")
    print(f"  验证通过：{report['verified']} 个文件")
    print()
    print(f"  永远明文的：{', '.join(encrypt.ALWAYS_CLEAR)}")
    print()
    print("解锁：lifecollect unlock --root <目录>")
    print("校验：lifecollect verify --root <目录>")
    return 0


def cmd_unlock(args) -> int:
    opened = _open_vault(args, "口令（解锁）: ")
    if opened is None:
        return 2
    archive, master_key = opened

    st = encrypt.status(archive)
    if not st["locked"]:
        print("档案没有锁定，不需要解锁。")
        return 0

    print(f"\n开始解密：{archive.root}")
    try:
        report = encrypt.unlock(archive, master_key,
                                remove_ciphertext=args.remove_ciphertext)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"\n完成。解密 {report['files']} 个文件，"
          f"{report['bytes']:,} 字节")
    if report["missing"]:
        print(f"  ⚠ 缺失 {report['missing']} 个密文文件", file=sys.stderr)
    if report["failed"]:
        print(f"  ⚠ 失败 {report['failed']} 个文件", file=sys.stderr)
    if args.remove_ciphertext:
        print("  已按你的要求删除密文（不可逆）")
    else:
        print("  密文仍保留。确认无误后可加 --remove-ciphertext 清理。")
    return 0 if report["failed"] == 0 else 1


def cmd_verify(args) -> int:
    opened = _open_vault(args, "口令（校验）: ")
    if opened is None:
        return 2
    archive, master_key = opened

    try:
        report = encrypt.verify(archive, master_key, verbose=not args.quiet)
    except (FileNotFoundError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    print(f"\n校验结果：OK {report['ok']}  坏 {report['failed']}  缺失 {report['missing']}")
    if report["problems"]:
        print("\n问题清单：")
        for p in report["problems"]:
            print(f"  - {p}")
    return 0 if report["failed"] == 0 and report["missing"] == 0 else 1


def cmd_status(args) -> int:
    archive = Archive(args.root)
    if not archive.exists():
        print(f"错误：{archive.root} 不是已初始化的档案。", file=sys.stderr)
        return 2

    level = archive.security_level
    st = encrypt.status(archive)
    env = None
    if vault.envelope_exists(archive.root):
        try:
            env = vault.load_envelope(archive.root)
        except vault.VaultError as exc:
            print(f"警告：密钥信封读取失败：{exc}", file=sys.stderr)

    print(f"档案：{archive.root}")
    print(f"安全级别：{level}")
    info = R.SECURITY_TABLE.get(level, {})
    if info:
        print(f"  挡什么：  {info['blocks']}")
        print(f"  不挡什么：{info['does_not_block']}")
    print()
    print(f"锁定状态：{'是（有加密文件）' if st['locked'] else '否（全部是明文）'}")
    print(f"  密文文件：{st['encrypted_files']}")
    print(f"  明文文件：{st['plain_files']}")

    if args.verbose and st["plain_files"]:
        print("\n尚未加密的明文文件：")
        for p in st["plain_files"][:60]:
            print(f"  {p}")
        if len(st["plain_files"]) > 60:
            print(f"  ... 还有 {len(st['plain_files']) - 60} 个")

    print()
    print(f"永远明文（这是设计，不是遗漏）：")
    for p in st["always_clear"]:
        print(f"  {p}")
    return 0


def cmd_passwd(args) -> int:
    archive = Archive(args.root)
    if not vault.envelope_exists(archive.root):
        print("错误：这个档案没有密钥信封，不需要换口令。", file=sys.stderr)
        return 2

    env = vault.load_envelope(archive.root)

    # 脚本模式：stdin 依次给 当前口令 / 新口令 / 确认
    if getattr(args, "password_stdin", False):
        try:
            lines = sys.stdin.read().split("\n")
            lines = [ln.rstrip("\r") for ln in lines]
            old = lines[0] if len(lines) > 0 else ""
            new = lines[1] if len(lines) > 1 else ""
            confirm = lines[2] if len(lines) > 2 else ""
        except (OSError, UnicodeDecodeError) as exc:
            print(f"错误：读取标准输入失败: {exc}", file=sys.stderr)
            return 2
        if not old:
            print("错误：当前口令为空。", file=sys.stderr)
            return 2
        if len(new) < 8:
            print("错误：新口令太短（至少 8 个字符）。", file=sys.stderr)
            return 2
        if new != confirm:
            print("错误：两次输入的新口令不一致，请重来。", file=sys.stderr)
            return 2
    else:
        try:
            old = prompts.ask_password("当前口令: ")
            new = prompts.ask_new_password("新口令: ")
        except prompts.PromptError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

    # 先验证旧口令（不借助 DPAPI，否则旧口令错了也会"成功"）
    try:
        vault.unlock_key(env, old, allow_dpapi=False)
    except vault.VaultError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    try:
        vault.change_password(archive.root, old, new)
    except vault.VaultError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1

    archive.audit("password_changed", {
        "key_fingerprint": env.get("key_fingerprint"),
        "note": "只重新包裹主密钥，没有重新加密档案",
    })
    print("口令已更换。")
    print("注意：档案文件没有重新加密——只是换了包裹主密钥的口令。")
    print("      所以这个操作很快，也不会碰到数据。")
    return 0


def cmd_forget_dpapi(args) -> int:
    archive = Archive(args.root)
    if not vault.envelope_exists(archive.root):
        print("错误：这个档案没有密钥信封。", file=sys.stderr)
        return 2
    removed = vault.remove_dpapi_way(archive.root)
    if not removed:
        print("这个档案本来就没有 DPAPI 解锁方式。")
        return 0
    archive.audit("dpapi_way_removed", {})
    print("已删除 DPAPI 解锁方式。")
    print("现在只能用口令解锁。这适合'我要把档案拷到别的电脑'的场景。")
    print("⚠ 请务必确认你还记得口令——删掉 DPAPI 后本机也不再免密。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lifecollect",
        description="数字生命档案采集器（第一版，离线，仅依赖 cryptography 做加密）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_root(sp):
        sp.add_argument("--root", required=True, help="档案根目录")

    sp = sub.add_parser("init", help="初始化一个档案")
    add_root(sp)
    sp.add_argument("--security-level", default=R.SECURITY_DEFAULT,
                    choices=list(R.SECURITY_LEVELS))
    sp.add_argument("--subject", default="", help="档案主人")
    sp.add_argument("--owner", default="", help="档案所有者")
    sp.add_argument("--password-stdin", action="store_true",
                    help="从标准输入读口令（脚本用）")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("import", help="导入材料")
    add_root(sp)
    sp.add_argument("paths", nargs="+", help="文件或目录")
    sp.add_argument("--source", default=R.SOURCE_WECHAT, choices=list(R.SOURCES))
    sp.add_argument("--source-level", type=int, default=2, choices=[1, 2, 3, 4],
                    help="来源可信级别（见标准 §4）")
    sp.add_argument("--self", nargs="*", default=[],
                    help="档案主人在导出文件里的名字/昵称（可多个）")
    sp.add_argument("--media-subdir", default=None, help="媒体文件放哪个子目录")
    sp.add_argument("--yes", action="store_true", help="跳过发送者确认")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("tag", help="标注内容类型（可重跑）")
    add_root(sp)
    sp.set_defaults(func=cmd_tag)

    sp = sub.add_parser("index", help="重建检索索引")
    add_root(sp)
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("stats", help="统计")
    add_root(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("search", help="全文检索")
    add_root(sp)
    sp.add_argument("query")
    sp.add_argument("--kind", choices=list(R.KINDS))
    sp.add_argument("--source", choices=list(R.SOURCES))
    sp.add_argument("--self-only", action="store_true", help="只搜本人发的")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("view", help="生成人类可读清单")
    add_root(sp)
    sp.set_defaults(func=cmd_view)

    sp = sub.add_parser("info", help="查看档案元数据")
    add_root(sp)
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("levels", help="列出安全级别及其含义")
    sp.set_defaults(func=cmd_levels)

    # --- 加密命令 ---
    sp = sub.add_parser("lock", help="加密档案（standard/paranoid）")
    add_root(sp)
    sp.add_argument("--password-stdin", action="store_true",
                    help="从标准输入读口令（脚本用；注意口令会留在管道里）")
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser("unlock", help="解密档案")
    add_root(sp)
    sp.add_argument("--password-stdin", action="store_true")
    sp.add_argument("--remove-ciphertext", action="store_true",
                    help="解密后删除密文（不可逆）")
    sp.set_defaults(func=cmd_unlock)

    sp = sub.add_parser("verify", help="校验密文能用这把钥匙解开")
    add_root(sp)
    sp.add_argument("--password-stdin", action="store_true")
    sp.add_argument("--quiet", action="store_true", help="只输出汇总")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("status", help="查看加密状态（不需要口令）")
    add_root(sp)
    sp.add_argument("--verbose", action="store_true", help="列出未加密的明文文件")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("passwd", help="更换口令")
    add_root(sp)
    sp.add_argument("--password-stdin", action="store_true")
    sp.set_defaults(func=cmd_passwd)

    sp = sub.add_parser("forget-dpapi", help="删除 DPAPI 解锁方式（准备拷到别的电脑）")
    add_root(sp)
    sp.set_defaults(func=cmd_forget_dpapi)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except prompts.PromptError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except vault.VaultError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except crypto.CryptoError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
