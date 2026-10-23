/* lifecollect UI — 前端

   设计立场：打开先是一份手册，不是仪表盘。
   手册讲清「这是什么 → 为什么 → 怎么做 → 做完能看到什么」，
   数据在用户自己走完流程之后才出现，不预先塞满编造的东西。 */

"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

async function api(name, params) {
  const r = await fetch("/api/" + name, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params || {}),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
async function apiGet(name, query) {
  const qs = new URLSearchParams(query || {}).toString();
  const r = await fetch("/api/" + name + (qs ? "?" + qs : ""));
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString("en-US"));
const fmtBytes = (n) => {
  if (n == null) return "—";
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(2) + " MB";
};
const fmtTs = (ts) => !ts ? "无时间"
  : String(ts).replace("T", " ").replace(/([+-]\d{2}:?\d{2}|Z)$/, "");

const KIND_LABEL = {
  authored: "自撰", chat_opinion: "观点", chat_emotion: "情绪",
  chat_media: "媒体", chat_casual: "闲聊", third_party: "他人",
  ai_drafted: "AI 起草", unknown: "未定",
};
const KIND_NOTE = {
  authored: "最高", chat_opinion: "高", chat_emotion: "高", chat_media: "高",
  chat_casual: "低", third_party: "零", ai_drafted: "不可当本人语料", unknown: "未定",
};
const KIND_CLASS = {
  authored: "high", chat_opinion: "high", chat_emotion: "high",
  chat_media: "high", chat_casual: "low", third_party: "low",
  ai_drafted: "low", unknown: "low",
};
const DIR_LABEL = { self: "本人", other: "对方", unknown: "未知" };

/* ── Markdown 渲染 ───────────────────────────────────────────────────── */

function inlineMd(s) {
  return s
    .replace(/`([^`]+)`/g, (_, c) => "<code>" + esc(c) + "</code>")
    .replace(/\*\*([^*]+)\*\*/g, (_, c) => "<strong>" + esc(c) + "</strong>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g,
      (_, t, u) => '<a href="' + esc(u) + '" target="_blank" rel="noreferrer">' + esc(t) + "</a>");
}

function renderMarkdown(md) {
  const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0, inList = null;
  const close = () => { if (inList) { out.push("</" + inList + ">"); inList = null; } };

  while (i < lines.length) {
    const line = lines[i].replace(/\s+$/, "");

    // 原生 HTML 块（<details> 折叠等）：原样放行，不转义。
    // 手册用它把技术细节折起来——默认不显示，点开才看。
    if (/^<details[\s>]/.test(line)) {
      close();
      const buf = [];
      while (i < lines.length) {
        buf.push(lines[i]);
        const isClose = /^<\/details>\s*$/.test(lines[i]);
        i++;
        if (isClose) break;
      }
      out.push(buf.join("\n"));
      continue;
    }

    if (/^```/.test(line)) {
      close();
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++;
      out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>");
      continue;
    }

    if (/^\|/.test(line) && i + 1 < lines.length && /^\|[\s:|-]+\|?$/.test(lines[i + 1])) {
      close();
      const cells = (ln) => ln.split("|").slice(1, -1).map((c) => c.trim());
      const head = cells(line); i += 2;
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) { rows.push(cells(lines[i])); i++; }
      out.push("<table><thead><tr>" + head.map((c) => "<th>" + inlineMd(c) + "</th>").join("") +
        "</tr></thead><tbody>" + rows.map((r) => "<tr>" +
        r.map((c) => "<td>" + inlineMd(c) + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      continue;
    }

    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) { close(); const lv = h[1].length;
      out.push("<h" + lv + ">" + inlineMd(h[2]) + "</h" + lv + ">"); i++; continue; }

    if (/^>\s?/.test(line)) {
      close(); const buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, "")); i++;
      }
      out.push("<blockquote><p>" + inlineMd(buf.join(" ")) + "</p></blockquote>");
      continue;
    }

    const ul = /^[-*]\s+(.*)$/.exec(line);
    const ol = /^\d+\.\s+(.*)$/.exec(line);
    if (ul || ol) {
      const tag = ul ? "ul" : "ol";
      if (inList !== tag) { close(); out.push("<" + tag + ">"); inList = tag; }
      out.push("<li>" + inlineMd((ul || ol)[1]) + "</li>"); i++; continue;
    }

    if (line.trim() === "") { close(); i++; continue; }
    close(); out.push("<p>" + inlineMd(line) + "</p>"); i++;
  }
  close();
  return out.join("\n");
}

/* ── 状态 ────────────────────────────────────────────────────────────── */

const state = {
  config: null,
  overview: null,
  view: "manual",
  chapter: 0,
  done: new Set(),
  recordsFilter: { kind: "", direction: "" },
  searchKind: "",
};

/* ── 手册内容 ────────────────────────────────────────────────────────── */

