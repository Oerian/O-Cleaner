"""O-Cleaner 后端包（P2-1 巨石拆分，2026-11-05 起，2026-11-07 更新）。

模块清单与职责：
- core.py      实现主体（扫描编排/识别/护栏/HTTP 路由 Handler）
- store.py     原子写 / JSON 缓存 / RLock / 跨进程文件锁
- deleter.py   三态删除 / 锁定预检 / 回收站 / 中断恢复
- measure.py   os.scandir 测量原语（tree/glob/dir_kind/tree_size）
- credentials.py  Windows DPAPI 凭据加密
- games.py     Steam/Epic/GOG 发现、游戏库、趋势
- integrations.py  Gist 云同步 / 自有 AI 接入 / 版本更新检查

兼容契约：根 app.py 全量 re-export core；子模块对 core 侧符号经
`_core.` 惰性引用（selftest 的 monkeypatch 落在 core 命名空间）；
新增模块必须登记 tools/make_package.py 的 INCLUDE（构建时强校验）。
"""
