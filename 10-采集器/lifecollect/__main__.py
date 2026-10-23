"""CLI 入口：python -m lifecollect

第一件事是强制 stdout/stderr 用 UTF-8。
中文 Windows 上 Python 的 stdout 默认是 GBK（本机实测：
sys.stdout.encoding == 'gbk' 而 sys.getfilesystemencoding() == 'utf-8'），
不处理的话打印中文长文本会中途炸掉。
"""

from __future__ import annotations

import sys


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_force_utf8()

from .cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