const CHAPTERS = [
  {
    id: "what",
    title: "这是什么",
    raw: `这是一个**数字生命档案**工具。它做的事情只有一件：

> 把散落在微信、笔记、照片、邮件里的材料，收拢成一套**不依赖任何软件、二十年后仍然能读**的档案。

它不是"克隆人"的工具。它不训练模型、不克隆声音、不生成任何合成内容——那些是后面的事，
而且是你自己的决定。它只负责**把材料留住**。

### 为什么需要它

现有工具都从"材料已经整理好了"这一步开始。现实中所有人卡在第一步：

- 材料散在多个 App 里，导不出来，或者导出来是一堆格式各异的文件
- 材料**单向衰减**——人在世时可采，人走之后账号被回收、手机被重置、记录被删
- 原始素材里**没有**"这条是闲聊、那条是观点"的信息，而下游需要它

### 做完会得到什么

一个文件夹。用记事本就能打开。里面按权威性分七层——最权威的是原始素材，
最可抛弃的是检索索引。**即使生成它的软件哪天不存在了，档案依然是完整的。**

这一章不需要动手操作。读完点"下一章"即可。`,
  },
  {
    id: "create",
    title: "建立一个档案",
    raw: `档案是一个普通文件夹。你指定路径，它按约定生成七个目录。

### 要做的决定：安全级别

| 级别 | 挡什么 | **不挡什么** |
|---|---|---|
| basic | 顺手翻看、误删 | 拿到文件的人。**等于没有加密保护** |
| standard | 磁盘被单独拿走 | 能接触已解锁机器的人 |
| paranoid | 上述 + 离线暴力破解 | **密钥丢失即永久不可读** |

**standard 也要设口令的原因**：只靠 Windows 账户（DPAPI）保护是不够的。
Windows 账户一旦重建（换主板、重装系统），密钥就取不回来了。
口令是跨设备恢复的唯一保证。

口令记在心里，或者写在你信得过的地方。**忘记口令、且 Windows 账户已重建，档案将永久打不开——
没有任何人（包括作者）能帮你找回。**`,
    render: () => (state.config && state.config.initialized)
      ? renderChapterDone("档案已经建立了。你可以直接进下一章。",
          '<a class="btn" href="#overview">看看档案结构</a>')
      : renderCreateForm(),
  },
  {
    id: "import",
    title: "导入材料",
    raw: `### 先理解一件事：材料是有等级的

不是所有材料对下游都有同样的价值。

| 材料 | 作用 | 说明 |
|---|---|---|
| **文字**（聊天、笔记、邮件） | 学说话方式、用词、句式 | **最有用的一层** |
| **语音**（语音消息、录音） | 音色、语气、情绪 | 微信语音消息性价比最高 |
| **视频/照片** | 数字人外形 | 最不重要，加工也最贵 |

### 两个必须知道的陷阱

**一、闲聊会稀释风格信号。** "收到"、"几点到"占聊天记录的大头，它们会淹没真正
有信息量的段落。所以导入之后要标注——这是下一章做的事。

**二、不要猜发送者。** 导入时你要指出哪个名字是"你自己"。不填的话，所有消息的
方向会标成"未定"——这是有意的。**"不知道是谁"和"知道是对方"是两回事**，
猜错会让下游把别人的话当成档案主人的。

### 现在有界面了

不用命令行。左边栏点「导入材料」，三步：

1. 选材料是什么（微信聊天 / 笔记文章 / 邮件 / 照片 / 其他）
2. 选文件或文件夹（会打开系统文件框）
3. 点一下哪个名字是你

导入完它会**自动标注、建索引、重写清单**，然后直接跳到记录页看结果。`,
    render: () => {
      const has = state.overview && state.overview.stats
        && state.overview.stats.total > 0;
      if (has) {
        return renderChapterDone(
          `档案里已经有 ${fmtNum(state.overview.stats.total)} 条记录了。可以继续导入，或直接进下一章。`,
          '<a class="btn btn-solid" href="#import">继续导入材料</a>' +
          '<a class="btn" href="#records">看看记录</a>');
      }
      return renderChapterBody(`
        <div class="note-block">
          <div class="kicker">现在还没有材料</div>
          <p><strong>方式一：导入你自己的真实数据</strong>（推荐）。点下面的按钮，
          三步走完即可。全部在你的电脑上处理，不上传任何地方。</p>
          <div class="btn-row">
            <a class="btn btn-solid" href="#import">去导入我的材料</a>
          </div>
          <p style="margin-top:24px"><strong>方式二：先用示例数据走一遍流程</strong>。
          装一小份编造的示例——文件带「示例-」前缀，随时可以整个删掉。</p>
          <div class="btn-row">
            <button class="btn" id="load-sample">装示例数据</button>
          </div>
          <div class="action-log" id="sample-log"></div>
        </div>`);
    },
  },

  {
    id: "tag",
    title: "标注：认出真正有用的部分",
    raw: `标注回答一个问题：**这条材料对下游值不值得用？**

工具把它分成七类。每一类都带**判定规则**和**置信度**：

| 类型 | 对下游价值 | 判定依据 |
|---|---|---|
| 自撰 | 最高 | 来源是你自己写的 |
| 观点 | 高 | 观点词（我觉得、本质上、关键是……）+ 足够长度 |
| 情绪 | 高 | 情绪词、标点爆发、省略号、语气词 |
| 媒体 | 高 | 语音/图片/视频消息 |
| 闲聊 | 低 | 短消息、常用语 |
| 他人 | 零 | 别人写给你的 |
| 未定 | 未知 | **兜底——没命中规则就照实说不知道** |

### 一个刻意的设计：宁可说"不知道"

命不中任何规则的，标成"未定"，置信度标成 \`weak\`。
**不硬塞进某一类。** 因为错误的分类比没有分类更糟——它会骗过下游工具。

### 操作

\`\`\`powershell
python -m lifecollect tag --root "你的档案路径"
\`\`\`

标注可以**反复重跑**：规则改了直接再跑一遍，不会碰原始层。`,
    render: () => {
      const s = state.overview && state.overview.stats;
      if (!s) return renderChapterDone("还没有数据可以标注。回到上一章先导入材料。");
      const rows = Object.entries(s.by_kind || {})
        .sort((a, b) => b[1] - a[1])
        .map(([k, n]) => `<tr><td><code>${esc(k)}</code></td><td>${fmtNum(n)}</td>
          <td>${esc(KIND_NOTE[k] || "")}</td></tr>`)
        .join("");
      return renderChapterBody(`
        <div class="note-block">
          <div class="kicker">已标注 ${fmtNum(s.total)} 条</div>
          <table class="mini-table">
            <thead><tr><th>类型</th><th>数量</th><th>对下游价值</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
          <p class="hint">其中标成"未定"的，是因为没命中任何明确规则——不是漏标，是照实说不知道。</p>
        </div>`);
    },
  },
  {
    id: "search",
    title: "检索",
    raw: `在检索页输入关键词，命中结果会高亮。可以只看本人发的消息。

### 一个需要知道的行为边界

**短语匹配要求连续**：

- 搜 \`成本\` → 命中"关键是**成本**"
- 搜 \`拍照\`，但原文是"**拍**了几张**照**片" → **不命中**
- 搜 \`照\` → 命中

这个"严格"是有意的。如果改成松散匹配（字符都出现就算），\`本成\` 也会命中 \`成本\`，
误报会多到没法用。**宁可漏，不可错**——漏了换个词就能找到，错了你会以为数据里真有那句话。

<details class="details-inline">
<summary>中文检索为什么需要特殊处理（技术细节，可以不看）</summary>

SQLite 内置的全文索引分词器对中文都不可用：\`unicode61\` 会把标点之间的整段当成一个词，
\`trigram\` 又只建三字滑窗、双字词查不到。所以这个工具自己做了字符级索引——
把 \`关键是成本\` 存成 \`关 键 是 成 本\`，查询时拆成连续短语匹配。

</details>`,
    render: () => renderChapterBody(`
      <div class="note-block">
        <div class="kicker">试试看</div>
        <p>去检索页输入一个词。如果装的是示例数据，试试「成本」或「换工作」。</p>
        <p><a class="btn" href="#search">打开检索</a></p>
      </div>`),
  },

  {
    id: "view",
    title: "呈现：给人看的东西",
    raw: `工具会生成四份 Markdown 清单，放在 \`40-呈现/\` 下。**不需要任何软件就能打开。**

| 文件 | 内容 |
|---|---|
| 总览 | 数量、类型分布、时间范围、训练材料量估算 |
| 会话清单 | 每个会话的来源、级别、条数、时间范围 |
| 高价值样本 | 按长度排序的自撰/观点/情绪类材料 |
| **待处理问题** | **主动列出这份档案的毛病** |

### 最后一份最重要

它不夸你档案做得好，而是列问题：多少条没有时间、多少条标注不确定、
多少条含第三方隐私、高价值材料够不够量。

**知道自己不知道什么**，比一个漂亮的假数字有用得多。`,
    render: () => renderChapterBody(`
      <div class="note-block">
        <div class="kicker">去看</div>
        <p>四份清单已经生成了。重点看最后一份《待处理问题》。</p>
        <p><a class="btn" href="#documents">打开呈现页</a></p>
      </div>`),
  },
  {
    id: "encrypt",
    title: "加密",
    raw: `### 为什么必须先有这一层

这个工具产出的档案里会有你全部的聊天记录、声音、照片。明文放在硬盘上，
等于把整理好的自己交给任何一个能碰到这台电脑的人。

### 怎么做的

| 项 | 选择 |
|---|---|
| 对称加密 | AES-256-GCM（认证加密——密文改一个字节，解密必须失败） |
| 密码派生 | scrypt（内存硬化，抵抗暴力破解） |
| 每文件子密钥 | 从主密钥按文件路径派生，一个文件被攻破不影响其他 |

### 一个必须说清楚的代价

**有三个东西永远不加密**，这是设计约束，不是遗漏：

| 文件 | 为什么必须明文 |
|---|---|
| \`00-说明.md\` | 否则没人知道这个档案是什么 |
| \`01-授权/\` | 否则没人知道谁有权使用它 |
| \`90-审计/\` | **加锁操作本身要写日志**。加密它会导致「加密→写日志→新明文」永远不收敛 |

所以锁定之后，**任何人拿到这个文件夹，都能看到这些说明——但看不到内容。**
这正是设计目标：**知道这是什么、谁能用，但碰不到里面。**

### 操作

去加密页，输口令，点加锁。然后回概览和记录页看看——
内容会被拒绝，但说明文件和授权页照常可读。

**这一步可以以后再回来做**，不加密也能把档案用起来。但别忘了它。`,
    render: () => renderChapterBody(`
      <div class="note-block">
        <div class="kicker">现在就是最后一章</div>
        <p>你可以现在就去加密，也可以先在工作台里四处看看。</p>
        <div class="btn-row">
          <a class="btn btn-solid" href="#security">去加密</a>
          <a class="btn" href="#overview">进工作台</a>
        </div>
      </div>`),
  },
];

/* ── 手册渲染 ────────────────────────────────────────────────────────── */

function renderChapterBody(html) {
  return html;
}

function renderChapterDone(message, actions = "") {
  return `
    <div class="chapter-done">
      <div class="done-mark">已完成</div>
      <p>${esc(message)}</p>
      <div class="btn-row">${actions}
        <button class="btn btn-solid" data-next>下一章 →</button>
      </div>
    </div>`;
}

function renderCreateForm() {
  return `
    <div class="wizard">
      <div class="wizard-step">
        <span class="step-num">1</span>
        <div class="step-body">
          <div class="step-title">档案主人是谁</div>
          <p class="step-hint">填名字或称呼。它会写进说明文件，给未来读到这份档案的人看。</p>
          <input class="input" id="w-subject" placeholder="例如：张三" autocomplete="off">
        </div>
      </div>

      <div class="wizard-step">
        <span class="step-num">2</span>
        <div class="step-body">
          <div class="step-title">放在哪里</div>
          <p class="step-hint">档案是一个普通文件夹，路径由启动时指定。这一次将建立在：</p>
          <input class="input" id="w-root" readonly>
        </div>
      </div>

      <div class="wizard-step">
        <span class="step-num">3</span>
        <div class="step-body">
          <div class="step-title">选安全级别</div>
          <p class="step-hint">看清「不挡什么」那一栏再选。</p>
          <div class="level-choice" id="level-choice">
            <label class="level-opt">
              <input type="radio" name="level" value="standard" checked>
              <div class="level-opt-body">
                <div class="level-opt-name">standard · 标准<span class="level-opt-default">默认</span></div>
                <div class="level-opt-row"><span class="k">挡什么</span>磁盘被单独拿走</div>
                <div class="level-opt-row danger"><span class="k">不挡什么</span>能接触已解锁机器的人；运行中的进程</div>
              </div>
            </label>
            <label class="level-opt">
              <input type="radio" name="level" value="paranoid">
              <div class="level-opt-body">
                <div class="level-opt-name">paranoid · 偏执</div>
                <div class="level-opt-row"><span class="k">挡什么</span>上述 + 无密钥者离线暴力破解</div>
                <div class="level-opt-row danger"><span class="k">不挡什么</span>密钥丢失即永久不可读，没有任何人（包括作者）能恢复</div>
              </div>
            </label>
            <label class="level-opt">
              <input type="radio" name="level" value="basic">
              <div class="level-opt-body">
                <div class="level-opt-name">basic · 不加密</div>
                <div class="level-opt-row"><span class="k">挡什么</span>顺手翻看、误删</div>
                <div class="level-opt-row danger"><span class="k">不挡什么</span>拿到文件的人。等于没有加密保护</div>
              </div>
            </label>
          </div>
        </div>
      </div>

      <div class="wizard-step" id="pw-step">
        <span class="step-num">4</span>
        <div class="step-body">
          <div class="step-title">设一个口令</div>
          <p class="step-hint">
            这是你换电脑后唯一能打开档案的凭据。至少 8 个字符。
            <b>丢了就永远打不开</b>——没有任何人能帮你找回。
          </p>
          <input class="input" id="w-pw" type="password" placeholder="口令" autocomplete="new-password">
          <input class="input" id="w-pw2" type="password" placeholder="再输一次" autocomplete="new-password" style="margin-top:8px">
        </div>
      </div>

      <div class="wizard-actions">
        <button class="btn btn-solid" id="create-btn">建立档案</button>
        <span class="action-log" id="create-log"></span>
      </div>
    </div>`;
}

