# O-Cleaner 项目接手指南（面向 GLM 5.3 Flash）

> **交接日期**：2026-11-21  
> **交接基线**：`v2.16+` · 仓库 `E:\O-Cleaner` · 分支 `main` · 已同步至 GitHub `origin/main`  
> **目标接手模型**：GLM 5.3 Flash（轻量高速模型，建议按「小步原子改动 + 每次改动跑对应门禁」的节奏推进）

---

## 一、30 秒建立全局心智模型

O-Cleaner 是一个 **Windows 原生、零第三方依赖、纯本地运行** 的磁盘空间分析与安全清理工具。

- **后端**：Python 3.8+ 标准库，已模块化拆分为 `ocleaner/` 11 模块包，根目录 `app.py`（21 行）作为兼容启动入口与全量 re-export 垫片。
- **前端**：单文件 `web/index.html`（原生 HTML + CSS + Vanilla ES6+，无构建步骤，内置中英双语词典 `I18N_EN`）。
- **核心架构图**：
  - `app.py` → 导出 `ocleaner/core.py`（扫描编排、识别、HTTP `Handler`）
  - `ocleaner/analysis.py` → 分析中心域（大文件排行 / 重复大文件 / 应用专清 / 毒瘤清除 / 磁盘快照 / 下载整理 / 磁盘迁移 / 实时搜索，v2.17 自 core.py 迁出）
  - `ocleaner/engines.py` → 自研 NTFS MFT 直读引擎 + WizTree 便携版下载/Authenticode 验签/CSV 解析
  - `ocleaner/guard.py` → `check_deletable` 九层防误删护栏、受保护域继承、程序本体与浏览器档案识别
  - `ocleaner/deleter.py` → 三态删除（`rm_tree_strict`）、锁定文件预检、回收站删除（带容量守卫）、断电恢复
  - `ocleaner/store.py` → `_atomic_write`、`_load_json`、`_lock`（RLock）、跨进程文件锁
  - `ocleaner/measure.py` → 进程内 `os.scandir` 测量原语（替代旧 PowerShell 测量）
  - `ocleaner/credentials.py` → Windows DPAPI 凭据加密（AI Key / Gist Token）
  - `ocleaner/games.py` → Steam / Epic / GOG / 自定义游戏库发现、中文名映射、残留检测
  - `ocleaner/integrations.py` → GitHub Gist 双向同步、自有 AI 助手、GitHub Releases 更新检查
  - `ps_scripts.py` → 6 个保留的 PowerShell 模板常量（全盘递归兜底、Steam 发现、锁进程枚举等）

---

## 二、当前状态快照（2026-11-20 已全部验证通过）

- **Git 状态**：工作区干净，`main` 已推送至 `github.com:Oerian/O-Cleaner.git`（最新提交 `56e4cb9`）。
- **门禁基线（全绿）**：
  - `tools/fix-encoding.ps1 -Check`：PASS（0 偏离）
  - `py_compile` + `pyflakes`：0 语法错、0 真实告警（`core.py` 顶部 re-export 除外）
  - `tools/lint_rules.py --strict`：0 错 0 警（5 包 / 172 条 / 208 样本 / 6 PS 模板）
  - `tools/_lint_selftest.py`：44 通过 / 0 失败
  - `tools/packs_ci_validate.py`：5 包 172 条 digest 校验通过
  - `tools/guard_matrix.py`：50 项护栏矩阵 + 规则单元自检全通过
  - `tools/unit_test.py`：38 通过 / 0 失败（含 P0 护栏防绕过、Python 3.8/3.9 AST 守卫、注册表白名单、下载归档、外置签名库安全边界与回执防路径穿越回归）
  - `tools/check_undefined.py`：61 个跨模块 `_core` 符号全可达
  - `tools/audit_dead.py`：0 死 CSS 类、0 缺失 i18n 键、0 未定义/未用 CSS 变量
  - `node --check`：`web/index.html` 两个 `<script>` 块均通过
  - `node tools/cdp_check.js http://127.0.0.1:8797`（隔离测试实例）：134 通过 / 0 失败 / 6 环境跳过
