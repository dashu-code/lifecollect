"""加密端到端演示。用合成数据跑完整流程，产出可人工检查的目录。

口令通过 stdin 传给子进程（演示脚本本身是非交互的）。
跑完即删。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "_demo_crypto"
PW = "demo-password-123"

CSV = """时间,发送者,内容
2024-03-01 10:02:11,老王,我觉得这个方案其实没必要改，关键是成本
2024-03-01 10:03:40,我,我不同意，本质上问题在于排期不是成本
2024-03-01 10:04:02,我,唉……这事儿真是让人难受
2024-03-01 21:30:00,我,今天真的累死了！！！
2024-03-02 08:00:00,我,[语音]
"""

NOTE = """关于要不要换工作

我这半年一直在想这件事。表面上是钱的问题，其实不是。
真正让我犹豫的是，我担心自己其实没有想象中那么能干。
如果换个地方还是这样，那说明问题在我身上，不在公司。
"""


def run(*args, stdin=None, show=True):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, "-m", "lifecollect", *[str(a) for a in args]]
    r = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env,
                       input=stdin if stdin is not None else "")
    if show and r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0:
        print(f"  !! exit={r.returncode}")
        if r.stderr:
            print("  stderr:", r.stderr.rstrip())
    return r


def tree(root: Path):
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        depth = len(rel.parts) - 1
        if p.is_dir():
            continue
        size = p.stat().st_size
        # 显示前 24 字节，让加密文件的魔数可见
        peek = ""
        if rel.parts[0] in ("10-原始", "20-派生", "30-索引", "40-呈现") \
                and p.suffix == ".lfc":
            with open(p, "rb") as fh:
                head = fh.read(4)
            peek = f"  [头部 {head!r}]"
        print("  " * depth + f"{rel.parts[-1]}  ({size} B){peek}")


def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    src = DEMO / "源材料"
    src.mkdir(parents=True)
    (src / "聊天记录.csv").write_text(CSV, encoding="utf-8")
    (src / "随笔.txt").write_text(NOTE, encoding="utf-8")
    arch = DEMO / "档案"

    print("=" * 70)
    print("1. 初始化 standard 级别加密档案")
    print("=" * 70)
    run("init", "--root", arch, "--security-level", "standard",
        "--subject", "演示", "--password-stdin", stdin=PW + "\n" + PW + "\n")

    print()
    print("=" * 70)
    print("2. 导入 + 标注 + 索引 + 呈现（这些操作在锁定前必须是明文）")
    print("=" * 70)
    run("import", "--root", arch, "--source", "wechat", "--self", "我",
        "--yes", src / "聊天记录.csv", show=False)
    run("import", "--root", arch, "--source", "documents", "--self", "我",
        "--yes", src / "随笔.txt", show=False)
    run("tag", "--root", arch, show=False)
    run("index", "--root", arch, show=False)
    run("view", "--root", arch, show=False)
    print("  完成（静默）")

    print()
    print("=" * 70)
    print("3. 加密之前的状态")
    print("=" * 70)
    run("status", "--root", arch, show=True)

    print()
    print("=" * 70)
    print("4. 加锁")
    print("=" * 70)
    run("lock", "--root", arch, "--password-stdin", stdin=PW + "\n")

    print()
    print("=" * 70)
    print("5. 锁定后的目录树")
    print("=" * 70)
    tree(arch)

    print()
    print("=" * 70)
    print("6. 锁定后的状态（不需要口令就能看）")
    print("=" * 70)
    run("status", "--root", arch, show=True)

    print()
    print("=" * 70)
    print("7. 关键验证：说明文件仍是明文，能用记事本打开")
    print("=" * 70)
    doc = arch / "00-说明.md"
    text = doc.read_text(encoding="utf-8")
    for line in text.split("\n"):
        if "安全级别" in line or "这是什么" in line or "挡什么" in line:
            print(f"  {line}")
    print(f"  （文件大小 {doc.stat().st_size} 字节）")

    print()
    print("=" * 70)
    print("8. 校验：密文能用这把钥匙解开吗")
    print("=" * 70)
    run("verify", "--root", arch, "--password-stdin", "--quiet",
        stdin=PW + "\n")

    print()
    print("=" * 70)
    print("9. 加密文件长什么样（前 300 字节，明文可读的头部）")
    print("=" * 70)
    lfc = next((arch / "10-原始").rglob("*.lfc"))
    data = lfc.read_bytes()
    print(f"  文件: {lfc.relative_to(arch)}")
    print(f"  总长: {len(data)} 字节")
    import struct
    (hlen,) = struct.unpack("<I", data[4:8])
    print(f"  魔数: {data[:4]!r}   头部长度: {hlen}")
    print("  头部 JSON（故意明文，二十年后要能看出这是什么）:")
    print("  " + data[8:8 + hlen].decode("utf-8"))
    print(f"  之后是 {len(data) - 8 - hlen} 字节密文")

    print()
    print("=" * 70)
    print("10. 篡改检测：改一个字节，验证必须失败")
    print("=" * 70)
    backup = data
    bad = bytearray(data)
    bad[-1] ^= 0xFF
    lfc.write_bytes(bytes(bad))
    r = run("verify", "--root", arch, "--password-stdin", "--quiet",
            stdin=PW + "\n", show=False)
    print(f"  篡改后 verify 退出码 = {r.returncode}（非 0 表示正确拒绝）")
    if r.stdout:
        for line in r.stdout.split("\n"):
            if "校验结果" in line or "问题" in line:
                print(f"  {line}")
    lfc.write_bytes(backup)

    print()
    print("=" * 70)
    print("11. 解锁（找回明文）")
    print("=" * 70)
    run("unlock", "--root", arch, "--password-stdin", stdin=PW + "\n")
    print()
    print("  解锁后能否照常检索：")
    r = run("search", "--root", arch, "成本", show=False)
    for line in (r.stdout or "").split("\n")[:4]:
        if line.strip():
            print(f"  {line}")

    print()
    print("=" * 70)
    print("12. 错误口令必须被拒绝")
    print("=" * 70)
    r = run("verify", "--root", arch, "--password-stdin", "--quiet",
            stdin="wrong-password\n", show=False)
    print(f"  错误口令 verify 退出码 = {r.returncode}（非 0 表示正确拒绝）")
    for line in (r.stderr or "").split("\n")[:3]:
        if line.strip():
            print(f"  {line}")


if __name__ == "__main__":
    main()