function bindCreateForm() {
  const rootInput = $("#w-root");
  if (rootInput && state.config) rootInput.value = state.config.root;

  $$('input[name="level"]').forEach((el) => el.addEventListener("change", () => {
    const lvl = document.querySelector('input[name="level"]:checked').value;
    $("#pw-step").style.display = lvl === "basic" ? "none" : "";
  }));

  const btn = $("#create-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const level = document.querySelector('input[name="level"]:checked').value;
    const subject = ($("#w-subject").value || "").trim();
    const pw = $("#w-pw").value;
    const pw2 = $("#w-pw2").value;
    if (!subject) { setLog("#create-log", "请填写档案主人。", "err"); return; }
    btn.disabled = true;
    setLog("#create-log", "正在建立…");
    try {
      const r = await api("create_archive", {
        subject, level, password: pw, password2: pw2,
      });
      if (r.error) { setLog("#create-log", r.message || r.error, "err"); return; }
      setLog("#create-log", "档案已建立。", "ok");
      await refreshConfig();
      renderChapter(1);
    } finally {
      btn.disabled = false;
    }
  });
}

function bindSampleLoader() {
  const btn = $("#load-sample");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    setLog("#sample-log", "正在装入示例…");
    try {
      const r = await api("load_sample");
      if (r.error) { setLog("#sample-log", r.message || r.error, "err"); return; }
      setLog("#sample-log", "示例数据已装入（编造的数据，可随时删除）。", "ok");
      await refreshConfig();
      await refreshOverview();
      renderChapter(2);
    } finally { btn.disabled = false; }
  });
}

function setLog(sel, text, cls) {
  const el = $(sel);
  if (!el) return;
  el.className = "action-log" + (cls ? " " + cls : "");
  el.textContent = text;
}

function renderToc() {
  $("#toc-list").innerHTML = CHAPTERS.map((c, i) => `
    <li class="toc-item${i === state.chapter ? " on" : ""}${state.done.has(i) ? " done" : ""}">
      <button data-chapter="${i}">
        <span class="toc-num">${String(i + 1).padStart(2, "0")}</span>
        <span class="toc-title">${esc(c.title)}</span>
      </button>
    </li>`).join("");
  $$("#toc-list button").forEach((el) => el.addEventListener("click", () => {
    renderChapter(Number(el.dataset.chapter));
  }));
  const total = CHAPTERS.length;
  const pct = Math.round(state.done.size / total * 100);
  $("#toc-bar-fill").style.width = pct + "%";
  $("#toc-progress-text").textContent = `进度：已完成 ${state.done.size} / ${total} 章`;
}

function renderChapter(idx, updateHash = true) {
  state.chapter = Math.max(0, Math.min(idx, CHAPTERS.length - 1));
  const ch = CHAPTERS[state.chapter];
  // 章节进 URL：刷新或重进手册时不会丢掉进度
  if (updateHash) {
    const want = "#manual/" + state.chapter;
    if (location.hash !== want) history.replaceState(null, "", want);
  }
  renderToc();

  const body = $("#manual-body");
  body.innerHTML = `
    <div class="chapter-head">
      <span class="kicker">第 ${state.chapter + 1} 章 / 共 ${CHAPTERS.length} 章</span>
      <h1 class="display-2">${esc(ch.title)}</h1>
    </div>
    <div class="chapter-prose">${renderMarkdown(ch.raw)}</div>
    <div class="chapter-action" id="chapter-action"></div>
    <div class="chapter-nav">
      <button class="btn" data-prev ${state.chapter === 0 ? "disabled" : ""}>← 上一章</button>
      <button class="btn btn-solid" data-next>${state.chapter === CHAPTERS.length - 1 ? "进工作台 →" : "下一章 →"}</button>
    </div>`;

  const actionBox = $("#chapter-action");
  if (ch.render) {
    actionBox.innerHTML = '<div class="section-head"><span class="kicker">动手</span></div>' + ch.render();
    if (ch.id === "create") bindCreateForm();
    if (ch.id === "import") bindSampleLoader();
  }

  $$("#manual-body [data-prev]").forEach((el) => el.addEventListener("click", () => {
    renderChapter(state.chapter - 1);
    scrollTop();
  }));
  $$("#manual-body [data-next]").forEach((el) => el.addEventListener("click", () => {
    state.done.add(state.chapter);
    if (state.chapter === CHAPTERS.length - 1) {
      location.hash = "#overview";
      return;
    }
    renderChapter(state.chapter + 1);
    scrollTop();
  }));
}

function scrollTop() {
  // 文档本身在滚（.frame 是 grid，body 滚动）。之前用
  // $("#main").scrollIntoView 对滚动容器无效，导致翻页后保持原滚动位置。
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
}

function renderEmptyInto(sel, title, body, action = "") {
  const el = $(sel);
  if (!el) return;
  el.innerHTML = `
    <div class="empty-state">
      <div class="kicker">还没有内容</div>
      <div class="empty-title">${esc(title)}</div>
      <p class="empty-body">${body}</p>
      <div class="btn-row">
        <a class="btn btn-solid" href="#manual">打开手册，从第一章开始</a>
        ${action}
      </div>
    </div>`;
}

/* ── 侧栏 ────────────────────────────────────────────────────────────── */

function setActiveNav(view) {
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.view === view));
  $("#nav-manual").classList.toggle("active", view === "manual");
  const label = {
    manual: "手册与向导", overview: "概览", import: "导入材料", records: "记录",
    search: "检索", documents: "呈现", consent: "授权", security: "加密",
  };
  $("#crumb").textContent = label[view] || view;
}

/* ── 概览 ────────────────────────────────────────────────────────────── */

function renderOverview(data) {
  state.overview = data;
  if (!data || !data.initialized) {
    $("#overview-empty").innerHTML = "";
    renderEmptyInto("#overview-empty", "还没有档案",
      "档案是一个普通文件夹，按权威性分七层。建立一个之后，这里会显示它的结构、指标和安全级别。");
    $("#overview-real").hidden = true;
    return;
  }
  $("#overview-empty").innerHTML = "";
  $("#overview-real").hidden = false;

  $("#rail-subject").textContent = data.subject || "（未命名）";
  $("#rail-level").textContent = "级别 " + data.level + (data.locked ? " · 已锁定" : "");
  $("#hero-subject").textContent = data.subject || "未命名档案";

  const lvlCn = { basic: "不加密", standard: "标准", paranoid: "偏执" };
  $("#hero-lede").textContent =
    `安全级别 ${lvlCn[data.level] || data.level}。原始素材是权威，索引可以随时抛弃重建。` +
    `说明文件与授权目录永远明文——否则没人知道这是什么、谁能用。`;

  const s = data.stats;
  const metrics = [];
  if (s) {
    metrics.push({ n: fmtNum(s.total), label: "记录总数", unit: "条" });
    metrics.push({ n: fmtNum(s.conversations), label: "会话数" });
    metrics.push({ n: fmtNum((s.by_kind || {}).authored || 0),
      label: "自撰材料 · 对下游价值最高" });
    metrics.push({
      n: s.no_timestamp ? fmtNum(s.no_timestamp) : "0",
      label: "没有时间的记录 · 时间精度影响排序",
      warn: s.no_timestamp > 0,
    });
  } else {
    metrics.push({ n: data.encrypted_files, label: "加密文件", unit: "个" });
    metrics.push({ n: data.plain_files, label: "明文文件", unit: "个" });
    metrics.push({ n: "—", label: "锁定状态下不可读取内容" });
    metrics.push({ n: "—", label: "解锁后可看统计" });
  }
  $("#metrics").innerHTML = metrics.map((m) => `
    <div class="metric${m.warn ? " metric-warn" : ""}">
      <div class="metric-num">${esc(m.n)}${m.unit ? '<span class="unit">' + esc(m.unit) + "</span>" : ""}</div>
      <div class="metric-label">${esc(m.label)}</div>
    </div>`).join("");

  const cells = [
    ["00-说明.md", "给十年后的读者：这是什么、怎么读、谁能用。", "权威", "authority"],
    ["01-授权/", "授权声明、身后指令、密钥托管。任何使用者必须先读。", "权威", "authority"],
    ["10-原始/", "原始素材。永不修改、永不删除。", "唯一权威", "authority"],
    ["20-派生/", "从原始素材派生的可读记录。", "可重建", "cache"],
    ["30-索引/", "SQLite 检索库。整个删掉也不影响档案完整性。", "可抛弃", "cache"],
    ["40-呈现/", "总览、会话清单、高价值样本、待处理问题。", "可重建", "cache"],
    ["90-审计/", "只追加的操作日志。锁定后仍为明文。", "设计如此", "clear"],
  ];
  $("#archive-map").innerHTML = cells.map(([name, role, tag, cls]) => `
    <div class="map-cell ${cls}">
      <div><div class="map-name">${esc(name)}</div>
        <div class="map-role">${esc(role)}</div></div>
      <div class="map-tag">${esc(tag)}</div>
    </div>`).join("");

  const levels = [
    ["basic", "不加密", "不加密，或仅混淆", "顺手翻看、误删", "拿到文件的人。等于没有加密保护"],
    ["standard", "标准", "Windows DPAPI + 口令", "磁盘被单独拿走", "能接触已解锁机器的人；运行中的进程"],
    ["paranoid", "偏执", "仅口令（scrypt 派生）", "上述 + 无密钥者离线暴力破解",
      "密钥丢失即永久不可读，没有任何人（包括作者）能恢复"],
  ];
  $("#levels").innerHTML = levels.map(([id, cn, key, blocks, notBlocks]) => `
    <div class="level${data.level === id ? " current" : ""}">
      <div class="level-name"><span>${esc(id)} · ${esc(cn)}</span>
        ${data.level === id ? '<span class="level-badge">当前</span>' : ""}</div>
      <div class="level-row"><span class="kicker">密钥保护</span><p>${esc(key)}</p></div>
      <div class="level-row"><span class="kicker">挡什么</span><p>${esc(blocks)}</p></div>
      <div class="level-row danger"><span class="kicker">不挡什么</span><p>${esc(notBlocks)}</p></div>
    </div>`).join("");

  $("#top-status").textContent = data.locked
    ? "已锁定 · " + data.encrypted_files + " 个密文"
    : "未锁定 · " + (data.plain_files || 0) + " 个明文";
  $("#top-root").textContent = data.root;
}

