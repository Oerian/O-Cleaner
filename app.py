# -*- coding: utf-8 -*-
"""O-Cleaner 启动入口（P2-1 巨石拆分，2026-11-05）。

实现主体已整体迁入 ocleaner/core.py（巨石一行未删、原样迁移）；
本文件只做兼容层：把 core 的全部符号 re-export 进模块命名空间，
让三条兼容线继续工作——
  1. 启动脚本与「检查更新」的 python app.py（含 --version/--data-dir
     /一键提权重启的目标路径）；
  2. `import app` 下游（tools/doctor、guard_matrix、lint_rules、
     make_package 的 load_version）；
  3. selftest / unit_test 以 `import ocleaner.core as app` 直接取得
     实现模块（monkeypatch 目标必须在 core 上）。
版本号唯一真相源 APP_VERSION 与全部实现都在 ocleaner/core.py。
"""
import ocleaner.core as _core

globals().update({k: v for k, v in vars(_core).items()
                  if not k.startswith("__")})

if __name__ == "__main__":
    _core.main()
