# 安全策略

O-Cleaner 按风险分级删除：中/低风险缓存类直接删除（不经过回收站），高风险项默认移入回收站（可恢复，可在界面关闭）。因此它的安全模型比只读工具更严格。

---

## 一、主要风险：规则投毒是本项目特有攻击面

本项目不存在需要用户输入才能触发的远程代码执行路径。实际风险集中在识别规则被改成把用户数据判为可清理：
`config/filetypes.json` 决定全盘扫描如何解释每一项，以及哪些项目会进入清理建议。

一条把 `Documents` 或某个软件的数据库目录写成 `cleanable: true` 的规则不会执行任何代码，
但它会让工具把这些内容列入清理建议。若用户未逐项核对就确认，会造成数据丢失。

应对措施：

| 措施 | 位置 |
|---|---|
| 规则改动必须通过 linter（`--strict`，警告计为失败），CI 强制 | `tools/lint_rules.py` |
| linter 最后一步输出全部「可清理」规则清单，供人工复核 | `lint_rules.py` 第 7 步 |
| 资源包的改动同样通过 linter（结构、清单一致性、关键词宽泛度） | `lint_rules.py` 第 2 步 |
| 高风险 + 可清理的规则必须登记豁免并写明理由与复核日期 | `lint_rules.py` 的 `ACCEPTED` |
| 删除目标另有九层硬编码保护名单，配置文件被改坏也拦截 | `ocleaner/guard.py` `PROTECTED_*` / `check_deletable()` |
| 只有 `cleanable: true` 的项目才会进入清理建议 | `ocleaner/core.py` `identify()` |
| 执行删除前弹出确认窗口，逐项列出路径、体积与文件数 | `web/app.js` / `web/js/i18n.js` |

审查规则类 PR 时，应重点看 `cleanable` 字段的改动，而不是文案改动。

---

## 二、资源包与自有 AI 的信任边界

这两项都涉及「外部输入」或「外部服务」，因此单独说明各自的权限范围。

### 资源包

资源包是从外部导入、可被替换的数据，它与内置知识库的权限**不相同**：

| | 内置知识库 | 资源包 |
|---|---|---|
| 为未识别的项目判定 `cleanable` / `risk` | 可以 | 可以 |
| 改写**已识别**项目的 `cleanable` / `risk` | — | **不可以** |
| 改写说明文字与显示名 | — | 可以 |
| 进入清理建议的前提 | 规则通过 linter 且人工复核 | 同左 |

- 已由内置规则识别的项目，其 `cleanable` 与 `risk` 一律以内置规则为准；资源包只能改写说明文字与显示名。
- 未识别的项目允许由资源包给出完整结论（包括是否可清理、风险等级）。这与内置知识库权限相同，因此资源包与知识库规则受同一道门禁约束：`tools/lint_rules.py` 会校验结构、清单与文件是否互相对得上、正则能否编译、关键词是否过于宽泛。
- 导入的资源包写入 `config/packs-user/`，与内置包同 id 时覆盖内置版本；界面里可随时停用单个包，立即生效。
- 「从 GitHub 检查更新」只接受 `https://` 地址，下载后仍按同一套结构校验，不合格的包会被拒绝并给出原因。
- 用户自己导入的包只影响本机，不会自动上报或分发。

### 自有 AI（可选）

- **默认关闭**。不填写接口地址时，程序不会发起任何 AI 相关请求。
- API Key 保存在 `config/ai.json`，该文件已加入 `.gitignore`：不进版本库，也不在 `tools/make_package.py` 的分发清单里。
- **Windows DPAPI 本机加密（`ocleaner/credentials.py`，Fail-Closed）**：API Key 落盘时通过 `CryptProtectData` 绑定当前 Windows 用户加密为 `dpapi:<base64>` 密文，用后在 `finally` 中调用 `ctypes.memset` 清零明文缓冲；若系统 DPAPI 加密失败则直接抛出 `RuntimeError` 拒绝明文保存（HTTP 接口返回 500 提示），绝不静默降级明文。即使你将整个 O-Cleaner 文件夹打包拷到其他电脑或分享给他人，其它机器也无法解密该密钥（解密失败安全回退为空串）。
- 界面对 Key 只回显尾 4 位；任何 HTTP 接口都不返回完整 Key。
- 使用该功能时，发送给接口方的内容包括：该清理项的名称、路径、体积、内置说明，以及你输入的清理要求或自定义规则文本。**不读取也不发送文件内容**。
- AI **不能执行删除**。生成方案时，候选清单只包含已被内置规则判定为可清理的项目；AI 返回的未知 id 会被丢弃并在结果中列出。方案只用于勾选复选框，实际删除仍需你确认后走底部按钮的同一链路。因此接入 AI 不会绕过第一节的任何保护。

