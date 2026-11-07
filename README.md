# O-Cleaner

[![CI](https://github.com/Oerian/O-Cleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/Oerian/O-Cleaner/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078D4)
![Python](https://img.shields.io/badge/python-3.8%2B%20%7C%20stdlib%20only-3776AB)
![Dependencies](https://img.shields.io/badge/dependencies-zero-22c55e)
![License](https://img.shields.io/badge/license-MIT-64748b)

Windows 磁盘空间分析与清理工具。本地运行，浏览器访问 Web 界面操作；
绿色免安装，不写注册表、不安装系统服务，整个目录复制即可在另一台计算机使用。

> **English** — O-Cleaner is a portable disk-space analyzer and cleanup assistant
> for Windows. It runs a local web UI (no installation, no registry writes, no telemetry);
> the interface itself is bilingual (中文 / English). Full-disk ranking with drill-down,
> a knowledge base that explains what is safe to remove, Recycle Bin handling, and
> built-in MFT direct-read acceleration (administrator) with optional WizTree
> integration — the engine is selectable and auto-picks the faster one. Zero
> third-party dependencies — standard-library Python only, or use the
> embeddable-runtime build. Copy the folder to another PC and it
> just works. Quick start: extract to a writable folder, run `启动 O-Cleaner.bat`,
> the UI opens at `http://127.0.0.1:8770/`. Full user guide in English:
> [docs/USAGE-en.md](docs/USAGE-en.md).

基本约束：

- **零第三方依赖**：仅使用 Python 标准库；免安装版内嵌官方 Python 运行时，无需安装 Python。
- **默认不删除任何内容**：所有删除均需手动勾选并经确认弹窗二次确认；系统目录设有硬保护，配置文件损坏时仍会拦截。
- **不采集数据**：无任何遥测；除用户主动触发的五类可选操作（下载 WizTree 便携版、更新资源包、检查版本更新、调用自配 AI、同步自配 GitHub Gist）外不发起任何网络请求；敏感凭据（AI 密钥、Gist 令牌）经 Windows DPAPI 绑定当前用户加密落盘，拷贝文件夹到其他电脑自动失效不泄露。

## 快速开始

**免安装版**（推荐，内嵌官方 Python 运行时）：

1. 解压到任意可写目录（不建议 `Program Files` 等需要管理员权限的位置）；
2. 运行 `启动 O-Cleaner.bat`；
3. 浏览器自动打开界面地址（默认 `http://127.0.0.1:8770/`）。

**便携版**（约 650 KB）：要求计算机已安装 Python 3，解压后同样运行 `启动 O-Cleaner.bat`。

命令行窗口为服务进程，运行期间请勿关闭；关闭窗口即停止服务。
程序未做代码签名，首次运行时 SmartScreen 提示「已保护你的电脑」属正常现象，
选择「更多信息 → 仍要运行」即可。

命令行参数：

| 参数 | 说明 |
|---|---|
| `--port N` | 指定服务端口（默认 8770，被占用时自动后移） |
| `--no-browser` | 启动时不自动打开浏览器 |
| `--data-dir DIR` | 运行数据目录重定向（历史记录、配置、定时任务等，也可用环境变量 `OC_DATA_DIR`） |
| `--scan-only` | 仅输出文字版扫描清单，不启动服务（可配合 `--drive D`） |
| `--fullscan-only` | 仅输出磁盘占用树，不启动服务（可配合 `--drive D --depth N`） |
| `--lint` | 仅运行知识库与资源包规则 linter（`--strict`），不启动服务 |
| `--version` | 打印当前版本号后退出 |

## 功能

| 模块 | 说明 |
|---|---|
| 空间占用分析 | 全盘按体积排行，支持点击逐层下钻与行内就地懒加载展开；快速扫描（默认 1 层）定位占地大头，深层目录按需现场扫描；「大文件排行」单独列出全盘最大的单个文件（支持按近 7 / 30 / 90 天过滤）；磁盘概览提供占比环与可清理/建议保留/未识别三类占比，环右侧组件图表坞三页签可切换：剩余空间趋势（按盘每日快照竖向柱状图）、清理记录、类型分布 |
| 重复文件与下载整理 | 「重复大文件」基于全盘扫描缓存按同体积分组 + 首尾 1 MB 采样哈希证实，只列出内容确认为副本的大文件组与可回收体积（只读不自动删）；「下载整理」对下载目录顶层文件按知识库自动归类并给出整理建议 |
| **可逆清理闭环** | 回收站一键原位还原、快照 Diff「近期暴涨」（高频回涨标记）、下载整理勾选清理与按类型归档（可撤销） |
| **应用专清与搬家** | 微信/开发者缓存等 10 类签名结构化拆解（cache/data/core 三级语义）；C 盘目录无损搬家至其他盘（Junction 透明映射，一键迁回） |
| **无头与桌面化** | `--auto-clean` 无头单次清理（配任务计划程序）；`--app` Edge 无边框窗口模式 |
| 清理执行 | 预定义系统级目标、四组预设（安全 / 深度 / 开发者 / 回收站）、自定义目录三种入口；占用排行内直接勾选，嵌套勾选自动去重（保留最小项，游戏本体整删不拆分）；执行前逐项列出路径、体积与文件数，完成后按项报告结果（含部分删除的残留说明与贡献前三）；每次清理自动留一份逐项回执 JSON（数据目录 `data/receipts/` 下，历史面板可随时导出） |
| 识别与释义 | 内置 42 条目录/文件识别规则；每项标注「是什么 / 能否重建 / 删除影响 / 何种情况建议保留」；内置资源包 5 组 172 条可扩展释义与游戏名，支持导入第三方资源包、右键自定义徽章/颜色/提级置顶与整目录永久排除 |
| 游戏库 | 读取 Steam / Epic / GOG 平台清单，合盘列出游戏体积与中文名；删除游戏前自动查询疑似存档位置并二次确认；各平台卸载残留与「很久没玩的大作」统一聚合在「回收建议」，勾选后并入底部「清理选中项」一次删除（行级进度反馈）；检测到残留时首页横幅主动提醒；游戏库一周增长超 15% 会提示；游戏库总体积趋势曲线（hover 显示每日数值）；游戏行内嵌封面缩略图（Steam 读本机客户端缓存，Epic/GOG 与无图标游戏显示统一占位图，可在自定义游戏库分区更换）；排序支持方向切换；游戏清单可一键导出 CSV / Markdown；高频删除可勾选「不再显示长说明」 |
| 被占用文件 | 删除失败时探测被锁定文件，并列出可能占用它的进程候选（依据程序路径与命令行判断）；仅提示，不代为结束进程 |
| 删除安全 | 目录删除前先以有限预算探测锁定文件，命中即整体中止（零副作用）；删除采用「预写日志 → 原子改名 → 递归删除 → 残留如实上报 / 中断启动自愈」三态流程；子树遇重解析点（Junction/Symlink）仅摘挂载点不穿透；目录内容具有程序本体特征（卸载程序、运行时、模型权重等）时自动改判为保留 |
| 云端同步与数据迁移 | 支持把换算单位、释义覆盖与固定项规则导出为单个 JSON 数据包，或通过用户自备的 GitHub 私密 Gist 双向同步；令牌经 Windows DPAPI 加密存储于本机 `config/sync.json` |
| 定时任务 | 可配置定时自动清理（写入 `data/schedule.json`，随服务进程执行，后台完成后下次打开界面顶部横幅汇报成果） |
| 版本更新 | 「说明」面板一键检查更新（只读 GitHub Releases 最新版并提示，是否下载升级永远由用户自己决定） |
| AI 助手 | 可接入自有 AI 服务（DeepSeek / 本机 Ollama 等 OpenAI 兼容接口）解释清理项、生成自定义方案；密钥经 DPAPI 加密存本机；AI 仅能勾选候选，不能执行删除 |
| 界面与无障碍 | 单文件前端；亮 / 暗 / 跟随系统三态主题；界面语言三选（跟随系统 / 中文 / English，右上角切换，偏好本机记忆，中文为源码默认文案、零外部依赖）；支持舒适/紧凑两档行密度、占用排行键盘导航（`↑`/`↓` 移焦、`Space` 勾选、`Enter` 下钻、`Esc` 逐级返回）与弹窗焦点陷阱；窄窗口下左右两栏等比缩放不堆叠；累积清理统计与趣味体积单位换算 |

扫描加速三选一（工具栏「引擎」下拉，默认「自动」实测择快，跨设备拷贝后自动重新校准）：
**WizTree 查表**（无需管理员，界面内可一键下载官方便携版并自动校验 Authenticode 数字签名）、
**内置 MFT 直读引擎**（v2.11 起，管理员权限下直接解析 NTFS 主文件表，免第三方组件、无临时文件）、
或标准库 `os.scandir` / PowerShell 递归扫描（兜底，功能不受影响，速度较慢）。

## 界面与操作

界面采用**顶栏 + 左右双栏独立滚动 + 常驻底栏**布局：**顶栏**选盘、切换功能视图、切换主题与语言；**左侧主区**是工具栏、磁盘概览（含占比环与三页签组件图表坞）及当前功能视图（占用排行树或游戏占用概览）；**右侧栏**是运行环境自检、一键预设、统计与各类设置（可折叠面板）。

- **两个功能页**：顶部「扫描概览」与「游戏占用概览」分段按钮常驻可达，点按钮即时切换，当前页高亮。
- **面板目录条**：右侧栏顶部吸顶列出全部折叠面板标题，点击任意一项自动展开并滚动到位，不用逐个翻找。
- **操作主线**：选盘 → 自动扫描 → 勾选要清的项（或点右侧「一键预设」）→ 点底部「清理选中项」→ 确认弹窗逐项核对路径与体积 → 查看实际释放量报告与贡献前三。
- **看清再删**：每行都有「详情」说明它是什么、能否重建、删除影响，并支持行内「展开 ▾」就地查看子目录或「归档」进自定义聚合组；大目录可「细分查看」拆出缓存子目录；「建议保留 / 未识别」的行也可以勾选，但确认弹窗会集中红框提醒，且个人数据/程序数据目录整删时强制移入回收站兜底。
- **键盘操作**：占用排行支持 `↑` / `↓` 移动焦点行、`Space` 勾选/取消、`Enter` 下钻进入子目录、`Esc` 逐级返回（关闭弹窗 → 退回上一层目录 → 返回扫描概览）。

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
右键 `启动 O-Cleaner.bat` 选择「以管理员身份运行」；或在页面顶部提示条点「一键提权重启」——弹 UAC 确认后自动以管理员身份重启（无黑色命令行窗口），页面自动恢复；同批次其余项目不受单项目权限失败影响。

**部分文件删除失败** — 文件正被其他程序使用；结果弹窗里点「一键查找占用进程」，工具会先确认文件是否真的被占（可靠判定），再列出候选进程（启发式猜测）。**只列候选，不代关进程**；先在相关程序里保存并退出再重试。`System`、`explorer.exe` 等系统进程不要结束。

**清理后游戏首次启动变慢** — 着色器缓存被清除后需要重新编译，属预期行为，不影响游戏数据。

**修改配置后是否需要重启** — 不需要，刷新页面即生效。

完整说明见 [docs/USAGE.md](docs/USAGE.md)。

## 项目结构

| 位置 | 内容 |
|---|---|
| `启动 O-Cleaner.bat` | 启动脚本（内置 Python 3.8+ 真实运行时校验） |
| `app.py` | 启动入口与向后兼容导出层（CLI 参数分发、服务启动） |
| `ocleaner\` | 核心业务标准库包（`core.py` 主逻辑、`store.py` 原子存取与锁、`deleter.py` 三态删除与占用预检、`measure.py` 进程内 `os.scandir` 测量、`credentials.py` Windows DPAPI 凭据加密、`games.py` 全平台游戏库发现与趋势、`integrations.py` 自有 AI / Gist 云同步 / 版本更新检查） |
| `ps_scripts.py` | PowerShell 脚本模板常量（全盘树回退、进程占用候选枚举、Steam 清单等） |
| `web/index.html` | Web 界面（单文件 HTML + CSS + JS，内置中英双语词典） |
| `config/targets.json` | 预定义清理目标与一键预设（可自行增删条目） |
| `config/filetypes.json` | 识别知识库（42 条目录/文件规则与处置建议） |
| `config/kb-en.json` | 内置知识库与资源包长文本的英文译文 |
| `packs\` | 内置资源包（5 组 172 条释义扩展，支持导入第三方包与在线更新） |
| `data\` | 运行数据目录（`history.json`、`schedule.json`、`disk-trend.json`、`pending-deletes.json`、`receipts\`） |
| `tools\` | 质量门禁与打包脚本（`lint_rules.py` 校验知识库与资源包安全闸门、`guard_matrix.py` 50 项删除护栏矩阵、`unit_test.py` 秒级纯逻辑单测、`selftest.py` 全量功能自测、`cdp_check.js` 浏览器渲染检查、`make_package.py` / `make_exe.py` 发行打包；贡献规则或修改配置前必须通过闸门） |

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
