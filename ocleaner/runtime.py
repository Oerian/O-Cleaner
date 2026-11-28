# -*- coding: utf-8 -*-
"""O-Cleaner 无环运行时基础模块。

仅依赖 Python 标准库，不导入任何其他 ``ocleaner.*`` 子模块，
用于提供路径常量、版本信息、并发操作锁及跨版本兼容原语。
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _argv_data_dir() -> str:
    """启动参数里找 ``--data-dir``（须在模块常量定值前生效，故在 import 期预扫）。"""
    for i, v in enumerate(sys.argv):
        if v == "--data-dir" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if v.startswith("--data-dir="):
            return v.split("=", 1)[1]
    return ""


def _read_saved_data_dir() -> str:
    """读取上次通过界面迁移保存的 ``.ocleaner-data-dir`` 指针。"""
    ptr = BASE_DIR / ".ocleaner-data-dir"
    try:
        if ptr.is_file():
            p = ptr.read_text(encoding="utf-8").strip()
            if p and os.path.isabs(p):
                return p
    except OSError:
        pass
    return ""


DATA_DIR = Path(
    os.environ.get("OC_DATA_DIR")
    or _argv_data_dir()
    or _read_saved_data_dir()
    or BASE_DIR
)
CONFIG_DIR = DATA_DIR / "config"
WEB_DIR = BASE_DIR / "web"
RUNTIME_DIR = DATA_DIR / "data"

APP_VERSION = "2.18"
APP_UA = f"O-Cleaner/{APP_VERSION}"

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000

_CLEAN_LOCK = threading.Lock()


def _cfg_or_bundled(name: str) -> Path:
    """优先读 ``DATA_DIR/config/<name>``，迁移或隔离目录缺失时回退内置 ``config/<name>``。"""
    p = CONFIG_DIR / name
    return p if p.exists() else (BASE_DIR / "config" / name)


@contextlib.contextmanager
def try_op_lock(server=None):
    """统一并发互斥锁，同时兼容测试设置的 ``server.busy`` 标记。"""
    acquired = _CLEAN_LOCK.acquire(blocking=False)
    if not acquired:
        yield False
        return
    if server is not None and getattr(server, "busy", False):
        _CLEAN_LOCK.release()
        yield False
        return
    if server is not None:
        server.busy = True
    try:
        yield True
    finally:
        if server is not None:
            server.busy = False
        _CLEAN_LOCK.release()


def _realpath_strict(p: str) -> str:
    """跨 Python 版本兼容的严格 realpath（3.8/3.9 无 ``strict`` 参数）。"""
    if sys.version_info >= (3, 10):
        return os.path.realpath(p, strict=True)
    os.stat(p)
    rp = os.path.realpath(p)
    os.stat(rp)
    return rp
