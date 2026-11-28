"""O-Cleaner 后端包（v2.18 分层模块化架构）。

模块清单与职责（共 17 模块）：
- runtime.py      无环运行时地基（路径常量 / APP_VERSION / _CLEAN_LOCK / try_op_lock / _realpath_strict）
- core.py         核心入口与兼容重导出层（扫描编排 / 识别 / 规则构建 / 服务启动）
- analysis.py     分析中心域（大文件排行 / 重复检测 / 毒瘤清除 / Junction
                  搬家 / 应用专清 / 下载整理 / 快照对比 / 实时搜索 / 趋势预测）
- server.py       HTTP 路由层（/__oc__/* 全部路由 / 入口守卫 / 统一并发锁 / /js/*.js 静态服务）
- engines.py      扫描引擎（自研 NTFS MFT 直读 + C ABI v5 加速 / WizTree 加速与 WinVerifyTrust 验签）
- guard.py        九层删除护栏链 / 受保护域 / 安装目录与浏览器档案判定
- deleter.py      三态删除 / 锁定预检 / 回收站 / 重启删除 / 中断恢复
- recycle.py      回收站容量探测 / $I/$R 解析 / 一键原位还原 / 按原始路径精准清空
- locks.py        CreateFileW 占用探测 / 候选占用进程推断 / Fail-Closed 安全停进程
- tray.py         空闲端口探测 / Edge `--app` 横向桌面窗口 / Win32 纯 ctypes 系统托盘
- store.py        原子写（flush+fsync）/ 深拷贝 JSON 缓存 / RLock / 跨进程文件锁 / 备份迁移
- receipts.py     清理历史与可检索审计回执
- scheduler.py    定时自动清理调度器
- measure.py      os.scandir 测量原语（tree / glob / dir_kind / tree_size）
- credentials.py  Windows DPAPI 凭据加密（Fail-Closed）
- games.py        Steam / Epic / GOG 发现、游戏库、趋势
- integrations.py Gist 云同步 / 自有 AI 接入 / 版本自更新 / 社区目录与脱敏上传

兼容契约：根 app.py 全量 re-export core；子模块对 core 侧符号经
`_core.` 惰性引用（selftest 的 monkeypatch 落在 core 命名空间）；
新增模块必须登记 tools/make_package.py 的 INCLUDE（构建时强校验）。
"""
