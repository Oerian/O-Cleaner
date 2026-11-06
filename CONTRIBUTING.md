# 贡献指南

O-Cleaner 会执行直接删除，因此协作规则比普通开源项目更严格：
代码缺陷通常表现为功能异常；而规则缺陷的影响更直接 —— 它决定哪些目录会被判定为可清理。

建议先读「安全门槛」一节，它比代码风格重要。

---

## 一、安全门槛（规则类 PR 的硬要求）

`config/filetypes.json` 是全盘扫描的识别知识库。任何改动都必须满足：

### 1. 必须通过 linter

```
python -X utf8 tools/lint_rules.py --strict
```

`--strict` 会让警告也算失败 —— CI 用的就是这个模式，本地跑不过就是 CI 过不了。

linter 会做八项检查：结构校验、资源包结构、语义自相矛盾、关键词跨规则冲突、
**用真实识别引擎跑知识库自己的每个关键词验证归属**、文档与配置一致性、
内嵌脚本模板的括号配平，以及列出所有「可清理」规则供人工复核。

### 2. 规则必须说明「为什么不该删」

每条规则都要填齐：`what`（是什么）、`advice`（怎么处理）、
以及在适用时给出 `tip`（什么情况下要保留）。

**重点是 `advice` 要能劝阻错误操作**，而不是只描述能删：

```json
// 不好：只说了能删，用户不知道风险
"advice": "可以清理这个目录。"

// 好：说清了后果与正确做法
"advice": "不要直接删目录，会丢聊天记录。请在客户端里操作：设置 → 存储空间，按会话清理。"
```

### 3. `cleanable: true` 的门槛很高

标成 `cleanable: true` 意味着这个项目会进入清理建议。请确认：

- 删除后**要么能自动重建，要么是明确无用的残留**
- 不会丢失用户创作内容、聊天记录、存档、凭证
- 如果删了需要重新下载或有其他代价，**必须在 `advice` 里写清楚**

拿不准就标 `cleanable: false`。让工具少提示一点，比让用户删错东西好得多。

### 4. 高风险 + 可清理需要登记豁免

`risk: "高"` 且 `cleanable: true` 是危险组合，linter 会报警告。
如果你确认有必要（例如回收站），请在 `tools/lint_rules.py` 的 `ACCEPTED` 里登记，
并写明**理由与复核日期**：

```python
"high-risk-cleanable:your-rule-id": {
    "reason": "为什么这个是安全的、引擎与 UI 有哪些额外约束",
    "reviewed": "2026-09-16",
},
```

豁免清单本身也会被检查：**已不再触发的豁免会被要求删除**，
防止它慢慢变成掩盖新问题的垃圾场。

### 5. 关键词要避免误匹配

- 英文关键词 ≤4 字符时，引擎要求**整词边界匹配**（所以 `tim` 不会命中 `optimization`）
- 但关键词仍然越具体越好：`steamlibrary` 优于 `steam`
- 中文没有词边界，宽泛词（如「下载」「工具」）会被 linter 警告，需要走豁免流程

新增关键词后，**请自己验证不会抢走别的规则**：

```
python -X utf8 tools/lint_rules.py     # 看「路由验证」一节
```

### 6. 资源包（`packs/`）

资源包是补充/改写清理项释义的数据（格式见 [packs/README.md](packs/README.md)）。它同样受门禁约束：

```
python -X utf8 tools/lint_rules.py --strict     # 第 [2/8] 节专门检查资源包
```