/* ── 记录 ────────────────────────────────────────────────────────────── */

function lockedBlock(message) {
  return `<div class="locked-msg">
    <div class="kicker">档案已锁定</div>
    <p>${esc(message || "解锁后才能读取内容。")}</p>
    <p><a href="#security" style="color:var(--red-ink)">前往加密页解锁</a></p>
  </div>`;
}

async function loadRecords() {
  const hasArchive = state.config && state.config.initialized;
  $("#records-real").hidden = true;
  if (!hasArchive) {
    renderEmptyInto("#records-empty", "还没有档案，所以没有记录",
      "先把材料导入档案，这里才会出现内容。");
    return;
  }
  // 所有网络调用都包在 try 里：失败时要给用户看错误，
  // 不能像之前那样留下一个空白的 0/0 页面。
  let kinds, data;
  try {
    kinds = await apiGet("record_kinds");
    if (kinds.error === "locked") {
      $("#records-empty").innerHTML = lockedBlock();
      return;
    }
    data = await apiGet("records", {
      limit: 80, kind: state.recordsFilter.kind,
      direction: state.recordsFilter.direction,
    });
  } catch (e) {
    $("#records-empty").innerHTML = errBlock(e);
    return;
  }
  $("#records-empty").innerHTML = "";
  if (data.error === "locked") { $("#records-empty").innerHTML = lockedBlock(data.message); return; }
  if (data.error) {
    renderEmptyInto("#records-empty", "还没有记录",
      "档案是空的。去手册第 3 章学会导入材料。");
    return;
  }
  $("#records-real").hidden = false;

  const order = ["authored", "chat_opinion", "chat_emotion", "chat_media",
                 "chat_casual", "third_party", "ai_drafted", "unknown"];
  const totalKinds = Object.values(kinds.kinds || {}).reduce((a, b) => a + b, 0);
  const kindChips = ['<button class="chip' + (state.recordsFilter.kind === "" ? " on" : "") +
    '" data-kind="">全部 <span class="n">' + fmtNum(totalKinds) + "</span></button>"];
  order.forEach((k) => {
    if (!(kinds.kinds || {})[k]) return;
    kindChips.push('<button class="chip' + (state.recordsFilter.kind === k ? " on" : "") +
      '" data-kind="' + k + '">' + (KIND_LABEL[k] || k) +
      ' <span class="n">' + fmtNum(kinds.kinds[k]) + "</span></button>");
  });
  $("#kind-chips").innerHTML = kindChips.join("");

  const dirChips = ['<button class="chip' + (state.recordsFilter.direction === "" ? " on" : "") +
    '" data-dir="">全部</button>'];
  ["self", "other", "unknown"].forEach((d) => {
    if (!(kinds.directions || {})[d]) return;
    dirChips.push('<button class="chip' + (state.recordsFilter.direction === d ? " on" : "") +
      '" data-dir="' + d + '">' + DIR_LABEL[d] +
      ' <span class="n">' + fmtNum(kinds.directions[d]) + "</span></button>");
  });
  $("#dir-chips").innerHTML = dirChips.join("");
  $$("#kind-chips .chip").forEach((el) => el.addEventListener("click", () => {
    state.recordsFilter.kind = el.dataset.kind; loadRecords();
  }));
  $$("#dir-chips .chip").forEach((el) => el.addEventListener("click", () => {
    state.recordsFilter.direction = el.dataset.dir; loadRecords();
  }));

  $("#records-count").textContent = `显示 ${data.shown} / ${fmtNum(data.total)} 条`;
  if (!data.records.length) {
    $("#record-list").innerHTML = '<div class="empty">这个筛选条件下没有记录。</div>';
    return;
  }
  $("#record-list").innerHTML = data.records.map((r) => `
    <div class="rec">
      <div class="rec-ts">${esc(fmtTs(r.ts))}</div>
      <div class="rec-dir ${esc(r.direction)}">${esc(DIR_LABEL[r.direction] || "—")}</div>
      <div class="rec-kind ${KIND_CLASS[r.kind] || "low"}">${esc(KIND_LABEL[r.kind] || r.kind)} · ${esc(KIND_NOTE[r.kind] || "?")}</div>
      <div>
        <div class="rec-text">${esc(r.text)}</div>
        <div class="rec-rule">${esc(r.id)} · ${esc(r.rule || "")} · 置信 ${esc(r.confidence || "?")} · 来源 ${esc(r.source)} L${esc(r.level)}</div>
      </div>
    </div>`).join("");
}

/* ── 检索 ────────────────────────────────────────────────────────────── */

async function loadSearch() {
  const hasArchive = state.config && state.config.initialized;
  $("#search-real").hidden = true;
  if (!hasArchive) {
    renderEmptyInto("#search-empty", "还没有档案",
      "检索的是档案里的记录。先建立一个档案、导入材料，才有东西可搜。");
    return;
  }
  let recs = 0;
  try {
    const k = await apiGet("record_kinds");
    if (k.error === "locked") {
      $("#search-empty").innerHTML = lockedBlock();
      return;
    }
    recs = Object.values(k.kinds || {}).reduce((a, b) => a + b, 0);
  } catch (e) {
    $("#search-empty").innerHTML = errBlock(e);
    return;
  }
  if (!recs) {
    renderEmptyInto("#search-empty", "档案里还没有记录",
      "先去手册第 3 章导入材料。有记录之后这里才会有检索框。");
    return;
  }
  if (!state.config.initialized) {
    renderEmptyInto("#search-empty", "还没有档案", "");
    return;
  }
  $("#search-empty").innerHTML = "";
  $("#search-real").hidden = false;
  const input = $("#search-input");
  if (input && document.activeElement !== input) input.focus();
}

function errBlock(e) {
  return `<div class="locked-msg">
    <div class="kicker">读取失败</div>
    <p>${esc(e && e.message ? e.message : String(e))}</p>
    <p>这不应该是正常状态。刷新页面再试一次。</p>
  </div>`;
}

async function doSearch() {
  const q = ($("#search-input").value || "").trim();
  if (!q) { $("#search-results").innerHTML = ""; $("#search-count").textContent = ""; return; }
  let data;
  try {
    data = await apiGet("search", {
      q, kind: state.searchKind, self_only: $("#search-self").checked ? "1" : "0",
    });
  } catch (e) {
    $("#search-count").textContent = "检索失败";
    $("#search-results").innerHTML = errBlock(e);
    return;
  }
  if (data.error === "locked") { $("#search-results").innerHTML = lockedBlock(data.message);
    $("#search-count").textContent = ""; return; }
  if (data.error) { $("#search-count").textContent = data.message || "检索失败";
    $("#search-results").innerHTML = ""; return; }
  $("#search-count").textContent = `命中 ${data.total} 条 · 查询「${data.query}」`;
  if (!data.results.length) {
    $("#search-results").innerHTML = '<div class="empty">没有匹配。试试更短的词——中文按字符匹配，' +
      '但短语要求连续（比如「拍照」不会命中「拍了几张照片」）。</div>';
    return;
  }
  const hl = (txt) => esc(txt).replace(
    new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"),
    (m) => "<mark>" + m + "</mark>");
  $("#search-results").innerHTML = data.results.map((r) => `
    <div class="rec">
      <div class="rec-ts">${esc(fmtTs(r.ts))}</div>
      <div class="rec-dir ${esc(r.direction)}">${esc(DIR_LABEL[r.direction] || "—")}</div>
      <div class="rec-kind ${KIND_CLASS[r.kind] || "low"}">${esc(KIND_LABEL[r.kind] || r.kind)}</div>
      <div><div class="rec-text">${hl(r.text)}</div>
        <div class="rec-rule">${esc(r.id)}</div></div>
    </div>`).join("");
}

/* ── 呈现 / 授权 ─────────────────────────────────────────────────────── */

