"""口令输入与确认。

三条约束：

  1. **非交互环境不能卡住**。stdin 不是终端时直接报错，
     而不是傻等——脚本和管道里卡住是最难排查的失败方式。

  2. **确认要分清**。init 时输两次；解锁时输一次。
     错误提示不能区分"口令错"和"文件被篡改"——这两种情况在
     认证加密下本来就无法区分，假装能区分会误导用户。

  3. **口令不落盘、不进日志、不进审计**。审计里只记密钥指纹前几位。
"""

from __future__ import annotations

import getpass
import sys


class PromptError(Exception):
    pass


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def ask_password(prompt: str = "口令: ", allow_empty: bool = False) -> str:
    if not _interactive():
        raise PromptError(
            "当前环境不是交互终端，无法读取口令。\n"
            "请在终端里运行，或使用 --password-stdin 从标准输入读取"
            "（注意：管道传递口令会留在 shell 历史里，不推荐）。")
    try:
        pw = getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        raise PromptError("已取消。") from None
    if not pw and not allow_empty:
        raise PromptError("口令不能为空。")
    return pw


def ask_new_password(prompt: str = "设置口令: ") -> str:
    """设置新口令。要求两次一致，并给出最低强度提示。"""
    pw1 = ask_password(prompt)
    if len(pw1) < 8:
        raise PromptError(
            "口令太短（至少 8 个字符）。\n"
            "这个口令是你在换电脑后唯一能打开档案的凭据——"
            "丢了就永远打不开，所以请用一个你记得住、但别人猜不到的口令。")
    pw2 = ask_password("再输一次确认: ")
    if pw1 != pw2:
        raise PromptError("两次输入不一致，请重来。")
    return pw1


def read_from_stdin() -> str:
    """从标准输入读一行。给 --password-stdin 用。"""
    try:
        line = sys.stdin.readline()
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(f"读取标准输入失败: {exc}") from exc
    pw = line.rstrip("\r\n")
    if not pw:
        raise PromptError("从标准输入读到空口令。")
    return pw


def confirm_or_password(args, prompt: str) -> str:
    """统一入口：优先 --password-stdin，其次交互输入。"""
    if getattr(args, "password_stdin", False):
        return read_from_stdin()
    return ask_password(prompt)