- **新增包**：在 `packs/` 下放 `你的包名.json`，并把文件名加进 `packs/index.json` 的 `packs` 列表。两处必须一致 —— linter 会双向核对，漏登或写了不存在的文件都会失败。
- **包 `id` 建议与文件名相同**（去掉 `.json`），因为 `id` 决定「覆盖谁」、文件名决定「下载哪个」，不一致时远端更新会失效（此项为警告）。
- **条目必须给出 `match` 与 `title`**。`match.any` / `match.all` 里的关键词要写到足够具体：单段关键词若命中 `GENERIC_KEYWORDS` 或纯 ASCII 且短于 3 字符，会被判为过于宽泛/过短。
- **不要试图改写已识别项目的 `cleanable` / `risk`**：运行时对此有硬性限制，包只能改写说明文字与显示名；对未识别项目才可以给出完整结论。提交这类改动前请先读 [SECURITY.md](SECURITY.md) 第二节。
- 分类字段（`category`）不在前端排序表里只是排到列表末尾，不影响正确性，因此只作提示。

---

## 二、代码类 PR

### 运行测试

```
python -X utf8 tools/lint_rules.py --strict   # 规则与资源包门禁（8 项检查，警告计为失败）
python -X utf8 tools/_lint_selftest.py        # 验证 linter 有检出能力（知识库 15 类 + 资源包 20 类 + 脚本模板 5 类注入，共 44 项）
python -X utf8 tools/guard_matrix.py          # 删除护栏 50 项对抗矩阵 + 规则单元自检（秒级，动护栏必跑）
python -X utf8 tools/unit_test.py             # 纯逻辑快速单元测试（24 项，无磁盘写副作用，2 秒内跑完）
python -m pyflakes app.py ps_scripts.py ocleaner tools   # Python 静态死导入与未定义符号检查
python -X utf8 tools/selftest.py              # 590+ 项断言，含真实删除与 HTTP 端到端
node tools/cdp_check.js "http://127.0.0.1:8770"   # 真实浏览器渲染与交互检查（140+ 项断言）
```

断言数量依机器环境浮动：数据盘数量、是否装有 Steam、是否装有 WizTree
都会影响实际执行的断言条数。判断标准是失败数为 0，而非统计条数。

CDP 检查使用系统已有的 Edge / Chrome，不需要安装 playwright。本项目遵循零依赖原则，
不在用户机器上安装浏览器运行时。

### CI 什么时候跑（快慢两级）

CI 分成两级，别指望每次推送都能立刻拿到完整结论：

| 级别 | 作业 | 触发条件 | 耗时量级 |
|---|---|---|---|
| 快 | `规则 linter`（编码契约 + .py/JS 语法 + linter + 缺陷注入 + 删除护栏矩阵 `guard_matrix.py` + 快速单测 `unit_test.py`） | 每次推送、PR、手动 | 十秒级 |
| 慢 | `自测`（`selftest.py`）、`界面渲染检查`（`cdp_check.js`） | 每晚定时、或手动触发（推荐） | 分钟级 / 十分钟级 |

慢的那级之所以慢，主要成本是全盘扫描：CI 机器的系统盘塞满十万级小文件，
一次扫描要十分钟以上，而界面作业必须等它（超时按环境条件记「跳过」）。

**提交前请在本地自己跑一遍**，比等 CI 快得多。需要让某次推送也跑完整验证时，
用**手动触发**，别往提交说明里塞开关：

```
gh workflow run ci.yml --repo <owner>/<repo> --ref main
```

（工作流里还留着「提交信息含 `[ci]` 即触发慢级」的判断，但它不适合当日常用法：
提交说明应当只讲这次改动做了什么，而那个字符串会让任何含它的提交意外拉起
12 分钟流水线。）

### 改完自测请确认它还有检出能力

这里容易出现的错误是：把断言改成适配现状，而不是修 bug。

- 断言失败时，先判断是代码错了还是断言错了，不要直接改断言
- 函数改名时记得同步更新断言。曾经出现过这种情况：断言仍在检查 `renderStats`，
  而函数已改名为 `renderCumulative`，于是这条断言静默失去保护作用，却依然「通过」
- 新增功能时，同时加一条能证伪它的断言

### 前后端架构约定