- **环境提醒**：本机 `127.0.0.1:8770` 可能已有用户常驻实例运行；开发测试请**一律使用独立端口（如 `--port 8797 --no-browser`）配合临时 `--data-dir`**，绝不触碰用户真实的 `config/ai.json`、`config/sync.json` 或 `data/`。

---

## 三、接手前必读的 8 条铁律（防踩坑清单）

1. **绝不放松安全护栏**：修改 `guard.py`（`check_deletable` / `_looks_like_installed_program`）、`identify`、`run_ps`、`auto_discovered_targets` 前，必须先读 `HANDOFF.md` 第五节踩坑史；改动后必跑 `guard_matrix.py` + `unit_test.py`。
2. **禁止直接单独 `import ocleaner.<子模块>`**：子模块对 `core` 存在 `_core.` 反向引用，测试与脚本必须先 `import ocleaner.core as app` 或 `import app`（否则触发循环导入）。
3. **Python 3.8/3.9 兼容红线**：凡在 `ocleaner/` 新增或编辑模块且使用了 `list[str]` / `str | None` 等注解，文件顶部**必须**有 `from __future__ import annotations`（已由 `unit_test.py` 的 AST 守卫强卡）。
4. **修改 `web/index.html` 禁 bash heredoc**：文件约 8,000 行，一律用精准的编辑工具修改局部块；每次改完必须跑 `node --check` 校验**全部** `<script>` 块 + `audit_dead.py`（检查死 CSS 与缺失的 `I18N_EN` 键）。
5. **编码与换行契约（CI 第一道红线）**：`.bat` = ASCII + CRLF；`.ps1` = UTF-8 with BOM + CRLF；`.py/.json/.html/.md/.yml/.js` = UTF-8 无 BOM + CRLF。每次改完文件先跑 `powershell -NoProfile -ExecutionPolicy Bypass -File tools/fix-encoding.ps1` 再跑 `-Check`。
6. **测试隔离纪律**：绝不在真实路径上跑删除测试；写配置类测试必须用临时 `--data-dir` 隔离；后台跑测试时严禁 `git add -A`，提交一律按文件名显式 `git add`。
7. **新增 UI 文案必须双语同步**：前端任何 `t("key", "中文")` 都必须在 `I18N_EN` 补充对应英文词条；新增 `render*` 函数要确认 `setLang` 重画列表覆盖；CDP/单测断言不可写死单一中文字符串（CI runner 是英文环境）。
8. **提交与发布规范**：提交说明首行只写「这次改动做了什么」，不写评价性形容词，不写 `[ci]`；文档与提交说明不写死动态浮动的断言总数（以「失败 0」为准）；日常本地提交，推送与打 tag 按用户指令执行。

---

## 四、常用命令速查表（在 `E:\O-Cleaner` 下执行）

```powershell
# 1. 编码归一化与检查（每次改动必跑）
powershell -NoProfile -ExecutionPolicy Bypass -File tools/fix-encoding.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools/fix-encoding.ps1 -Check

# 2. 快速门禁全套（<10 秒，改动 Python/配置/前端后必跑）
python -X utf8 tools/lint_rules.py --strict
python -X utf8 tools/_lint_selftest.py
python -X utf8 tools/packs_ci_validate.py
python -X utf8 tools/guard_matrix.py
python -X utf8 tools/unit_test.py
python -X utf8 tools/check_undefined.py
python -X utf8 tools/audit_dead.py

# 3. 隔离环境跑真实浏览器 CDP 检查（不干扰 8770 与真实用户数据）
$td = Join-Path $env:TEMP "oc_cdp_test"
New-Item -ItemType Directory -Path (Join-Path $td "config") -Force | Out-Null
Copy-Item config\drives.json, config\targets.json, config\convert.json, config\filetypes.json, config\kb-en.json, config\game-names.json (Join-Path $td "config")
$srv = Start-Process python -ArgumentList "-X","utf8","app.py","--port","8797","--no-browser","--data-dir",$td -PassThru -WindowStyle Hidden
Start-Sleep -Seconds 2
node tools/cdp_check.js "http://127.0.0.1:8797"
Stop-Process -Id $srv.Id -Force -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $td -ErrorAction SilentlyContinue

# 4. 打包便携版 zip（发版前重建，自动跑编码预检 + 全模块语法校验 + sha256 自检）
python -X utf8 tools/make_package.py
```

---

