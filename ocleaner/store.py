# -*- coding: utf-8 -*-
"""配置存取基础设施（P2-1 域拆分批次 B，从 core.py 迁出）。

原子写（_atomic_write）、带 mtime 缓存的 JSON 读取（_load_json）、
进程内可重入锁（_lock）与跨进程文件锁（_cross_process_lock）。
core.py 通过 re-import 取得同一份对象 —— 字典/锁按**对象共享**，
跨模块读写一致；core 内不存在对这些名字的重绑定（已核查）。

对 core 的反向依赖（RUNTIME_DIR / IS_WINDOWS）走 `_core` 惰性引用：
core 在自身初始化早期 import 本模块，本模块在**调用期**才访问 core
属性（此时 core 已全量初始化），且 monkeypatch（core.X = stub）
对 store 内调用点可见。
"""

from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import ocleaner.core as _core

_cache: dict = {}
_lock = threading.RLock()
# 可重入锁：HTTP 路由层（2026-11-05 P2-3）把 config 读-改-写整段
# 包进 _lock，路由体内嵌套调用的 append_history 等也拿同一把锁 ——
# threading.Lock 不可重入会当场死锁，必须用 RLock。


@contextmanager
def _cross_process_lock(name: str, timeout: float = 3.0):
    """跨进程互斥（同一份数据被两个实例同时改写时用）。

    为什么需要：`_lock` 只在**本进程**内有效。用户双开两个实例（各自端口）
    时，两边的 append_history 都是「读 history.json → 改 → 写回」，
    交错执行就会丢记录 —— 而且丢的是清理凭据，事后无法追溯删了什么。

    Windows 用 msvcrt 的字节区间锁，其他平台用 fcntl.flock。锁文件常驻
    数据目录（只创建不删除，避免触发本项目的安全删除钩子）。

    超时后**放行**而不是报错：宁可极小概率丢一条历史，也不能因为别人
    持锁就把用户的清理卡死。
    """
    path = _core.RUNTIME_DIR / f".oc-{name}.lock"
    fh = None
    locked = False
    try:
        try:
            _core.RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+b")
        except OSError:
            yield          # 连锁文件都建不了（只读目录）：降级为无锁
            return
        deadline = time.time() + timeout
        while True:
            try:
                if _core.IS_WINDOWS:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.02)
        yield
    finally:
        if fh is not None:
            if locked:
                try:
                    if _core.IS_WINDOWS:
                        import msvcrt
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                fh.close()
            except OSError:
                pass


def _load_json(path: Path, default, use_cache: bool = True):
    """读 JSON。utf-8-sig 容错记事本保存的 BOM；带 mtime 缓存。"""
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return default
    if use_cache and _cache.get(key, (None, None))[1] == mtime:
        return _cache[key][0]
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:
        return default
    if use_cache:
        _cache[key] = (data, mtime)
    return data


def _atomic_write(path: Path, data) -> None:
    """原子写 JSON：先写临时文件再 os.replace，避免读到半个文件。

    临时文件名必须唯一（带 pid + 线程 id + 随机串），不能用固定的 `.tmp` 后缀：
    并发写同一个目标文件时，两个线程会同时打开**同一个** tmp 并互相覆盖，
    随后 os.replace 争抢同一个源路径，Windows 上直接抛
    PermissionError: [WinError 5] 拒绝访问。实测并发 POST /__oc__/schedule
    20 路时有 3 路 500（调用点虽然持了 _lock，但 _atomic_write 还被
    append_history 等其他路径调用，锁不住所有组合）。

    os.replace 在 Windows 上偶发共享冲突（杀软/索引器短暂持有目标文件），
    这里做几次短重试 —— 写配置失败是绝对不可接受的。
    """
    tmp = path.with_name(
        f"{path.name}.{os.getpid()}.{threading.get_ident()}."
        f"{uuid.uuid4().hex[:8]}.tmp")
    try:
        # 父目录不存在就先建：用户用 --data-dir 指向一个全新空目录
        # （数据迁移场景）时，第一次保存配置不该因为「config/ 还没建」
        # 而抛 FileNotFoundError。以前这里直接 open 会失败。
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        last: Exception | None = None
        for attempt in range(4):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:   # WinError 5 / 32：被短暂占用
                last = exc
                time.sleep(0.02 * (attempt + 1))
        raise last if last else RuntimeError("os.replace 失败")
    finally:
        # 成功时 tmp 已被 replace 掉；失败时清理残留，避免在仓库里堆垃圾
        # （本项目的安全删除钩子会把这类残留当删除目标拦下，见 HANDOFF）
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        except SystemExit:
            pass