### 云端同步、社区上传与远端抓取边界

- **云端同步（GitHub Gist，可选）**：用户自备 GitHub Personal Access Token（仅需 `gist` 权限），将自定义单位、释义覆盖与固定项规则同步到用户自己的私密 Gist。Token 同样经 **Windows DPAPI 加密**后存入本机 `config/sync.json`，换机拷贝自动失效；所有 `gist_id` 强制校验安全字符正则（`^[0-9A-Za-z_-]{1,64}$`），截断大文件的 `raw_url` 仅允许 `https://gist.githubusercontent.com` 且限制最大读取 16 MB；拉取的数据走与本地导入完全一致的 `_import_bundle` 逐项校验。
- **社区规则上传隐私脱敏（`community_upload`）**：生成公开 Gist 或 GitHub Issue 模板前，自动将路径键与描述文本中的本机用户主目录替换为 `%USERPROFILE%`、当前用户名脱敏为 `<USER>`，且默认跳过含本机私有目录结构的 `fs_groups` 列表。
- **远端 JSON 抓取 SSRF 收口（`fetch_json`）**：所有远端资源包导入与在线更新统一在 `fetch_json()` 入口调用 `_is_unsafe_remote()`（含 HTTP 重定向每跳复检与 16 MB 体积上限），硬性拦截非 `http/https` 协议、带账号密码的 URL、本机回环地址（`127.0.0.1` / `localhost.` / `::1` / `0.0.0.0`）、链路本地地址及云元数据地址（`169.254.169.254` / `metadata.google.internal` 等）。
- **WizTree 便携版与提权期 Win32 实时验签（`_authenticode_info`）**：通过纯 Win32 `wintrust.WinVerifyTrust` + `crypt32.CryptQueryObject` / `CertGetNameStringW` 实时核验程序数字签名状态为 `Valid` 且签名主体含 `Antibody Software`（不依赖易受环境污染的 PowerShell）。`find_wiztree()` 在管理员提权状态下对 `Program Files` 之外的候选执行同样强制验签；`acquire_wiztree()` 另校验 ZIP 魔数、体积与防 Zip-Slip 穿越。
- **整机备份导入白名单（`import_full_backup`）**：`store.py` 导入还原仅放行已知用户配置（大小写归一化比对，拒绝以 `AI.json` 等变体写入凭据文件）与 `data/` 允许列表，并拦截 `targets.json`、`pending-deletes.json` 及压缩包符号链接。

---

## 三、对抗性测试与回归用例

下列结论均来自实际构造的输入，对应断言已固化在 `tools/guard_matrix.py`（50 项删除护栏矩阵）、`tools/unit_test.py`（24 项秒级纯逻辑回归）与 `tools/selftest.py`（590+ 项全量端到端自测）中，可本地复跑。

### 路径护栏（`tools/guard_matrix.py` 50 项矩阵 + `tools/selftest.py` 阶段二）

