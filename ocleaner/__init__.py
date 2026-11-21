"""O-Cleaner 后端包（P2-1 巨石拆分，2026-11-05 起，2026-11-21 更新）。

模块清单与职责（共 11 模块）：
- core.py         实现主体（扫描编排 / 识别 / HTTP 路由 Handler）
- analysis.py     分析中心域（大文件排行 / 重复检测 / 毒瘤清除 / Junction
                  搬家 / 应用专清 / 下载整理 / 快照对比 / 实时搜索）
- engines.py      扫描引擎（自研 NTFS MFT 直读 / WizTree 加速与验签）
- guard.py        九层删除护栏链 / 受保护域 / 安装目录与浏览器档案判定
- deleter.py      三态删除 / 锁定预检 / 回收站 / 中断恢复
- store.py        原子写 / JSON 缓存 / RLock / 跨进程文件锁
- measure.py      os.scandir 测量原语（tree / glob / dir_kind / tree_size）
- credentials.py  Windows DPAPI 凭据加密
- games.py        Steam / Epic / GOG 发现、游戏库、趋势
- integrations.py Gist 云同步 / 自有 AI 接入 / 版本更新检查

兼容契约：根 app.py 全量 re-export core；子模块对 core 侧符号经
`_core.` 惰性引用（selftest 的 monkeypatch 落在 core 命名空间）；
新增模块必须登记 tools/make_package.py 的 INCLUDE（构建时强校验）。
"""
