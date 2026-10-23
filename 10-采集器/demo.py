"""生成演示档案。用合成数据跑完整条链路，产出可人工检查的目录。

跑完即删。目的：眼见为实，确认产出的档案真的"不依赖任何软件也能读"。
"""

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEMO = HERE / "_demo"

CSV = """时间,发送者,内容
2024-03-01 09:14:22,我,收到
2024-03-01 09:15:01,老王,方案我看了
2024-03-01 09:15:30,我,马上回你
2024-03-01 10:02:11,老王,我觉得这个方案其实没必要改，关键是成本
2024-03-01 10:03:40,我,我不同意，本质上问题在于排期不是成本
2024-03-01 10:04:02,我,唉……这事儿真是让人难受
2024-03-01 21:30:00,我,今天真的累死了！！！
2024-03-01 21:31:00,老王,[图片]
2024-03-02 08:00:00,我,[语音]
2024-03-02 08:01:00,我,晚安
2024-04-11 14:20:00,我,我一直在想，人到底为什么要在意别人的看法
2024-04-11 14:21:00,我,说到底，评价标准如果不是自己定的，那就永远没有尽头
"""

NOTE = """关于要不要换工作

我这半年一直在想这件事。表面上是钱的问题，其实不是。
真正让我犹豫的是，我担心自己其实没有想象中那么能干。
如果换个地方还是这样，那说明问题在我身上，不在公司。

我不想承认这一点，但我知道这是真的。
"""

LONG_NOTE = "\n\n".join(
    f"第{i}段随想：关于时间、关于选择、关于要不要回头。"
    f"我越来越觉得，人做的每一个决定，其实都是在替过去的自己收尾。"
    for i in range(1, 120)
)


def run(*args):
    cmd = [sys.executable, "-m", "lifecollect", *args]
    print("$ " + " ".join(str(a) for a in args[:4]) + (" ..." if len(args) > 4 else ""))
    r = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0:
        print("!! exit", r.returncode)
        if r.stderr:
            print(r.stderr.rstrip())
    return r


def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    src = DEMO / "源材料"
    src.mkdir(parents=True)
    (src / "聊天记录.csv").write_text(CSV, encoding="utf-8")
    (src / "随笔.txt").write_text(NOTE, encoding="utf-8")
    (src / "长文.txt").write_text(LONG_NOTE, encoding="utf-8")
    # 造一个假的语音文件，验证媒体登记
    (src / "voice").mkdir()
    (src / "voice" / "msg001.silk").write_bytes(b"\x02#!SILK_V3" + b"\x00" * 64)

    arch = DEMO / "档案"

    run("init", "--root", str(arch), "--security-level", "standard",
        "--subject", "演示档案")
    run("import", "--root", str(arch), "--source", "wechat",
        "--self", "我", "--yes", str(src / "聊天记录.csv"))
    run("import", "--root", str(arch), "--source", "documents",
        "--self", "我", "--yes", str(src / "随笔.txt"))
    run("import", "--root", str(arch), "--source", "documents",
        "--self", "我", "--yes", str(src / "长文.txt"))
    run("import", "--root", str(arch), "--source", "wechat",
        "--yes", str(src / "voice"))
    run("tag", "--root", str(arch))
    run("index", "--root", str(arch))
    run("stats", "--root", str(arch))
    run("view", "--root", str(arch))
    print()
    print("=== 检索测试（中文双字词）===")
    for q in ("成本", "本", "换工作", "随笔", "同意"):
        r = run("search", "--root", str(arch), q, "--limit", "3")
    print()
    print("=== 产出目录 ===")
    for p in sorted(arch.rglob("*")):
        rel = p.relative_to(arch)
        depth = len(rel.parts) - 1
        mark = "[D]" if p.is_dir() else "   "
        size = "" if p.is_dir() else f"  ({p.stat().st_size} B)"
        print("  " * depth + f"{mark} {rel.parts[-1]}{size}")


if __name__ == "__main__":
    main()