async function loadDocuments() {
  const hasArchive = state.config && state.config.initialized;
  $("#documents-real").hidden = true;
  if (!hasArchive) {
    renderEmptyInto("#documents-empty", "还没有档案",
      "四份可读清单是在导入材料、标注、建索引之后生成的。");
    return;
  }
  const data = await apiGet("documents");
  if (data.error === "locked") { $("#documents-empty").innerHTML = lockedBlock(); return; }
  const items = (data.documents || []).map((d) => ({
    name: d.name, body: renderMarkdown(d.content),
  }));
  if (!items.length) {
    renderEmptyInto("#documents-empty", "还没有生成清单",
      "先去手册第 4 章跑一次「标注」，第 5 章跑一次「索引」，清单会自动生成。");
    return;
  }
  $("#documents-empty").innerHTML = "";
  $("#documents-real").hidden = false;
  renderDocTabs(items, "#doc-tabs", "#doc-body");
}

/* ── 授权：可编辑表单 ─────────────────────────────────────────────────── */

const CONSENT_ITEMS = ["存储", "结构化处理", "文本人格构建", "语音克隆",
  "视频头像克隆", "3D_avatar构建", "模型微调", "公开索引",
  "商业使用", "跨境处理", "研究使用", "纪念化运行"];

const CONSENT_HINTS = {
  "存储": "把材料保存在档案里。关掉这一项，档案就没有意义了。",
  "结构化处理": "把杂乱的材料整理成可检索的记录（标注、索引）。",
  "文本人格构建": "用你的文字材料构建一个说话像你的文本模型。",
  "语音克隆": "用你的录音合成你的音色。声纹一旦泄露不可撤回。",
  "视频头像克隆": "用你的视频或照片生成会动的人像。",
  "3D_avatar构建": "构建三维形象。成本最高，通常最后才做。",
  "模型微调": "拿你的材料去微调模型，会改变模型的说话方式。",
  "公开索引": "把你的档案登记到公开目录，别人能搜到。",
  "商业使用": "把这些材料用于营利。",
  "跨境处理": "把数据传到中国大陆以外的地方处理。",
  "研究使用": "用于学术研究。",
  "纪念化运行": "在你去世后继续运行这个档案。",
};

let consentState = {};  // 当前表单里的 12 项取值

function renderConsentForm(statement) {
  const g = (statement && statement.granularity) || {};
  consentState = {};
  CONSENT_ITEMS.forEach((k) => { consentState[k] = g[k] || "unset"; });

  $("#consent-list").innerHTML = CONSENT_ITEMS.map((k) => `
    <div class="consent-row" data-item="${esc(k)}">
      <div class="consent-info">
        <div class="consent-name">${esc(k)}</div>
        <div class="consent-hint">${esc(CONSENT_HINTS[k] || "")}</div>
      </div>
      <div class="seg" role="group">
        ${["allow", "unset", "deny"].map((v) => `
          <button class="seg-btn${(g[k] || "unset") === v ? " on" : ""}"
                  data-item="${esc(k)}" data-val="${v}">
            ${v === "allow" ? "允许" : v === "deny" ? "禁止" : "未定"}
          </button>`).join("")}
      </div>
    </div>`).join("");

  $$("#consent-list .seg-btn").forEach((el) => el.addEventListener("click", () => {
    const item = el.dataset.item, val = el.dataset.val;
    consentState[item] = val;
    $$(`#consent-list .seg-btn[data-item="${CSS.escape(item)}"]`)
      .forEach((b) => b.classList.toggle("on", b.dataset.val === val));
    updateConsentSummary();
  }));

  $("#consent-signature").value = (statement && statement.signature) || "";
  $("#consent-date").value = (statement && statement.date)
    || new Date().toISOString().slice(0, 10);
  updateConsentSummary();
}

function updateConsentSummary() {
  const vals = Object.values(consentState);
  const a = vals.filter((v) => v === "allow").length;
  const d = vals.filter((v) => v === "deny").length;
  const u = vals.filter((v) => v === "unset").length;
  $("#consent-summary").textContent =
    `允许 ${a} · 禁止 ${d} · 未定 ${u}（未定按禁止处理）`;
}

async function loadConsent() {
  const hasArchive = state.config && state.config.initialized;
  $("#consent-real").hidden = true;
  if (!hasArchive) {
    renderEmptyInto("#consent-empty", "还没有档案",
      "档案建立时会自动生成授权声明、身后指令、密钥托管三份模板。");
    return;
  }
  const data = await apiGet("consent");
  if (data.error === "locked") {
    $("#consent-empty").innerHTML = lockedBlock();
    return;
  }
  $("#consent-empty").innerHTML = "";
  $("#consent-real").hidden = false;

  renderConsentForm(data.statement);
  $("#after-death").value = data.after_death || "";
  $("#key-custody").textContent = data.key_custody
    ? JSON.stringify(data.key_custody, null, 2) : "（无）";
}

async function saveConsent() {
  const btn = $("#consent-save");
  btn.disabled = true;
  setLog("#consent-log", "保存中…");
  try {
    const r = await api("save_consent", {
      granularity: consentState,
      signature: $("#consent-signature").value.trim(),
      date: $("#consent-date").value.trim(),
    });
    if (r.error) { setLog("#consent-log", r.message || r.error, "err"); return; }
    setLog("#consent-log", "已保存。授权声明.md 已同步重写。", "ok");
  } finally { btn.disabled = false; }
}

async function saveAfterDeath() {
  const btn = $("#after-death-save");
  btn.disabled = true;
  setLog("#after-death-log", "保存中…");
  try {
    const r = await api("save_after_death", { text: $("#after-death").value });
    if (r.error) { setLog("#after-death-log", r.message || r.error, "err"); return; }
    setLog("#after-death-log", `已保存（${r.chars} 字符）。`, "ok");
  } finally { btn.disabled = false; }
}

function bindConsentPage() {
  $("#consent-save").addEventListener("click", saveConsent);
  $("#after-death-save").addEventListener("click", saveAfterDeath);
  $("#consent-bulk-allow").addEventListener("click", () => {
    CONSENT_ITEMS.forEach((k) => { consentState[k] = "allow"; });
    $$("#consent-list .seg-btn").forEach((b) => b.classList.toggle("on", b.dataset.val === "allow"));
    updateConsentSummary();
  });
  $("#consent-bulk-deny").addEventListener("click", () => {
    CONSENT_ITEMS.forEach((k) => { consentState[k] = "deny"; });
    $$("#consent-list .seg-btn").forEach((b) => b.classList.toggle("on", b.dataset.val === "deny"));
    updateConsentSummary();
  });
}

function renderDocTabs(items, tabsSel, bodySel) {
  $(tabsSel).innerHTML = items.map((it, i) =>
    '<button class="doc-tab' + (i === 0 ? " on" : "") + '" data-i="' + i + '">' +
    esc(it.name) + "</button>").join("");
  const show = (i) => {
    $$(tabsSel + " .doc-tab").forEach((el, j) => el.classList.toggle("on", i === j));
    $(bodySel).innerHTML = items[i].body;
  };
  $$(tabsSel + " .doc-tab").forEach((el) => el.addEventListener("click", () => {
    show(Number(el.dataset.i));
  }));
  show(0);
}

/* ── 加密 ────────────────────────────────────────────────────────────── */

function renderSecurity(data) {
  const hasArchive = state.config && state.config.initialized;
  if (!hasArchive) {
    $("#security-real").hidden = true;
    renderEmptyInto("#security-empty", "还没有档案",
      "加密是档案的一个属性，在建立档案时选定。先建一个档案。");
    return;
  }
  $("#security-empty").innerHTML = "";
  $("#security-real").hidden = false;

  const fp = data.key_fingerprint || "未记录";
  const ways = (data.ways || []).join(" / ") || "无";
  $("#security-status").innerHTML = `
    <div>
      <div class="lock-badge ${data.locked ? "locked" : ""}">
        ${data.locked ? "已锁定" : "未锁定"}
      </div>
      <div style="margin-top:16px">
        <div class="kicker">安全级别</div>
        <div class="mono" style="font-size:20px;margin-top:4px">${esc(data.level)}</div>
      </div>
    </div>
    <div class="status-rows">
      <div class="status-row"><span class="k">挡什么</span><span>${esc((data.level_info || {}).blocks || "")}</span></div>
      <div class="status-row"><span class="k">不挡什么</span><span style="color:var(--red-ink)">${esc((data.level_info || {}).does_not_block || "")}</span></div>
      <div class="status-row"><span class="k">密钥指纹</span><span class="mono">${esc(fp)}</span></div>
      <div class="status-row"><span class="k">解锁方式</span><span class="mono">${esc(ways)}</span></div>
      <div class="status-row"><span class="k">密文 / 明文</span><span class="mono">${data.encrypted_files} / ${data.plain_files}</span></div>
      <div class="status-row"><span class="k">加密方案</span><span class="mono">AES-256-GCM · scrypt n=2^14</span></div>
    </div>`;
  $("#clear-list").innerHTML = (data.always_clear || [])
    .map((n) => '<div class="clear-item">' + esc(n) + "</div>").join("");
}