| 输入类型 | 结果 |
|---|---|
| 盘符根 `C:\`、`C:\Windows`、`C:\Users`、`C:\Program Files` | 拒绝 |
| 任意层级出现 `System32`、`SysWOW64`、`Installer`、`WinSxS`、`Boot` | 拒绝 |
| 8.3 短路径混淆（如 `C:\PROGRA~1\...`） | 展开长名后拦截（`GetLongPathNameW`） |
| Windows 保留设备名（`CON`、`NUL`、`AUX`、`PRN`、`COM1`..`COM9`、`LPT1`..`LPT9`） | 拒绝 |
| 回收站目录本体 `D:\$Recycle.Bin`（非专用 `kind="recyclebin"` 通道） | 拒绝 |
| 受保护域子树（`Users\<u>\<dir>`、`AppData\<L>\<app>`、`ProgramData\<app>`、`Program Files\<app>`） | 默认拒绝整目录删除；用户显式勾选放行时**强制移入回收站** |
| 已安装程序本体及内部通用缓存容器（祖先目录含卸载程序或多版本号目录） | 拒绝单独删除 |
| O-Cleaner 仓库自身及数据目录（`BASE_DIR`） | 拒绝 |
| 顶层 NTFS Junction / 符号链接指向受保护目录（如 `D:\cache-link` → `C:\Windows`） | `_realpath_strict` 解析后递归执行整套护栏，拒绝受保护路径并透传 `real_note` 继承个人数据强制回收站约束 |
| 待删目录**子树内部**嵌套 NTFS Junction / 目录符号链接 | `_is_reparse_dir` 识别重解析点，仅 `os.rmdir` 摘除挂载点，绝不穿透目标 |
| 大小写混淆（`c:\WINDOWS\system32`）、尾随反斜杠 | 拒绝 |
| 空路径与纯空白路径 | 拒绝 |
| 非绝对路径：`C:`、`D:`、`..\Windows`、`.`、`..`、`/etc`、`temp` | 拒绝 |
| 合法目标（`C:\Windows\Temp` 等） | 放行 |
| `kind="files"` 传入目录（内部按 glob 删文件） | 放行；传入盘符根或 `System32` 仍拒绝 |

其中「空路径」与「非绝对路径」两类是回归用例：`os.path.abspath("")` 返回当前工作目录，
`os.path.abspath("E:")` 返回 E 盘上的当前工作目录（而不是盘符根）。若在归一化前不拦截，
这两类输入会被解析成一个合法目录并通过后续全部检查。

### HTTP 层与系统敏感接口护栏

服务仅绑定 `127.0.0.1`，不对局域网开放。所有 HTTP 请求（`GET` / `POST` / `DELETE`）及全部 16 条写盘/维护 POST 路由遵循以下防线：

| 输入 / 接口机制 | 结果与防护边界 |
|---|---|
| 外部或重绑定的 `Host` 头（如 `evil.example.com:8770`、不匹配绑定端口、空 `Host`） | 403（`_host_ok` 校验显式或默认 `:80` 端口一致，阻断 DNS Rebinding） |
| `Origin: null`、跨端口 Origin（如 `http://127.0.0.1` 访问 `:8770`）或异常 `Sec-Fetch-Site` | 403（`_origin_ok` 仅放行 `same-origin` / `none` 及匹配端口的 `http` 本机源） |
| 带外部 `Origin` / `Referer` 的写操作（`DELETE` 及全部 `POST` 接口） | 403 |
| 16 条清理/删除/搬家/卸载/迁移/导入 POST 路由并发请求 | `try_op_lock()` 原子获取全局 `_CLEAN_LOCK`，并发冲突返回 HTTP 409 |
| 静态脚本端点 `GET /js/<name>.js` | 仅放行 `[a-z0-9_-]+\.js` 且严格校验 `.resolve().parent == web/js` |
| 非法 JSON、JSON 数组或超上限请求体（常规 4 MB / 导入 32 MB） | 400 / 413 拒收，进程存活 |
| `stop_lock_processes` 停止占用进程 | **Fail-Closed**：拒绝 `pid <= 4`、自身/父进程、`tasklist` 查询不到进程名者，及 `_PROTECTED_PROCESS_NAMES` 系统关键进程 |
| `schedule_delete_on_reboot` 重启删除 | 必须管理员权限，逐项通过 `check_deletable` + `_realpath_strict`，拒绝重解析点/目录，仅普通文件注册 `MoveFileExW(DELAY_UNTIL_REBOOT)` |
| `sys_slim_run` 官方组件瘦身 | 命令 ID 白名单（`dism_cleanup`/`hiber_off`/`hiber_on`/`do_cache`），固定 `%SystemRoot%\System32` 绝对可执行路径，剥离 `PSModulePath` 避免 PS 模块加载错误 |
| `bloatware/uninstall` & `bloatware/sweep` | 只接受已扫描命中项；卸载命令由服务端实时读取注册表；提权管理员状态下明确拒绝执行 `HKCU` 卸载项（403）防低权注册表劫持提权 |

不带 `Origin` 的本地脚本/curl 调用照常工作，仍受 `Host` 绑定端口与全部服务端校验约束。

### 代码层

