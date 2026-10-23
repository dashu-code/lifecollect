# DLRS 核查备忘

> 核查对象：`D:\Downloads\DLRS-master.zip`（615.1 KB，LastWriteTime 2026-10-15 22:30）
> 本地解压位置（不入库）：`_reference/DLRS-master/`（本地解压位置，不入库）
> 核查日期：2026-10-15
> 核查人：DSH agent（本机实测，非引用）
> 核查方式：读取源文件 + 在本机运行其自带测试套件 + 体量统计

---

## 0. 先更正我自己的判断

| 轮次 | 我说过的话 | 修正后的说法 |
|---|---|---|
| 第 6 轮 | "我提到过 DLRS，刚才去核，仓库页取不到，命中的是一个医学影像数据集，**我无法核实那个项目**，引用它不严谨" | **我取不到页面是真的，但结论下错了。** 项目确实存在且体量很大。取不到是因为网络抓取失败，不是因为项目不存在。我当时应该只报告"取不到"，而不是隐含地质疑其存在性。 |
| 第 7 轮 | "要自己发明档案标准"（隐含） | **不必发明。** DLRS 已经定义了 `.life` 档案格式的 JSON Schema，且大部分设计比我的草案更细。方向应改为"对接"而不是"另立"。 |
| 第 8 轮 | 我把 `policies/*.md` 读成空壳 | **文件不是空的，是"提得对但极短"。** 见 §3.3，每个政策文件 2~3 行，内容是有效的政策声明，但不构成可执行规范。我第一次读出来是乱码（PowerShell 默认 GBK 解码 UTF-8 文件），据此得出"空壳"结论，属于工具误用导致的错误判断。 |

---

## 1. 项目实况（事实，可核实）

| 项 | 值 |
|---|---|
| 名称 | Digital Life Repository Standard (DLRS) |
| 版本 | v0.8-asset-architecture |
| 状态 | RFC 征求意见阶段（自述"早期开放标准草案"） |
| 许可证 | **MIT**（`LICENSE`，Copyright (c) 2026 DLRS Hub Contributors） |
| 自述完成度 | 约 82% |
| 核心交付 | `.life` **文件格式** + `.life` **运行时协议**（双标准） |
| 仓库结构 | `schemas/`（16 个 JSON Schema）、`docs/`（30+ 规范文档）、`tools/`（22 个测试 + 15 个工具）、`pipelines/`（6 条离线管线）、`runtime/`、`policies/`、`standards/`、`templates/`、`examples/` |
| 自述缺口 | **runtime 零实现**（推迟 v0.9+）；encrypted-mode 未实现；签名未实现；RBAC/ReBAC/ABAC 0%；C2PA/水印 0% |

### 1.1 它明确定义自己"不是"什么

引自 `README.md` 第 71–82 行：

> - ❌ **不是**"复活人类"或"克隆人格"的技术 —— `.life` 加载后产生的是 *AI 数字生命实例*，永远不等同于真人
> - ❌ **不是**无授权 / 无撤回机制的死后复现工具
> - ❌ **不是**法律合规的保证
> - ❌ **不是**永久存储解决方案 —— `.life` 必须有 `expires_at`，超期 MUST 拒绝挂载
> - ❌ **不是**成熟的生产系统 —— 本仓库当前阶段交付的是规范 + Schema + 示例打包器；**reference runtime 实现尚未交付**

**判断**：这个自我定位和我们前面讨论的方向一致，不是那种过度承诺的项目。

---

## 2. 本机实测：测试套件跑通情况

