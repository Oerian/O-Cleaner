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

from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import os
import secrets
import shutil
import sys
import time
import ctypes
import stat
from ctypes import wintypes

import ocleaner.core as _core
from ocleaner.store import _atomic_write, _load_json, _lock

def _is_reparse_dir(p: str) -> bool:
    """目录型重解析点（NTFS Junction / 目录符号链接）。

    外部审计 P1-2：待删目录内部的嵌套链接若被当成普通目录走
    rename + rmtree，存在穿透隐患。必须同时具备目录属性与重解析点属性，
    避免把 OneDrive 云文件 / WOF 压缩普通文件误判为目录链接。
    """
    try:
        st = os.lstat(p)
    except OSError:
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY) and bool(
        attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)

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
    """删除单个文件或目录（Junction/符号链接只摘挂载点），失败返回 False。"""
    def _is_link_dir(p: str) -> bool:
        try:
            st = os.lstat(p)
            attrs = getattr(st, "st_file_attributes", 0)
            return bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY) or stat.S_ISDIR(st.st_mode)
        except OSError:
            return os.path.isdir(p)

    try:
        if os.path.islink(path) or _is_reparse_dir(path):
            if _is_link_dir(path):
                os.rmdir(path)
            else:
                os.remove(path)
            return True
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            return not os.path.lexists(path)
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except (PermissionError, OSError):
        try:
            if os.path.islink(path) or _is_reparse_dir(path):
                if _is_link_dir(path):
                    os.rmdir(path)
                else:
                    os.remove(path)
                return True
            os.chmod(path, 0o700)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
                return not os.path.lexists(path)
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
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if not _is_reparse_dir(os.path.join(root, d))]
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
    if _is_reparse_dir(path) or os.path.islink(path):
        if rm(path):
            return True, False, ""
        return False, False, "目录链接无法移除"
    staging = path.rstrip("\\/") + f".oc-deleting-{os.getpid()}-{secrets.token_hex(4)}"
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
            if func not in (os.remove, os.unlink, os.rmdir):
                errs.append(p)
                return
            if not os.path.islink(p) and not _is_reparse_dir(p):
                os.chmod(p, 0o700)
            func(p)
        except Exception:
            errs.append(p)

    try:
        if hasattr(shutil, "rmtree") and sys.version_info >= (3, 12):
            shutil.rmtree(staging, onexc=_onerr)
        else:
            shutil.rmtree(staging, onerror=_onerr)
    except Exception:
        pass
    if not errs and not os.path.lexists(staging):
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

    只在「暂存存在、原名不存在」且两者确属同父目录 .oc-deleting-* 暂存时动手。
    """
    out: list[dict] = []
    with _lock:
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
            if (os.path.dirname(orig).lower() != os.path.dirname(stage).lower()
                    or not os.path.basename(stage).startswith(os.path.basename(orig.rstrip("\\/")) + ".oc-deleting-")):
                continue
            try:
                if os.path.lexists(stage) and not os.path.lexists(orig):
                    os.rename(stage, orig)
                    out.append({"restored": orig, "from": stage})
                    continue
                if os.path.lexists(stage) and os.path.lexists(orig):
                    keep.append(item)
                    out.append({"skipped": orig, "reason": "原路径已被占用"})
                    continue
            except OSError as exc:
                keep.append(item)
                out.append({"failed": orig, "error": str(exc)})
        try:
            _atomic_write(_core.PENDING_DELETES_PATH, keep)
        except Exception:
            pass
    return out


def schedule_delete_on_reboot(paths: list[str]) -> dict:
    """用 MoveFileExW(MOVEFILE_DELAY_UNTIL_REBOOT) 把顽固占用文件登记为下次重启时删除。

    仅支持 Windows 且需要管理员权限（写 HKLM Session Manager\\PendingFileRenameOperations）；
    每个路径均解析真实路径并经过完整护栏校验。
    """
    if not _core.IS_WINDOWS:
        return {"ok": False, "error": "重启删除仅支持 Windows",
                "scheduled": [], "skipped": [], "failed": []}
    if not _core.is_admin():
        return {"ok": False, "error": "标记重启删除需要管理员权限，请先一键提权重启",
                "scheduled": [], "skipped": [], "failed": []}
    MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_ex = k32.MoveFileExW
    move_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_ex.restype = wintypes.BOOL

    scheduled: list[str] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    seen: set[str] = set()
    for raw in list(paths or [])[:50]:
        p = str(raw or "").strip()
        if not p:
            continue
        norm_key = os.path.normpath(p).lower()
        if norm_key in seen:
            continue
        seen.add(norm_key)
        ok, reason = _core.check_deletable(p, allow_user_data=True)
        if not ok:
            skipped.append({"path": p, "reason": reason})
            continue
        try:
            real = os.path.realpath(p, strict=True)
        except OSError:
            skipped.append({"path": p, "reason": "无法解析真实路径"})
            continue
        if _is_reparse_dir(p) or os.path.islink(p) or not os.path.isfile(real):
            skipped.append({"path": p, "reason": "仅支持普通文件"})
            continue
        if os.path.normpath(real).lower() != norm_key:
            ok2, reason2 = _core.check_deletable(real, allow_user_data=True)
            if not ok2:
                skipped.append({"path": p, "reason": reason2})
                continue
        if move_ex(real, None, MOVEFILE_DELAY_UNTIL_REBOOT):
            scheduled.append(real)
        else:
            err_code = ctypes.get_last_error()
            failed.append({"path": real, "error": f"WinError {err_code}"})
    return {"ok": True, "scheduled": scheduled,
            "skipped": skipped, "failed": failed}