- 无 `shell=True`；无 `eval()` / `exec()` / `pickle` / `yaml.load` 等动态代码执行与反序列化调用（`config/convert.json` 的换算表达式如 `"1/3 * movie4k"` 走 `_safe_eval_unit_expr()` 的 `ast.parse` 纯算术白名单求值）
- 调用 PowerShell 时剥离 `PSModulePath` 并通过环境变量传递动态路径参数，避免注入与编码损坏
- 请求体默认上限 4 MB（整机备份导入独立限制在 32 MB 与解压总额上限），超限与畸形输入严格拒收

---

## 四、报告漏洞

请通过 GitHub Security Advisory（仓库页 Security → Report a vulnerability）私下报告，
不要开公开 issue。

报告请包含：

1. 问题类型（恶意规则 / 保护名单绕过 / 路径穿越 / 权限提升 / 其它）
2. 复现步骤，包含具体的路径与配置改动
3. 影响：可能导致用户丢失哪些数据
4. 验证环境（Windows 版本、是否管理员）

---

## 五、会被视为安全问题的情况

- 构造出能绕过 `ocleaner/guard.py` / `app.py` 保护名单的删除路径
- 使引擎把用户文档、照片、聊天记录、游戏存档或模型权重列入清理建议
- 提交把用户数据标为 `cleanable: true` 的规则
- 采集用户数据的行为（本项目声称零遥测）
- 使 `--scan-only` / `--lint` 等只读模式产生写入

## 六、不算安全问题的情况

- 清理了规则中明确标为可清理的缓存、临时文件与日志
- 需要管理员权限才能清理的系统缓存
- 杀软误报（请向杀软厂商反馈；本项目未做代码签名）
- 用户自行修改配置文件导致的行为异常
- 删除后需要重新下载等代价（已在各规则的 `advice` 字段中说明）

---

## 七、安全承诺

- **零遥测**：程序不采集、不上报任何使用数据。服务仅绑定 `127.0.0.1`。
  出网请求只有五类，且全部由用户主动触发：点击「下载 WizTree 便携版」向官方域名下载（带数字签名核验）、
  点击资源包「从 GitHub 检查更新」、在「说明」面板点击「检查更新」（只读 GitHub Releases）、
  启用自有 AI 后调用你自己配置的接口、以及在「云端同步」中推送/拉取你自己的 GitHub 私密 Gist。
  五者都不触发则完全不发起任何网络请求。可用抓包或断网运行验证。
- **零提权即可启动**：查看清单与清理用户级缓存不需要管理员权限；
  只有清理系统目录或启用内置 MFT 直读引擎时才需要提权，界面会标注哪些项需要提权以及提权后可释放多少。
- **零第三方依赖**：只使用 Python 标准库与系统自带的 PowerShell。
  不写注册表、不安装后台服务。
- **占用进程查询只在本机进行，且是只读的**：`POST /__oc__/locks` 用系统 API 判断
  「文件是否被别的进程打开」（以不共享方式试开句柄，试完立即关闭），并枚举**本机**
  进程列表来推断候选。结果只回给本机页面，不落盘、不出网。
  该接口**不会结束任何进程** —— 它只列出候选，关不关、关哪个都由使用者自己决定。
- **删除游戏有专门的窄接口，且具备占用预检与三态原子删除**：`POST /__oc__/clean-game` 只接受
  **后端自己识别为游戏安装目录**的路径（`steamapps\common` 的直接子目录，或
  Epic / GOG 清单里的精确安装路径），再过一遍与常规清理相同的系统目录保护
  （拒绝盘符根、相对路径、空路径、System32 等受保护目标）。非游戏路径一律 400，
  前端传什么都绕不过这道校验 —— 这个端点不能被当成「删除任意目录」的口子。
  执行前同样走 `_precheck_locked_files` 探测 `.exe/.dll` 锁定（命中即返回 409 整体中止，防止把运行中的游戏删成残缺目录），删除过程走 `rm_tree_strict` 三态原子改名与中断恢复流程。
- **保护优先于清理**：无法确定性质的项一律标为 `cleanable: false`。
  游戏本体是例外：它被标为 `cleanable: true`（没在玩的游戏是最值得回收的空间），
  但风险等级保持「高」，且只能通过上述专用接口、经二次确认后整体删除。

---

## 八、免责声明

本工具按「现状」提供，不对数据丢失承担责任。中/低风险项为直接删除，不可通过回收站恢复；高风险项虽默认移入回收站，仍应在删除前确认内容。
执行删除前请核对确认清单中的路径与体积；重要数据请先备份。