- **后端模块化（`app.py` + `ocleaner/`）**：根目录 `app.py` 保持为启动入口与向后兼容导出层（re-export `ocleaner.core` 全部符号），核心实现位于 `ocleaner/` 标准库子包（`core.py`、`store.py`、`deleter.py`、`measure.py`、`credentials.py`、`games.py`、`integrations.py`）。新增子模块时必须同步加入 `tools/make_package.py` 的 `INCLUDE` 清单。
- **前端单文件（`web/index.html`）**：HTML + CSS + JS + 双语词典集中于单文件，不引入构建步骤、不引第三方库；所有动态插入 `innerHTML` 的字段必须经过 `esc()` 转义；新增渲染函数需注册到 `setLang()` 的语言切换重绘列表中。
- **零运行时依赖**：只用 Python 标准库 + 系统自带的 PowerShell / Win32 API / Edge。
- **确认与焦点约束**：危险操作（删除）必须有确认弹窗，列出确切路径与大小，且弹窗开启时焦点默认落于安全按钮（「取消」）。

### 编码与换行（必须遵守）

| 文件 | 编码 | 换行 |
|---|---|---|
| `*.bat` `*.cmd` | 纯 ASCII | CRLF |
| `*.ps1` | UTF-8 **带 BOM** | CRLF |
| `*.py` `*.json` `*.html` `*.md` `*.yml` `*.yaml` `*.js` | UTF-8 **无 BOM** | CRLF |

原因：cmd.exe 无法正确解析使用 LF 换行的批处理文件（会把下一行内容并入上一行的 `echo`）；
PowerShell 5.1 在中文 Windows 上会把没有 BOM 的文件按 GBK 读取，导致语法错误。

上述规则由 `tools/fix-encoding.ps1` 定义，CI 的 `lint` 作业第一步会用 `-Check` 校验。
新增文件后请执行一次该脚本（不带 `-Check`）以规范化编码与换行。

---

## 三、提交前自查清单

- [ ] `powershell -NoProfile -ExecutionPolicy Bypass -File tools/fix-encoding.ps1 -Check` 通过
- [ ] `python -m pyflakes app.py ps_scripts.py ocleaner tools` 0 告警
- [ ] `tools/lint_rules.py --strict` 通过
- [ ] `tools/_lint_selftest.py` 通过（linter 有检出能力）
- [ ] `tools/guard_matrix.py` 通过（删除护栏 50 项矩阵 + 规则单元自检）
- [ ] `tools/unit_test.py` 通过（24 项快速纯逻辑单测）
- [ ] `tools/selftest.py` 通过（或受宿主删除配额限制时由 CI 权威验证）
- [ ] 新增/修改的规则我亲手验证过识别结果符合预期
- [ ] 如果我改了断言，是因为代码确实对了，而不是为了让它变绿
- [ ] 危险组合（高风险 + 可清理）已登记豁免并写明理由
- [ ] 涉及新文件时，同步更新了 `docs/USAGE.md` 的目录结构清单
      （`tools/lint_rules.py` 第 5 步会校验文档与配置是否漂移，`tools/selftest.py`
      也会断言 README 里提到了 linter 与安全闸门）
- [ ] 命令或行为有变化时，`README.md`（简介）与 `docs/USAGE.md`（详述）都跟着改了

---

## 四、不能接受的内容

- 采集用户数据的任何代码（遥测、上报、"匿名统计"）
- 需要管理员权限才能启动的功能（用户应当能先看清单再决定要不要提权）
- 引入 pip / npm 依赖（违反零依赖原则）
- 把用户文档、照片、聊天记录、游戏存档、模型权重标记为 `cleanable: true`
- 绕过保护名单（`app.py` 里的 `PROTECTED_*` 常量）的改动

---

## 五、报告问题

- **功能 bug / 识别错误**：开 issue，附上目录或文件的**完整路径**与识别结果截图
- **误删或险些误删**：属于最高优先级，请标注 `data-loss` 标签并说明操作步骤
- **恶意规则**：见 [SECURITY.md](SECURITY.md)