**环境**：`C:\Users\37151\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（Python 3.12.14）
**前置动作**：按其 `tools/requirements.txt` 安装了 `jsonschema`（4.26.0）
**命令**：`PYTHONPATH=<repo root> python tools/<test>.py`

**结果：22 个测试，10 通过 / 12 失败。**

### 2.1 通过（10）

```
test_registry.py                       exit=0  all 14 passed
test_memory_atom_schema.py             exit=0  all 17 case(s) passed
test_entity_graph_schema.py            exit=0  all 22 case(s) passed
test_hosted_api_policy.py              exit=0  all assertions passed
test_life_package_schema.py            exit=0  all 64 case(s) passed
test_genesis_schema.py                 exit=0  37 cases, 0 failures
test_lifecycle_schema.py               exit=0  42 cases, 0 failures
test_binding_schema.py                 exit=0  63 cases, 0 failures
test_tier_schema.py                    exit=0  81 cases, 0 failures
test_derived_asset_schema.py           exit=0  all 8 case(s) passed
```

**Schema 层是真的能跑**：`.life` 包结构、Genesis、Lifecycle、Binding、Tier 五组 Schema 合计 287 个用例，全部通过。这一层可以信任。

### 2.2 失败（12）——按根因分类，不要混为一谈

**根因 A：Windows 路径分隔符（占 6 个，非逻辑错误）**

```
ValueError: output path 'derived\text\voice_master.clean.txt' must start with derived/text/
ValueError: output path 'derived\vectorization\voice_master.index.json' must start with derived/vectorization/
ValueError: output path 'derived\memory_atoms\diary.atoms.jsonl' must start with derived/memory_atoms/
ValueError: output path 'derived\knowledge_graph\diary.nodes.jsonl' must start with derived/knowledge_graph/
```

定位：`tools/validate_pipelines.py:43` 用 `spec.output_pointer_template.startswith(expected_prefix)` 做前缀校验，而 Windows 上 `os.path.join` 产生反斜杠，永远匹配不上正斜杠前缀。

影响：**6 条离线管线（text / vectorization / memory_atoms / knowledge_graph / moderation / asr）在 Windows 上全部无法运行。**

**根因 B：UTF-8 文件被按 GBK 读（占 2 个，非逻辑错误）**

```
test_runtime_scaffold.py  ERROR: UnicodeDecodeError: 'gbk' codec can't decode byte 0x94
test_pipelines.py         UnicodeDecodeError: 'gbk' codec can't decode byte 0x90
```

定位：测试脚本读文件时未显式指定 `encoding="utf-8"`，在中文 Windows 上命中 GBK 默认编码。

**根因 C：依赖 bash / 子进程（占 3 个，环境问题）**

```
test_minimal_life_package.py  CalledProcessError: ['bash', '...build_life.sh'] returned non-zero
test_memory_graph_demo.py     --- stderr ---
test_descriptor_audit_bridge.py  bridge/chain: expected 2 events, got 0
```

**根因 D：真实逻辑失败（1 个，平台无关）**

```
test_runtime_verify.py  FAIL  test_lifectl_run_stage1_pass_then_pending
                       1 of 18 runtime-verify tests failed
