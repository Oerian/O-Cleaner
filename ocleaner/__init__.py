"""O-Cleaner 后端包（P2-1 巨石拆分，2026-11-05 起步）。

拆分策略（外部审计 P2-1 / HANDOFF ⑮补）：叶子模块逐个从 app.py 迁出，
**app.py 保留在仓库根作为启动入口并 re-export 迁出符号** —— 三条兼容线
不变：`import app`（tools/selftest 等下游）、启动脚本（python app.py）、
打包清单（make_package.INCLUDE，新模块需逐个登记）。

已迁出：credentials（DPAPI 凭据加密，P1-4）、measure（os.scandir
测量原语，P2-4）。后续待迁：store（原子写/JSON 缓存/统一锁）、
guard（删除护栏链）、deleter（三态删除）、engines（MFT/WizTree/
walk）、games、integrations、server（路由表）。
"""