async function doLock() {
  const pw = $("#lock-pw").value;
  $("#lock-btn").disabled = true;
  setLog("#lock-log", "加密中…");
  try {
    const r = await api("lock", { password: pw });
    if (r.error) { setLog("#lock-log", r.message || r.error, "err"); return; }
    if (r.already) { setLog("#lock-log", "档案已经是锁定状态。"); return; }
    const rep = r.report;
    setLog("#lock-log", `完成 · ${rep.files} 个文件 · 明文 ${fmtBytes(rep.bytes_in)} → 密文 ${fmtBytes(rep.bytes_out)} · 验证通过 ${rep.verified}`, "ok");
    await refreshAll();
  } finally { $("#lock-btn").disabled = false; }
}
async function doUnlock() {
  const pw = $("#unlock-pw").value;
  $("#unlock-btn").disabled = true;
  setLog("#unlock-log", "解密中…");
  try {
    const r = await api("unlock", { password: pw });
    if (r.error) { setLog("#unlock-log", r.message || r.error, "err"); return; }
    const rep = r.report;
    let msg = `完成 · 解密 ${rep.files} 个文件 · ${fmtBytes(rep.bytes)}`;
    if (rep.failed) msg += ` · 失败 ${rep.failed}`;
    setLog("#unlock-log", msg, rep.failed ? "err" : "ok");
    await refreshAll();
  } finally { $("#unlock-btn").disabled = false; }
}
async function doVerify() {
  const pw = $("#verify-pw").value;
  $("#verify-btn").disabled = true;
  setLog("#verify-log", "校验中…");
  try {
    const r = await api("verify", { password: pw });
    if (r.error) { setLog("#verify-log", r.message || r.error, "err"); return; }
    const rep = r.report;
    setLog("#verify-log", `OK ${rep.ok} · 坏 ${rep.failed} · 缺失 ${rep.missing}`,
      r.ok ? "ok" : "err");
  } finally { $("#verify-btn").disabled = false; }
}

/* ── 路由 ────────────────────────────────────────────────────────────── */

const VIEWS = ["manual", "overview", "import", "records", "search",
               "documents", "consent", "security"];

async function showView(view) {
  if (!VIEWS.includes(view)) view = "manual";
  state.view = view;
  VIEWS.forEach((v) => { $("#view-" + v).hidden = v !== view; });
  setActiveNav(view);

  if (view === "manual") renderChapter(state.chapter, false);
  if (view === "overview") await refreshOverview();
  if (view === "import") await loadImport();
  if (view === "records") await loadRecords();
  if (view === "search") await loadSearch();
  if (view === "documents") await loadDocuments();
  if (view === "consent") await loadConsent();
  if (view === "security") await refreshOverview();
}

function route() {
  const raw = (location.hash || "#manual").slice(1);
  // 支持 #manual/2 这种带章节号的写法
  const [view, arg] = raw.split("/");
  if (view === "manual" && arg != null && arg !== "") {
    const n = parseInt(arg, 10);
    if (!Number.isNaN(n)) {
      state.chapter = Math.max(0, Math.min(n, CHAPTERS.length - 1));
    }
  }
  showView(view);
}

/* ── 启动 ────────────────────────────────────────────────────────────── */

async function refreshConfig() {
  state.config = await apiGet("config");
  if (state.config.initialized) {
    $("#rail-subject").textContent = state.config.subject || "（未命名）";
    $("#rail-level").textContent = "级别 " + state.config.level +
      (state.config.locked ? " · 已锁定" : "");
    $("#top-root").textContent = state.config.root;
  } else {
    $("#rail-subject").textContent = "尚未建立";
    $("#rail-level").textContent = "—";
    $("#top-root").textContent = state.config.root + "（尚未建立）";
    $("#top-status").textContent = "尚未建立档案";
  }
  return state.config;
}

async function refreshOverview() {
  const data = await apiGet("overview");
  renderOverview(data);
  renderSecurity(data);
  return data;
}

async function refreshAll() {
  await refreshConfig();
  await refreshOverview();
}

function bind() {
  window.addEventListener("hashchange", route);
  $("#search-btn").addEventListener("click", doSearch);
  $("#search-input").addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  $("#search-self").addEventListener("change", doSearch);
  bindConsentPage();
  bindImportPage();
  bindAIPanel();
  $("#lock-btn").addEventListener("click", doLock);
  $("#unlock-btn").addEventListener("click", doUnlock);
  $("#verify-btn").addEventListener("click", doVerify);
  ["lock-pw", "unlock-pw", "verify-pw"].forEach((id) => {
    $("#" + id).addEventListener("keydown", (e) => {
      if (e.key === "Enter") $("#" + id.replace("-pw", "-btn")).click();
    });
  });

  const kinds = ["", "authored", "chat_opinion", "chat_emotion", "chat_media", "chat_casual"];
  $("#search-kind-chips").innerHTML = kinds.map((k) =>
    '<button class="chip' + (k === "" ? " on" : "") + '" data-kind="' + k + '">' +
    (k === "" ? "全部" : KIND_LABEL[k]) + "</button>").join("");
  $$("#search-kind-chips .chip").forEach((el) => el.addEventListener("click", () => {
    state.searchKind = el.dataset.kind;
    $$("#search-kind-chips .chip").forEach((x) => x.classList.toggle("on", x === el));
    doSearch();
  }));
}

/* 初始路由：第一次打开 → 手册第一章，不管有没有档案。
   有档案的话手册里会显示"已完成"，但它仍然是第一站。 */
(async function init() {
  bind();
  await refreshConfig();
  await refreshOverview();
  route();
})();

/* ==========================================================================
   导入页（v4 新增）
   ========================================================================== */

const SOURCE_OPTIONS = [
  ["wechat", "微信聊天记录", "导出的 CSV / TXT / HTML"],
  ["documents", "笔记与文章", "你自己写的文字，价值最高"],
  ["email", "邮件", "导出的邮件"],
  ["photos", "照片与视频", "只登记，暂不解码"],
  ["manual", "手工整理的内容", "直接粘贴或手写"],
  ["loose", "其他文件", "不确定归哪类时用"],
];

const importState = {
  source: "wechat",
  paths: [],
  selfs: new Set(),
};

async function loadImport() {
  $("#import-real").hidden = true;
  if (!state.config || !state.config.initialized) {
    renderEmptyInto("#import-empty", "还没有档案",
      "材料要导进一个档案里。先去手册第 2 章建立一个。");
    return;
  }
  let st;
  try {
    const ov = await apiGet("overview");
    st = ov;
  } catch (e) {
    $("#import-empty").innerHTML = errBlock(e);
    return;
  }
  if (st.locked) {
    $("#import-empty").innerHTML = lockedBlock(
      "档案已锁定。锁定状态不能写入新材料——先解锁。");
    return;
  }
  $("#import-empty").innerHTML = "";
  $("#import-real").hidden = false;
  renderSourceChoice();
  renderPickedList();
  $("#do-import").disabled = true;
  $("#pick-note").textContent = "";
}

function renderSourceChoice() {
  $("#source-choice").innerHTML = SOURCE_OPTIONS.map(([id, name, hint]) => `
    <button class="source-opt${importState.source === id ? " on" : ""}"
            data-src="${id}">
      <div class="source-opt-name">${esc(name)}</div>
      <div class="source-opt-hint">${esc(hint)}</div>
    </button>`).join("");
  $$("#source-choice .source-opt").forEach((el) => el.addEventListener("click", () => {
    importState.source = el.dataset.src;
    $$("#source-choice .source-opt").forEach((x) =>
      x.classList.toggle("on", x === el));
  }));
}

/* 系统文件对话框走 pywebview 桥。桥不存在（浏览器模式）时退化为手输路径。 */
function hasGuiBridge() {
  return !!(window.pywebview && window.pywebview.api
            && typeof window.pywebview.api.pick_files === "function");
}

/* pywebview 桥暴露的是同步 Python 方法，会返回 Promise */
async function bridgeCall(name) {
  if (!hasGuiBridge()) {
    setLog("#pick-note", "浏览器模式用不了系统文件框，请用下面的路径输入框。", "err");
    $("#manual-path-box").open = true;
    return;
  }
  try {
    const r = await window.pywebview.api[name]();
    if (!r) { setLog("#pick-note", "选择框没有返回内容。", "err"); return; }
    if (r.error) { setLog("#pick-note", r.message || r.error, "err"); return; }
    if (r.canceled) return;
    addPaths(r.paths || []);
  } catch (e) {
    setLog("#pick-note", "打开文件框失败：" + (e.message || e), "err");
  }
}

function addPaths(paths) {
  const before = importState.paths.length;
  paths.forEach((p) => {
    const v = String(p || "").trim();
    if (v && !importState.paths.includes(v)) importState.paths.push(v);
  });
  const added = importState.paths.length - before;
  setLog("#pick-note", added ? `已加入 ${added} 个` : "这些已经在列表里了", "ok");
  renderPickedList();
  peekPaths();
}

function renderPickedList() {
  const box = $("#picked-list");
  if (!importState.paths.length) { box.innerHTML = ""; return; }
  box.innerHTML = importState.paths.map((p, i) => `
    <div class="picked-item">
      <span class="path">${esc(p)}</span>
      <button class="remove" data-i="${i}" title="移除">×</button>
    </div>`).join("");
  $$("#picked-list .remove").forEach((el) => el.addEventListener("click", () => {
    importState.paths.splice(Number(el.dataset.i), 1);
    renderPickedList();
    peekPaths();
  }));
  $("#do-import").disabled = importState.paths.length === 0;
}