```

这是唯一的、与操作系统无关的逻辑失败。说明 `runtime/cli/lifectl.py` 的阶段推进状态机有缺陷。

### 2.3 为什么这些 bug 没被上游发现

`.github/workflows/validate.yml` 全部 4 个 job 都是 `runs-on: ubuntu-latest`，**CI 只在 Linux 跑**。

**推论（推断）**：Windows 上的路径和编码问题从未进入过他们的 CI 视野。任何在 Windows 上对接 DLRS 的人，第一件事就是踩这个坑。

---

## 3. 内容实质核查

### 3.1 Schema 层（强）

`schemas/` 16 个文件，覆盖 `.life` 包、清单、指针、同意、审计事件、派生资产、记忆原子、实体图节点/边、Genesis、Lifecycle、Binding、Tier、公共档案、hosted-API 策略、注册表条目。

287 个用例通过，且包含反例测试（bi-conditional、路径不允许 `..`、`forbidden_uses` 必须含 fraud/political/explicit、`expires_at > created_at`）。

`schemas/life-package.schema.json` 单文件 21 KB —— 这是整个仓库最有价值的部分。

### 3.2 政策与标准层（弱）

| 目录 | 文件数 | 单文件最大 | 性质 |
|---|---|---|---|
| `policies/` | 9 | 187 字节 | 每个 2~3 行的政策声明 |
| `standards/dlrs/v0.2/` | 10 | 1995 字节（`spec.md`） | 提纲级 |

**实测体量**（`policies/` 全部 9 个文件）：

```
deceased-persons.md               110 字节
takedown.md                       110 字节
cross-border.md                   116 字节
minors.md                         120 字节
privacy.md                        137 字节
acceptable-use.md                 149 字节
data-retention.md                 155 字节
watermarking-and-disclosure.md    164 字节
risk-levels.md                    187 字节
```

**全文示例**（`policies/deceased-persons.md`，注意：读该文件必须显式用 UTF-8）：

> # Deceased Persons Policy
>
> 逝者档案必须声明继承/纪念化策略。争议时优先冻结运行态。

### 3.3 这里有一个真实的不一致（值得记录）

`docs/COLLECTION_STANDARD.md` 第 48–50 行把它写成**硬规则**：

> - **Deceased subjects**: archives where `subject.status="deceased"` **MUST** follow `policies/deceased-persons.md` and **MUST** set `inheritance_policy.default_action_on_death`.

而 `policies/deceased-persons.md` 总长 110 字节、一句话。

**判断**：这不是"文档缺失"，而是**"MUST 指向了一个不具备约束力的文档"**。政策里提的 `inheritance_policy.default_action_on_death` 在 manifest schema 里是有定义的（这一层可机器校验），但政策本身把"谁有权在逝后决定什么"这个真正的问题留白了 —— 这正是我们前几轮讨论的那个缺口。

### 3.4 政策与标准未被 Schema 化

`rights-and-consent.md` 列了 12 类同意粒度（存储 / 结构化处理 / 文本人格构建 / 语音克隆 / 视频头像克隆 / 3D avatar 构建 / 模型微调 / 公开索引 / 商业使用 / 跨境处理 / 研究使用 / 纪念化运行）。

**判断（推断）**：这份清单的内容和我们的授权线设计高度重合，但它是散文形式，不是可校验的结构。Schema 层里有 `consent.schema.json` 和 `separate_biometric_consent` 标志，但**12 类粒度并没有全部落到 schema 的枚举里**。

---

## 4. 与本研究项目的接口关系

### 4.1 原始层架构：存在映射，但不是一对一

`standards/dlrs/v0.2/spec.md` 第 15–23 行定义四层分离：

| DLRS 层 | GitHub 中保存什么 | 我们的对应层 |
|---|---|---|
| Raw | pointer、hash、region、sensitivity | 原始层（但 DLRS 存指针，我们存文件） |
| Derived | 摘要、schema、低风险派生样例 | 索引层 / 派生层 |
| Runtime | prompt、adapter、voice profile、avatar config | （我们不做） |
| Public Output | 标识、权限、输出记录、C2PA 指针 | （我们不做） |

**关键差异**：DLRS 假设归档在 **Git 仓库**里、原始素材在**外部对象存储**（S3 等）里，仓库只存指针。我们的目标是**桌面应用 + 本地文件夹**，对非技术用户操作"对象存储 + 指针"是不现实的。

**判断**：这个差异必须在设计里正面处理，不能假装不存在。可能的处理方式是——采集器生成 DLRS 兼容的指针结构，同时把文件放在本地约定目录，指针里的 URI 指向本地路径；将来需要打包成 `.life` 时再走他们的 builder。

### 4.2 接口缺口：DLRS 不覆盖"采"

`docs/GAP_ANALYSIS.md` 第 16 行自述：

> | 数据采集规范 | 40% | 80% | 85% | 85% | 85% | **85%** | 100% |

85% 指的是**采集规范文档**（最低分辨率、时长、字数门槛）。而 `tools/` 和 `pipelines/` 里**没有任何从个人设备或社交平台提取素材的工具**。

在本仓库 `docs/` 全目录 grep `chat export|导出|微信|WeChat|collector`，只命中 4 处，全部是把 "chat exports" 当作**已存在的输入**来假设：

```
docs\COLLECTION_STANDARD.md:122:  | Source | Self-authored: writings, emails, chat exports |
docs\FAQ.md:198:                  - 访问权（数据导出）
docs\community\consent-model-feedback.md:419:  - 提供数据导出功能
docs\community\consent-model-feedback.md:496:  - ✅ 必须提供数据导出
```

**结论（事实）**：DLRS 假设你**已经**把材料整理好了。它做的是"整理好之后怎么打包、授权、运行"。

**我们要做的那一块，是它假设存在但从未实现的那一块。** 这不是竞争关系，是上下游关系。

### 4.3 与"用户自选托管"的一致性

用户在本次会话中明确要求：**方案倾向 A（秘密共享 + 制度托管），但决定权留给用户**。

DLRS 侧已有的对应字段：
- `consent.withdrawal_endpoint`（必填，撤回入口）
- `inheritance_policy.default_action_on_death`（enum，manifest schema 内）
- `policies/deceased-persons.md`（110 字节，见 §3.3）

**判断**：DLRS 给了"撤回"和"继承动作"的**字段**，没给"密钥怎么在无本人协助下传给继承人"的**机制**。我们的加密与托管层填的是这个洞，不会和它冲突。

---

## 5. 建议的对接方式（供决策，不代替决策）

三条路，事实后果如下：

| 方案 | 做法 | 后果 |
|---|---|---|
| **依赖** | 直接 `pip` 依赖或用其 schema 校验产物 | MIT 允许；但仓库是 RFC 草案、schema 可能变；Windows 管线跑不起来（见 §2.2 根因 A） |
| **对接** | 自己做采集器，输出**兼容** DLRS 指针结构；不引入其代码 | 解耦，不受其 bug 和版本变动影响；需要我们抄一份 schema 或做兼容层 |
| **参与** | 在上游提 issue/PR，修 Windows 路径 bug、补采集层 | 影响力最大；但要接受上游节奏，且投入不小 |

**待决策**：选哪条。这个决定会影响采集器的输出格式设计。

---

## 6. 本机环境注意事项（实测踩到）

1. **PowerShell 默认按 GBK 解码 UTF-8 文件。** `Get-Content <UTF-8 中文文件>` 会输出乱码。必须加 `-Encoding UTF8`。
   - 本次核查中我因此误判 `policies/` 为空壳，已更正（见 §0）。
2. **Python 在中文 Windows 上默认编码也是 GBK。** 所有 `open()` 必须显式 `encoding="utf-8"`。DLRS 自己的测试就栽在这里（§2.2 根因 B）。
   - 这条对我们的采集器是**硬性工程约束**：任何读写文本的代码都必须显式指定编码。
3. 本机已装 `jsonschema 4.26.0` 到 DSH 主运行时（为跑 DLRS 测试而装）。
