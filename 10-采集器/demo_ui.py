"""生成 UI 演示档案。用合成数据，供人工检查和截图。跑完可保留。"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEMO = HERE / "_demo_ui"
PW = "demo-password-123"

CSV = """时间,发送者,内容
2024-03-01 09:14:22,我,收到
2024-03-01 09:15:01,老王,方案我看了，整体方向没问题
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
2024-04-11 15:02:00,老王,你这个想法我理解，但现实没那么简单
2024-04-11 15:03:00,我,我知道现实不简单，我只是不想假装它简单
2024-04-12 09:00:00,我,今天看了个纪录片，讲一个老头在山上住了三十年
2024-04-12 09:01:00,老王,好看吗
2024-04-12 09:02:00,我,说不上好看，但看完之后我想了很久
2024-05-20 20:10:00,我,我妈今天打电话来问我要不要回家吃饭
2024-05-20 20:11:00,我,我说好，然后她说她做了排骨
2024-05-20 20:12:00,我,不知道为什么，这句话让我有点想哭
2024-06-01 11:00:00,老王,[文件]
2024-06-01 11:05:00,我,这个文件我看了，第三页有个数字对不上
2024-06-01 11:06:00,我,你核对一下，别到时候又返工
"""

NOTE = """关于要不要换工作

我这半年一直在想这件事。表面上是钱的问题，其实不是。
真正让我犹豫的是，我担心自己其实没有想象中那么能干。
如果换个地方还是这样，那说明问题在我身上，不在公司。

我不想承认这一点，但我知道这是真的。

人最难的不是做决定，是承认自己做决定的理由。
我一直告诉自己是公司的问题，其实有一半是我自己不敢往前走。
"""

LONG = "\n\n".join(
    f"第{i}段随想：关于时间、关于选择、关于要不要回头。"
    f"我越来越觉得，人做的每一个决定，其实都是在替过去的自己收尾。"
    f"有些话当时没说，后来就再也没机会说了。"
    for i in range(1, 80)
)


def run(*args, stdin=""):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-m", "lifecollect", *[str(a) for a in args]],
                       cwd=str(HERE), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, input=stdin)
    if r.returncode != 0:
        print("FAIL:", r.stdout[-300:], r.stderr[-300:])
    return r


def main():
    if DEMO.exists():
        shutil.rmtree(DEMO)
    src = DEMO / "源材料"
    src.mkdir(parents=True)
    (src / "聊天记录.csv").write_text(CSV, encoding="utf-8")
    (src / "随笔.txt").write_text(NOTE, encoding="utf-8")
    (src / "长文.txt").write_text(LONG, encoding="utf-8")
    arch = DEMO / "档案"

    run("init", "--root", arch, "--security-level", "standard",
        "--subject", "林某", "--password-stdin", stdin=PW + "\n" + PW + "\n")
    run("import", "--root", arch, "--source", "wechat", "--self", "我",
        "--yes", src / "聊天记录.csv")
    run("import", "--root", arch, "--source", "documents", "--self", "我",
        "--yes", src / "随笔.txt")
    run("import", "--root", arch, "--source", "documents", "--self", "我",
        "--yes", src / "长文.txt")
    run("tag", "--root", arch)
    run("index", "--root", arch)
    run("view", "--root", arch)
    print("演示档案:", arch)


if __name__ == "__main__":
    main()