/* 导入前先解析一遍，让用户看到"它读出了什么、读出了哪些名字" */
async function peekPaths() {
  const box = $("#peek-result");
  $("#self-picker").innerHTML = '<div class="self-empty">解析中…</div>';
  if (!importState.paths.length) {
    box.innerHTML = "";
    $("#self-picker").innerHTML =
      '<div class="self-empty">选完文件后，这里会列出出现过的名字，点一下就能标记为你。</div>';
    return;
  }
  let data;
  try {
    data = await api("peek_files", { paths: importState.paths });
  } catch (e) {
    box.innerHTML = errBlock(e);
    return;
  }
  if (data.error) { box.innerHTML = errBlock({ message: data.message }); return; }

  const senders = new Map();
  box.innerHTML = (data.items || []).map((it) => {
    if (it.error) {
      return `<div class="peek-card">
        <div class="peek-name">${esc(it.path)}</div>
        <div class="peek-error">读不出来：${esc(it.error)}</div>
      </div>`;
    }
    if (it.is_dir) {
      return `<div class="peek-card">
        <div class="peek-name">${esc(it.path)}　（文件夹）</div>
        <div class="peek-stat">可解析文件 <b>${it.parseable}</b> 个 · 媒体文件 <b>${it.media}</b> 个</div>
      </div>`;
    }
    (it.senders || []).forEach((s) => {
      if (s.name) senders.set(s.name, (senders.get(s.name) || 0) + s.count);
    });
    return `<div class="peek-card">
      <div class="peek-name">${esc(it.name || it.path)}</div>
      <div class="peek-stat">读出 <b>${it.messages}</b> 条</div>
      ${it.messages === 0 ? '<div class="peek-warn">解析成功但没有任何消息——这个文件可能格式不对。</div>' : ''}
    </div>`;
  }).join("");

  renderSelfPicker(senders);
}

function renderSelfPicker(senders) {
  const box = $("#self-picker");
  const list = Array.from(senders.entries()).sort((a, b) => b[1] - a[1]);
  if (!list.length) {
    box.innerHTML = '<div class="self-empty">读出的文件里没有发送者信息。' +
      '这可能是纯文本，或者导出文件没有发送者列。</div>';
    return;
  }
  box.innerHTML = list.map(([name, n]) => `
    <button class="sender-pill${importState.selfs.has(name) ? " on" : ""}"
            data-name="${esc(name)}">${esc(name)} · ${n}</button>`).join("")
    + '<div class="self-empty" style="width:100%">点一下标记为"你自己"。可以选多个（你在不同群里可能用不同名字）。</div>';
  $$("#self-picker .sender-pill").forEach((el) => el.addEventListener("click", () => {
    const name = el.dataset.name;
    if (importState.selfs.has(name)) {
      importState.selfs.delete(name);
      el.classList.remove("on");
    } else {
      importState.selfs.add(name);
      el.classList.add("on");
    }
    syncSelfInput();
  }));
}

function syncSelfInput() {
  $("#self-input").value = Array.from(importState.selfs).join("，");
}

async function doImport() {
  if (!importState.paths.length) return;
  const btn = $("#do-import");
  btn.disabled = true;
  setLog("#import-log", "导入中…");
  const selfs = Array.from(importState.selfs);
  const typed = $("#self-input").value.split(/[,，\s]+/).filter(Boolean);
  typed.forEach((t) => { if (!selfs.includes(t)) selfs.push(t); });

  let r;
  try {
    r = await api("import_paths", {
      paths: importState.paths,
      source: importState.source,
      source_level: 2,
      self_names: selfs,
      tag_and_index: true,
    });
  } catch (e) {
    setLog("#import-log", "导入失败：" + (e.message || e), "err");
    btn.disabled = false;
    return;
  }
  if (r.error) {
    setLog("#import-log", r.message || r.error, "err");
    btn.disabled = false;
    return;
  }

  const rep = r.report || {};
  const box = $("#import-result");
  const ok = (rep.files_ok || 0) > 0;
  box.innerHTML = `
    <div class="result-box${ok ? "" : " fail"}">
      <div class="result-title">${ok ? "导入完成" : "没有文件被导入"}</div>
      <div class="result-rows">
        <div class="result-row"><span class="k">成功文件</span><span>${rep.files_ok || 0}</span></div>
        <div class="result-row"><span class="k">失败文件</span><span>${rep.files_failed || 0}</span></div>
        <div class="result-row"><span class="k">消息条数</span><span>${rep.messages || 0}</span></div>
        <div class="result-row"><span class="k">登记媒体</span><span>${rep.media_registered || 0}</span></div>
        ${r.tagged ? `<div class="result-row"><span class="k">已标注</span><span>${
          Object.entries(r.tagged.by_kind || {})
            .map(([k, n]) => `${esc(KIND_LABEL[k] || k)} ${n}`).join(" · ") || "—"
        }</span></div>` : ""}
        ${r.indexed ? `<div class="result-row"><span class="k">索引</span><span>${r.indexed.records} 条记录</span></div>` : ""}
      </div>
      ${(rep.failures || []).length ? `<div class="peek-warn">${rep.failures.map(
        (f) => esc(f.file.split(/[\\/]/).pop()) + "：" + esc(f.reason)).join("<br>")}</div>` : ""}
      <div class="result-link">
        <a class="btn btn-solid" href="#records">去看记录</a>
        <a class="btn" href="#documents">看呈现清单</a>
      </div>
    </div>`;
  setLog("#import-log", ok ? "完成" : "失败", ok ? "ok" : "err");

  // 导入成功后清空已选，方便继续导下一批
  if (ok) {
    importState.paths = [];
    importState.selfs.clear();
    renderPickedList();
    $("#self-input").value = "";
    $("#peek-result").innerHTML = "";
    await refreshConfig();
    await refreshOverview();
  }
  btn.disabled = false;
}

function bindImportPage() {
  $("#pick-files").addEventListener("click", () => bridgeCall("pick_files"));
  $("#pick-folder").addEventListener("click", () => bridgeCall("pick_folder"));
  $("#manual-paths").addEventListener("change", () => {
    const raw = $("#manual-paths").value || "";
    const paths = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (paths.length) addPaths(paths);
  });
  $("#do-import").addEventListener("click", doImport);
  $("#self-input").addEventListener("input", () => {
    importState.selfs = new Set(
      $("#self-input").value.split(/[,，\s]+/).filter(Boolean));
    $$("#self-picker .sender-pill").forEach((el) =>
      el.classList.toggle("on", importState.selfs.has(el.dataset.name)));
  });
}

/* pywebview 桥就绪后，如果当前在导入页，刷新一下按钮状态 */
window.addEventListener("pywebviewready", () => {
  setLog("#pick-note", "", "");
});

/* ==========================================================================
   AI 协助起草（v5 新增）
   ========================================================================== */

const aiState = { draftText: "", draftModel: "", loaded: false };

async function loadAI() {
  const hasArchive = state.config && state.config.initialized;
  if (!hasArchive) {
    $("#ai-empty").innerHTML = '<div class="ai-status err">先建立档案才能用 AI 协助。</div>';
    $("#ai-real").hidden = true;
    return;
  }
  $("#ai-empty").innerHTML = "";
  $("#ai-real").hidden = false;

  let cfg;
  try {
    cfg = await apiGet("ai_config");
  } catch (e) {
    $("#ai-empty").innerHTML = '<div class="ai-status err">读取配置失败：'
      + esc(e.message || e) + "</div>";
    return;
  }
  if (cfg.error) {
    $("#ai-empty").innerHTML = '<div class="ai-status err">' + esc(cfg.message || cfg.error) + "</div>";
    return;
  }

  // 服务商下拉
  $("#ai-provider").innerHTML = (cfg.providers || []).map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  $("#ai-provider").value = cfg.provider || "deepseek";

  const applyProvider = (pid, keepValue) => {
    const p = (cfg.providers || []).find((x) => x.id === pid);
    if (!p) return;
    if (!keepValue) {
      $("#ai-baseurl").value = p.base_url || "";
      $("#ai-model").value = p.model || "";
    }
  };

  // 如果服务商和已存的不是同一个，或者没有存过，用预设值
  const sameProvider = cfg.provider === $("#ai-provider").value;
  $("#ai-baseurl").value = sameProvider && cfg.base_url ? cfg.base_url
    : (($("#ai-provider").selectedOptions[0] || {}).text ? "" : "");
  $("#ai-model").value = sameProvider && cfg.model ? cfg.model : "";
  if (!$("#ai-baseurl").value || !$("#ai-model").value) {
    applyProvider($("#ai-provider").value, false);
  }

  $("#ai-key").value = "";
  $("#ai-key").placeholder = cfg.has_key
    ? "已保存（留空表示不修改）" : "填入你的 API Key";
  $("#ai-save").textContent = cfg.has_key ? "更新配置" : "保存配置";

  const bits = [];
  if (cfg.has_key) bits.push("已配置 Key（" + (cfg.key_mode === "dpapi"
    ? "DPAPI 加密" : "明文存储") + "）");
  else bits.push("还没配置 Key");
  if (cfg.configured_at) bits.push("配置于 " + fmtTs(cfg.configured_at));
  $("#ai-status").textContent = bits.join(" · ");
  $("#ai-status").className = "ai-status";

  $("#ai-provider").onchange = () => {
    const p = (cfg.providers || []).find(
      (x) => x.id === $("#ai-provider").value);
    if (p) { $("#ai-baseurl").value = p.base_url || ""; $("#ai-model").value = p.model || ""; }
  };

  $("#ai-save").onclick = doAISave;
  $("#ai-test").onclick = doAITest;
  $("#ai-draft").onclick = doAIDraft;
  $("#ai-commit").onclick = doAICommit;
  bindDigestPanel();
  $("#ai-discard").onclick = () => {
    $("#ai-review").hidden = true;
    aiState.draftText = "";
    $("#ai-draft-status").textContent = "已丢弃";
  };
}

