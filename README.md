# O-Cleaner

[![CI](https://github.com/Oerian/O-Cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/Oerian/O-Cleaner/actions/workflows/ci.yml)

Windows 磁盘空间分析与清理工具。本地运行，浏览器访问 Web 界面操作；
绿色免安装，不写注册表、不安装系统服务，整个目录复制即可在另一台计算机使用。

> **English** — O-Cleaner is a portable disk-space analyzer and cleanup assistant
> for Windows. It runs a local web UI (no installation, no registry writes, no telemetry);
> the interface itself is bilingual (中文 / English). Full-disk ranking with drill-down,
> a knowledge base that explains what is safe to remove, Recycle Bin handling, and an
> optional WizTree (MFT) accelerator. Zero third-party dependencies — standard-library
> Python only, or use the embeddable-runtime build. Copy the folder to another PC and it
> just works. Quick start: extract to a writable folder, run `启动 O-Cleaner.bat`,
> the UI opens at `http://127.0.0.1:8770/`. Full user guide in English:
> [docs/USAGE-en.md](docs/USAGE-en.md).

基本约束：

- **零第三方依赖**：仅使用 Python 标准库；免安装版内嵌官方 Python 运行时，无需安装 Python。
- **默认不删除任何内容**：所有删除均需手动勾选并经确认弹窗二次确认；系统目录设有硬保护，配置文件损坏时仍会拦截。
- **不采集数据**：除用户主动点击「下载 WizTree 便携版」外不发起任何网络请求；所有数据仅保存在本机。

## 快速开始

**免安装版**（推荐，内嵌官方 Python 运行时）：

1. 解压到任意可写目录（不建议 `Program Files` 等需要管理员权限的位置）；
2. 运行 `启动 O-Cleaner.bat`；
3. 浏览器自动打开界面地址（默认 `http://127.0.0.1:8770/`）。

**便携版**（约 300 KB）：要求计算机已安装 Python 3，解压后同样运行 `启动 O-Cleaner.bat`。

命令行窗口为服务进程，运行期间请勿关闭；关闭窗口即停止服务。
程序未做代码签名，首次运行时 SmartScreen 提示「已保护你的电脑」属正常现象，
选择「更多信息 → 仍要运行」即可。

命令行参数：

| 参数 | 说明 |
|---|---|
| `--port N` | 指定服务端口（默认 8770，被占用时自动后移） |
| `--no-browser` | 启动时不自动打开浏览器 |
| `--data-dir DIR` | 运行数据目录重定向（历史记录、配置、定时任务等） |
| `--scan-only` | 仅输出文字版扫描清单，不启动服务 |
| `--fullscan-only` | 仅输出磁盘占用树，不启动服务 |

## 功能

| 模块 | 说明 |
|---|---|
| 空间占用分析 | 全盘按体积排行，逐层下钻；快速扫描（1 层）定位占地大头，深层目录按需现场扫描；「大文件排行」单独列出全盘最大的单个文件（视频 / 镜像 / 安装包这类真大头）；磁盘概览提供容量、使用率与可清理/建议保留/未识别三类占比，并按盘记录空间变化趋势 |
| 清理执行 | 预定义系统级目标、四组预设（安全 / 深度 / 开发者 / 回收站）、自定义目录三种入口；占用排行内直接勾选，嵌套勾选自动去重（保留最小项，游戏本体整删不拆分）；执行前逐项列出路径、体积与文件数，完成后按项报告结果（含部分删除的残留说明）；每次清理自动留一份逐项回执 JSON（数据目录 `receipts/` 下，历史面板可随时导出） |
| 识别与释义 | 内置 42 条目录/文件识别规则；每项标注「是什么 / 能否重建 / 删除影响 / 何种情况建议保留」；内置资源包 5 组 172 条可扩展释义与游戏名，支持导入第三方资源包与自定义覆盖 |
| 游戏库 | 读取 Steam / Epic / GOG 平台清单，列出游戏体积与中文名；删除游戏前自动查询疑似存档位置并二次确认；可检测卸载残留目录 |
| 被占用文件 | 删除失败时探测被锁定文件，并列出可能占用它的进程候选（依据程序路径与命令行判断）；仅提示，不代为结束进程 |
| 删除安全 | 目录删除前先以有限预算探测锁定文件，命中即整体中止（零副作用）；删除采用「原子改名 → 递归删除 → 残留如实上报」三态流程；目录内容具有程序本体特征（卸载程序、运行时、模型权重等）时自动改判为保留 |
| 定时任务 | 可配置定时自动清理（写入 `schedule.json`，随服务进程执行） |
| 版本更新 | 「说明」面板一键检查更新（只读 GitHub Releases 最新版并提示，是否下载升级永远由用户自己决定） |
| AI 助手 | 可接入自有 AI 服务解释清理项、生成自定义方案；AI 仅能勾选候选，不能执行删除 |
| 界面 | 单文件前端；亮 / 暗 / 跟随系统三态主题；界面语言三选（跟随系统 / 中文 / English，右上角切换，偏好本机记忆，中文为源码默认文案、零外部依赖）；窄窗口下左右两栏等比缩小，不换行堆叠；累积清理统计与体积单位换算 |

可选加速：安装 WizTree 后扫描走 MFT 查表（界面内可一键下载便携版）；未安装时自动回退为标准库扫描，功能不受影响，速度较慢。

## 界面与操作

界面分三栏：**左侧栏**选盘、预设与自定义目录，**中间主区**是清理项卡片与全盘占用排行，**右侧栏**是统计与各类设置（折叠面板）。

- **两个功能页**：顶部「常规清理」与「Steam 游戏库」标签常驻可达，点标签切换，当前页高亮。
- **面板目录条**：右侧栏顶部列出全部面板标题，点击任意一项展开并滚动到位，不用逐个翻找。
- **操作主线**：选盘 → 自动扫描 → 勾选要清的项 → 点「清理选中项」→ 确认弹窗逐项核对路径与体积 → 查看实际释放量报告。
- **看清再删**：每项都有「详情」说明它是什么、能否重建、删除影响；大目录可「细分查看」到子项层级再决定；「建议保留 / 未识别」的行也可以勾选，但确认弹窗会集中红框提醒。
- **Esc** 返回上一级（下钻层级、细分查看）；扫描进行中会提示稍后再退。

逐项操作细节见 [docs/USAGE.md](docs/USAGE.md)。

## 数据与迁移

- 运行数据（历史记录、配置、定时任务、磁盘趋势等）集中于程序目录内，
  或由 `--data-dir` 指定；界面右栏「运行环境自检」显示实际位置。
- 整个目录复制即可完成迁移：路径不依赖固定盘符与用户名。
- 自定义释义、换算单位、固定项规则等可导出为 JSON 文件单独迁移。

## 安全边界

以下目标永远不会被删除（即使识别规则或配置被改坏也拦得住）：
盘符根目录、`Windows`、`Program Files`、`ProgramData`、`Users`、
`System32` / `WinSxS` / `Installer` 等系统组件、用户文档与桌面、
已安装程序本体、已下载的 AI 模型。

- 识别结论分层：资源包只能补充说明文案或重新归类未识别项，不能改写已识别项的「可否清理」结论；AI 只能勾选候选。
- 删除按风险分级：高风险项默认移入回收站（可恢复，可在界面关闭），中/低风险缓存类直接删除。
- 完整安全模型、信任边界与对抗性测试结果见 [SECURITY.md](SECURITY.md)。

## 常见问题

**界面打不开** — 查看服务进程输出的「界面地址」；端口被占用时自动后移。

**界面语言不对 / 想换语言** — 点右上角 `A / 中 / EN`：`A` 跟随浏览器语言（非中文环境显英文），`中` / `EN` 强制指定并记忆在本机。界面骨架、动态文案与知识库长文本（内置规则与资源包释义的「是什么 / 怎么处理 / 提示」、风险与置信度标签、分类短词）均已双语，英文界面下扫描结果自动显示英文说明；切换语言即时生效并重新拉取数据。

**提示需要管理员权限** — `Windows\Temp`、系统更新缓存等位置需要提权：
右键 `启动 O-Cleaner.bat` 选择「以管理员身份运行」；同批次其余项目不受单项目权限失败影响。

**部分文件删除失败** — 文件正被其他程序使用；结果弹窗里点「一键查找占用进程」，工具会先确认文件是否真的被占（可靠判定），再列出候选进程（启发式猜测）。**只列候选，不代关进程**；先在相关程序里保存并退出再重试。`System`、`explorer.exe` 等系统进程不要结束。

**清理后游戏首次启动变慢** — 着色器缓存被清除后需要重新编译，属预期行为，不影响游戏数据。

**修改配置后是否需要重启** — 不需要，刷新页面即生效。

完整说明见 [docs/USAGE.md](docs/USAGE.md)。

## 项目结构

| 位置 | 内容 |
|---|---|
| `启动 O-Cleaner.bat` | 启动脚本 |
| `app.py` | 主程序：扫描、占用分析、清理执行、HTTP 服务 |
| `web/index.html` | Web 界面（单文件） |
| `config/targets.json` | 预定义清理目标（可自行增删条目） |
| `config/filetypes.json` | 识别知识库（目录/文件规则与处置建议） |
| `packs\` | 内置资源包（释义扩展，支持导入第三方包） |
| `tools\` | 自测与检查脚本（`selftest.py` 功能自测、`lint_rules.py` 校验知识库与资源包；修改配置后建议运行） |

## 文档索引

- [docs/USAGE.md](docs/USAGE.md) — 完整使用指南
- [docs/USAGE-en.md](docs/USAGE-en.md) — User guide (English)
- [docs/ROADMAP.md](docs/ROADMAP.md) — 路线图（近期 / 中期 / 远期规划）
- [docs/CUSTOMIZE.md](docs/CUSTOMIZE.md) — 自定义数据格式（可导入，可由 AI 生成）
- [packs/README.md](packs/README.md) — 资源包格式规范
- [SECURITY.md](SECURITY.md) — 安全模型与漏洞报告
- [CONTRIBUTING.md](CONTRIBUTING.md) — 贡献流程与测试门禁

## 许可

见 [LICENSE](LICENSE)。第三方组件 WizTree 的许可与分发限制见
[docs/USAGE.md](docs/USAGE.md#明确标明第三方许可及声明)。
