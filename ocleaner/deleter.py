# -*- coding: utf-8 -*-
"""删除执行原语（P2-1 域拆分批次 C，从 core.py 迁出）。

三态删除（rm_tree_strict：原子改名暂存 → 严格递归删除 → 断电恢复）、
锁定文件预检（_precheck_locked_files）、回收站删除（rm_trash，带容量
守卫）、长路径规范化（_long_path）与删除日志（pending-deletes.json）。

本模块是被 core.py「再绑定」的迁出域：core 顶部
`from ocleaner.deleter import ...` 保持全部既有调用点的裸名兼容；
selftest 的 monkeypatch 落在 core 命名空间（core.X = stub），本模块
对 core 侧符号（run_ps / 护栏常量 / 回收站守卫等）经 `_core.` 惰性
引用访问 —— patch 对这里可见。
"""
import os
import shutil
import time
import ctypes
import stat
from ctypes import wintypes

import ocleaner.core as _core
from ocleaner.store import _atomic_write, _load_json, _lock

def _is_reparse_dir(p: str) -> bool:
    """目录型重解析点（NTFS Junction / 目录符号链接）。

    外部审计 P1-2：待删目录内部的嵌套链接若被当成普通目录走
    rename + rmtree，存在穿透隐患。防御纵深：枚举子项时遇到重解析点
    只摘挂载点本身（os.rmdir 不跟随），绝不进入其目标。CPython 3.8+
    的 shutil.rmtree 虽已不跟随 junction，这里显式兜住所有下游路径。
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT)

def _long_path(p: str) -> str:
    """把 8.3 短名（PROGRA~1）展开成长名，失败时原样返回。

    Windows 每个目录都有一个 8.3 短名别名，短名和长名指向同一个目录。
    护栏是按路径**分段名**比对的，短名 `PROGRA~1` 不会命中 `program files`
    这条规则 —— 也就是说 `C:\\PROGRA~1\\xxx` 可以绕过整张名单。
    这里用 GetLongPathNameW 把短名展开后再走同一套检查。
    """
    if not _core.IS_WINDOWS or "~" not in p:
        return p
    try:
        import ctypes
        from ctypes import wintypes
        get_long = ctypes.windll.kernel32.GetLongPathNameW
        get_long.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_long.restype = wintypes.DWORD
        buf = ctypes.create_unicode_buffer(32768)
        n = get_long(p, buf, 32768)
        if n and buf.value:
            return buf.value
    except Exception:
        pass
    return p

def rm(path: str) -> bool:
    """删除单个文件或目录，失败返回 False。"""
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
            return not os.path.exists(path)
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except (PermissionError, OSError):
        try:
            os.chmod(path, 0o700)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                return not os.path.exists(path)
            os.remove(path)
            return True
        except Exception:
            return False

def _precheck_locked_files(path: str) -> list[str]:
    """删除前预检：目录内是否有**确定被占用**的文件。返回被占用文件列表。

    只统计 locked=True 的确定结论；权限不足（无法判定）不算占用 ——
    那是「探测不到」而不是「被占用」，不能反过来当成中止删除的理由。
    优先探测 .exe/.dll（运行中的软件锁住的正是它们）。
    """
    locked: list[str] = []
    probed = 0
    for root, _dirs, files in os.walk(path):
        files.sort(key=lambda n: 0 if n.lower().endswith((".exe", ".dll")) else 1)
        for f in files:
            if probed >= _core._LOCK_PRECHECK_BUDGET or len(locked) >= _core._LOCK_PRECHECK_REPORT_MAX:
                return locked
            probed += 1
            fp = os.path.join(root, f)
            pr = _core.probe_locked(fp)
            if pr.get("locked") is True:
                locked.append(fp)
    return locked

def rm_trash(path: str) -> tuple[bool, str]:
    """把文件/目录移入回收站。返回 (是否成功, 失败原因)。"""
    import ctypes
    from ctypes import wintypes
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return True, ""
    root = os.path.splitdrive(path)[0] + "\\"
    cap = _core._recycle_capacity_bytes(root)
    size = _core._size_of(path)
    if size > cap:
        return False, (f"体积 {_core.human(size)} 超过该盘回收站容量上限（约 {_core.human(cap)}），"
                       f"移入回收站会被系统转为永久删除，已拒绝。"
                       f"可直接删除，或在回收站设置里调大上限后再试。")
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", wintypes.WORD), ("fAnyOperationsAborted",
                                                wintypes.BOOL),
                    ("hNameMappings", wintypes.LPVOID),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = 3                       # FO_DELETE
    op.pFrom = path + "\0"             # 双 \0 结尾由 ctypes 自动补一个
    op.fFlags = (_core._FOF_ALLOWUNDO | _core._FOF_NOCONFIRMATION | _core._FOF_SILENT
                 | _core._FOF_NOERRORUI)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code == 0 and not op.fAnyOperationsAborted:
        return True, ""
    return False, f"系统回收站操作失败（错误码 {code}）"

def rm_tree_strict(path: str) -> tuple[bool, bool, str]:
    """三态删除目录：返回 (是否全删, 是否部分删除, 说明)。

    全删 = 目录已不存在；部分删除 = 已从原位置移除、内部有删不掉的文件
    残留在 .oc-deleting-* 暂存目录；两者都失败 = 原目录原封不动。

    改名暂存前先落一条「待恢复」日志：改名和递归删除之间不是原子的，
    这中间进程被杀（宿主安全钩子抛 SystemExit、用户关掉窗口、断电），
    用户看到的就是「我的 Adobe 文件夹不见了，多了一个
    Adobe.oc-deleting-05462600」。下次启动时按日志改回原名
    （见 recover_pending_deletes）。
    """
    staging = path.rstrip("\\/") + f".oc-deleting-{time.time_ns() % 10**8:08d}"
    _journal_stage(path, staging)
    try:
        os.rename(path, staging)
    except OSError as exc:
        _journal_done(staging)
        return False, False, (f"目录被占用或无法移动，未删除任何内容"
                              f"（{exc.strerror or exc}）")
    errs: list[str] = []

    def _onerr(func, p, _excinfo):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            errs.append(p)

    try:
        shutil.rmtree(staging, onerror=_onerr)
    except Exception:
        pass
    if not errs and not os.path.exists(staging):
        _journal_done(staging)
        return True, False, ""
    # 部分残留：日志保留，下次启动会把剩下的内容改回原名交还给用户
    # （残缺但位置正确，比「名字怪异的孤儿目录」好处理）。
    tail = staging if len(staging) <= 80 else "…" + staging[-77:]
    return False, True, (f"已从原位置移除，但内部有删不掉的文件，"
                         f"残留在 {tail}（确认相关程序已退出后可手动删除）")

def _journal_stage(original: str, staging: str) -> None:
    """登记一条「即将改名删除」，供下次启动恢复。失败不影响删除本身。"""
    try:
        with _lock:
            data = _load_json(_core.PENDING_DELETES_PATH, [], use_cache=False)
            if not isinstance(data, list):
                data = []
            data.append({"original": original, "staging": staging,
                         "ts": time.time()})
            _atomic_write(_core.PENDING_DELETES_PATH, data[-200:])
    except Exception:
        pass

def _journal_done(staging: str) -> None:
    """删除已了结（成功或彻底失败），把这条登记去掉。"""
    try:
        with _lock:
            data = _load_json(_core.PENDING_DELETES_PATH, [], use_cache=False)
            if not isinstance(data, list):
                return
            left = [d for d in data
                    if not (isinstance(d, dict) and d.get("staging") == staging)]
            if len(left) != len(data):
                _atomic_write(_core.PENDING_DELETES_PATH, left)
    except Exception:
        pass

def recover_pending_deletes() -> list[dict]:
    """启动时把上次中断留下的暂存目录改回原名。

    只在「暂存存在、原名不存在」时动手 —— 两个都在说明用户自己新建了同名
    目录，这时候贸然覆盖就是第二次伤害。
    """
    out: list[dict] = []
    try:
        data = _load_json(_core.PENDING_DELETES_PATH, [], use_cache=False)
    except Exception:
        return out
    if not isinstance(data, list) or not data:
        return out
    keep: list = []
    for item in data:
        if not isinstance(item, dict):
            continue
        orig, stage = str(item.get("original") or ""), str(item.get("staging") or "")
        if not orig or not stage:
            continue
        try:
            if os.path.exists(stage) and not os.path.exists(orig):
                os.rename(stage, orig)
                out.append({"restored": orig, "from": stage})
                continue
            if os.path.exists(stage) and os.path.exists(orig):
                # 两个都在：保留暂存，但不再自动处理，交给用户
                keep.append(item)
                out.append({"skipped": orig, "reason": "原路径已被占用"})
                continue
            # 暂存不存在 = 上次已删干净，登记作废
        except OSError as exc:
            keep.append(item)
            out.append({"failed": orig, "error": str(exc)})
    try:
        _atomic_write(_core.PENDING_DELETES_PATH, keep)
    except Exception:
        pass
    return out