## 五、给 GLM 5.3 Flash 的可执行任务清单（按优先级拆分）

> 说明：原规划中的「应用专清/毒瘤签名库外置 JSON（17 类应用）」「历史回执档案检索与详情界面」「重复大文件 `mtime` + 保留最新/最早 + 扩展名筛选」已于 2026-11-21 提前落地并通过全套门禁（`unit_test` 38/38）。以下为剩余可立即开工的原子任务：

### Task 1（v2.17 首选）：从 `core.py` 拆出 `ocleaner/analysis.py`（分析中心域）
- **目标**：将 `appclean_*`、`bloatware_*`、`junction_move`/`junction_restore`、`downloads_advice`/`downloads_archive*`、`dupfiles_scan`、`snapshot_diff`/`_save_scan_snapshot`、`fs_search` 迁入 `ocleaner/analysis.py`，`core.py` 顶部 re-export，保持 `_core.` 惰性引用与 `check_undefined.py` 100% 通过。
- **改动文件**：`ocleaner/analysis.py`、`ocleaner/core.py`、`ocleaner/__init__.py`、`tools/make_package.py`（`INCLUDE` 强校验）。
- **验收**：`check_undefined.py` + `unit_test.py` + `guard_matrix.py` + `make_package.py` + `cdp_check.js` 全绿。

### Task 2（v2.17）：`lint_rules.py` 接入外置签名库校验
- **目标**：在 `tools/lint_rules.py` 增加对 `config/appclean.json` 与 `config/bloatware-builtin.json` 的结构与安全边界校验（必填字段、`kind` 合法值、禁止数据库/AI 模型目录标为 `cache`、无重复 `id`）。
- **改动文件**：`tools/lint_rules.py`、`tools/_lint_selftest.py`。
- **验收**：`lint_rules.py --strict` + `_lint_selftest.py` + `unit_test.py` 全绿。

### Task 3（v2.18）：基于快照的主动空间预警
- **目标**：基于 `data/disk-trend.json` 与 `data/snapshots/` 计算每盘近 7–14 天日均消耗与「预计剩余可用天数」，当系统盘剩余空间 < 10% 或存在 ≥3 次 `hot` 回涨目录时，在扫描概览顶部展示可关闭的预警条，并支持一键跳转对应分析工具或定时清理面板。
- **改动文件**：`ocleaner/core.py`（或 `analysis.py`）、`web/index.html`（含 `I18N_EN`）、`tools/unit_test.py`。
- **验收**：`unit_test.py` + `audit_dead.py` + `cdp_check.js` 全绿。

### Task 4（v2.18）：大文件排行自定义门槛与扩展名过滤
- **目标**：在分析中心「大文件排行」页签新增体积门槛选择（默认 / ≥100MB / ≥500MB / ≥1GB）与扩展名下拉过滤（纯前端按结果集动态生成，不重复读盘）。
- **改动文件**：`web/index.html`（含 `I18N_EN`）。
- **验收**：`node --check` + `audit_dead.py` + `cdp_check.js` 全绿。

### Task 5（v2.18）：从 `core.py` 拆出 `ocleaner/server.py`（HTTP 路由层）
- **目标**：在 Task 1 稳定后，将 `Handler` 路由类从 `core.py` 拆至 `ocleaner/server.py`，使 `core.py` 降至 ~4,500 行，进一步降低后续维护负担。
- **改动文件**：`ocleaner/server.py`、`ocleaner/core.py`、`ocleaner/__init__.py`、`tools/make_package.py`。
- **验收**：`check_undefined.py` + `unit_test.py` + `guard_matrix.py` + `make_package.py` + `cdp_check.js` 全绿。

---

## 六、关键参考文档索引

- 全量历史与踩坑库：[HANDOFF.md](../HANDOFF.md)（修改核心函数前必读第五节）
- 长期路线图：[docs/ROADMAP.md](ROADMAP.md)
- 安全模型与信任边界：[SECURITY.md](../SECURITY.md)
- 中英文使用指南：[docs/USAGE.md](USAGE.md) / [docs/USAGE-en.md](USAGE-en.md)
- 自定义数据与资源包规范：[docs/CUSTOMIZE.md](CUSTOMIZE.md) / [packs/README.md](../packs/README.md)