async function doAISave() {
  $("#ai-save").disabled = true;
  $("#ai-status").textContent = "保存中…";
  try {
    const r = await api("ai_save_config", {
      provider: $("#ai-provider").value,
      base_url: $("#ai-baseurl").value.trim(),
      model: $("#ai-model").value.trim(),
      api_key: $("#ai-key").value,
      enabled: true,
    });
    if (r.error) {
      $("#ai-status").textContent = r.message || r.error;
      $("#ai-status").className = "ai-status err";
      return;
    }
    $("#ai-status").textContent = "已保存（Key 方式：" + r.key_mode + "）";
    $("#ai-status").className = "ai-status ok";
    $("#ai-key").value = "";
  } finally { $("#ai-save").disabled = false; }
}

async function doAITest() {
  $("#ai-test").disabled = true;
  $("#ai-status").textContent = "测试中…";
  try {
    const r = await api("ai_test");
    if (r.ok) {
      $("#ai-status").textContent = "连接正常（模型：" + r.model + "）";
      $("#ai-status").className = "ai-status ok";
    } else {
      $("#ai-status").textContent = r.message || "测试失败";
      $("#ai-status").className = "ai-status err";
    }
  } finally { $("#ai-test").disabled = false; }
}

async function doAIDraft() {
  const topic = $("#ai-topic").value.trim();
  if (!topic) {
    $("#ai-draft-status").textContent = "请先填主题。";
    $("#ai-draft-status").className = "ai-status err";
    return;
  }
  $("#ai-draft").disabled = true;
  $("#ai-draft-status").textContent = "正在请求 AI（会联网）…";
  $("#ai-draft-status").className = "ai-status";
  try {
    const r = await api("ai_draft", {
      topic,
      context: $("#ai-context").value,
      style_hint: $("#ai-style").value,
    });
    if (r.error) {
      $("#ai-draft-status").textContent = r.message || r.error;
      $("#ai-draft-status").className = "ai-status err";
      return;
    }
    aiState.draftText = r.draft;
    aiState.draftModel = r.model;
    $("#ai-draft-text").value = r.draft;
    $("#ai-review").hidden = false;
    $("#ai-draft-status").textContent = "草稿已生成（" + r.model + "）。下面逐句核对。";
    $("#ai-draft-status").className = "ai-status ok";
    $("#ai-review").scrollIntoView({ behavior: "smooth", block: "start" });
  } finally { $("#ai-draft").disabled = false; }
}

async function doAICommit() {
  const text = $("#ai-draft-text").value.trim();
  if (!text) {
    $("#ai-commit-status").textContent = "正文是空的。";
    $("#ai-commit-status").className = "ai-status err";
    return;
  }
  const ok = window.confirm(
    "确认这段是经过你核对的内容吗？\n\n" +
    "保存后它会以「AI 协助起草」的身份进档案——这个标记会一直跟着它，\n" +
    "将来用这份档案训练模型的人能看到。");
  if (!ok) return;

  $("#ai-commit").disabled = true;
  $("#ai-commit-status").textContent = "保存中…";
  try {
    const r = await api("ai_commit_draft", {
      topic: $("#ai-topic").value.trim(),
      text,
      model: aiState.draftModel,
    });
    if (r.error) {
      $("#ai-commit-status").textContent = r.message || r.error;
      $("#ai-commit-status").className = "ai-status err";
      return;
    }
    $("#ai-commit-status").textContent =
      "已保存。标记为「" + (r.source_label || "AI 协助起草") + "」。";
    $("#ai-commit-status").className = "ai-status ok";
    $("#ai-review").hidden = true;
    $("#ai-topic").value = "";
    $("#ai-context").value = "";
    $("#ai-draft-text").value = "";
    await refreshConfig();
    await refreshOverview();
  } finally { $("#ai-commit").disabled = false; }
}

function bindAIPanel() {
  const panel = $("#ai-panel");
  if (!panel) return;
  // 第一次展开时才加载配置，避免无谓的请求
  panel.addEventListener("toggle", () => {
    if (panel.open && !aiState.loaded) {
      aiState.loaded = true;
      loadAI();
    }
  });
}

/* ── AI 批量整理（v6）────────────────────────────────────────────────── */

let digestState = { ready: false, est: null };

async function doDigestEstimate() {
  $("#digest-estimate").innerHTML = '<div class="ai-status">估算中…</div>';
  let r;
  try {
    r = await api("ai_digest_estimate", { scope: $("#digest-scope").value });
  } catch (e) {
    $("#digest-estimate").innerHTML =
      '<div class="ai-status err">估算失败：' + esc(e.message || e) + "</div>";
    return;
  }
  if (r.error) {
    $("#digest-estimate").innerHTML =
      '<div class="ai-status err">' + esc(r.message || r.error) + "</div>";
    return;
  }
  digestState.est = r;
  const rows = [
    ["将整理", fmtNum(r.records) + " 条记录"],
    ["分成", r.chunks + " 批"],
    ["预计输入", fmtNum(r.in_tokens) + " tokens"],
    ["预计输出", fmtNum(r.out_tokens) + " tokens"],
    ["服务商", (r.provider_name || "?") + " · " + (r.model || "?")],
  ].map(([k, v]) => `<div class="row"><span class="k">${esc(k)}</span><span>${esc(v)}</span></div>`).join("");

  const costHtml = r.cost_usd == null
    ? `<div class="cost">未收录</div><div class="warn">这个服务商的价格没有收录，`
      + `无法估算。请自行查阅官网。${esc(r.cost_note || "")}</div>`
    : `<div class="cost">$${r.cost_usd}</div>`
      + (r.cost_note ? `<div class="warn">${esc(r.cost_note)}。这只是估算，实际以账单为准。</div>` : "");

  $("#digest-estimate").innerHTML = `<div class="digest-box">${rows}`
    + `<div class="row" style="margin-top:8px"><span class="k">预计花费</span>`
    + `<span>${costHtml}</span></div></div>`;

  $("#digest-run").disabled = r.records === 0;
  digestState.ready = true;
  if (r.records === 0) {
    $("#digest-status").textContent = "没有可整理的记录。先导入材料。";
    $("#digest-status").className = "ai-status err";
  }
}

async function doDigestRun() {
  const scope = $("#digest-scope").value;
  const est = digestState.est;
  if (est && est.cost_usd != null) {
    const ok = window.confirm(
      "预计花费 $" + est.cost_usd + "，分 " + est.chunks + " 批，整理 "
      + fmtNum(est.records) + " 条记录。\n\n"
      + "会把这些记录的文字内容发送给你配置的 AI 服务商。\n"
      + "整理结果不会自动进档案——会先给你核对。\n\n继续？");
    if (!ok) return;
  }
  $("#digest-run").disabled = true;
  $("#digest-status").textContent = "整理中…（" + (est ? est.chunks : "?") + " 批，可能需要一两分钟）";
  $("#digest-status").className = "ai-status";
  try {
    const r = await api("ai_digest_run", { scope });
    if (r.error) {
      $("#digest-status").textContent = r.message || r.error;
      $("#digest-status").className = "ai-status err";
      return;
    }
    $("#digest-text").value = r.digest;
    $("#digest-review").hidden = false;
    const failMsg = (r.failed || []).length
      ? "　⚠ " + r.failed.length + " 批失败：" + r.failed.map(
          (f) => "第" + (f.chunk + 1) + "批 " + f.error.slice(0, 30)).join("；")
      : "";
    $("#digest-status").textContent = "完成。" + r.notice + failMsg;
    $("#digest-status").className = "ai-status ok";
    $("#digest-review").scrollIntoView({ behavior: "smooth", block: "start" });
  } finally {
    $("#digest-run").disabled = false;
  }
}

async function doDigestCommit() {
  const text = $("#digest-text").value.trim();
  if (!text) {
    $("#digest-commit-status").textContent = "摘要是空的。";
    $("#digest-commit-status").className = "ai-status err";
    return;
  }
  const ok = window.confirm(
    "确认这份摘要经过你核对了吗？\n\n"
    + "保存后它会以「AI 归纳摘要」的身份进档案，标记为 AI 产出，\n"
    + "不会被当成你本人写的材料。");
  if (!ok) return;

  $("#digest-commit").disabled = true;
  $("#digest-commit-status").textContent = "保存中…";
  try {
    const r = await api("ai_digest_commit", {
      digest: text,
      scope: $("#digest-scope").value,
      model: (digestState.est || {}).model || "",
    });
    if (r.error) {
      $("#digest-commit-status").textContent = r.message || r.error;
      $("#digest-commit-status").className = "ai-status err";
      return;
    }
    $("#digest-commit-status").textContent = "已保存，标记为 AI 归纳。";
    $("#digest-commit-status").className = "ai-status ok";
    $("#digest-review").hidden = true;
    $("#digest-text").value = "";
    await refreshConfig();
    await refreshOverview();
  } finally { $("#digest-commit").disabled = false; }
}

function bindDigestPanel() {
  const btn = $("#digest-estimate");
  if (!btn) return;
  btn.addEventListener("click", doDigestEstimate);
  const run = $("#digest-run");
  if (run) run.addEventListener("click", doDigestRun);
  const commit = $("#digest-commit");
  if (commit) commit.addEventListener("click", doDigestCommit);
  const disc = $("#digest-discard");
  if (disc) disc.addEventListener("click", () => {
    $("#digest-review").hidden = true;
    $("#digest-text").value = "";
    $("#digest-status").textContent = "已丢弃";
  });
  const scope = $("#digest-scope");
  if (scope) scope.addEventListener("change", () => {
    $("#digest-run").disabled = true;
    $("#digest-estimate").innerHTML = "";
    digestState.ready = false;
  });
}
