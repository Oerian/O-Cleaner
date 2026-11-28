# -*- coding: utf-8 -*-
"""文件占用探测与安全进程停止域（从 core.py 迁出）。

包含 CreateFileW 无共享句柄探测（_lock_api / probe_locked）、
候选占用进程推断（find_lock_holders）及受保护系统进程名单与安全停止
（_PROTECTED_PROCESS_NAMES / _pid_process_name / stop_lock_processes）。
"""

from __future__ import annotations

import json
import os
import subprocess

import ocleaner.core as _core

LOCK_PROBE_LIMIT = 80
LOCK_CANDIDATE_LIMIT = 15
LOCK_RAW_ROW_LIMIT = 120

_LOCK_GENERIC_WRITE = 0x40000000
_LOCK_GENERIC_READ = 0x80000000
_LOCK_LIST_DIRECTORY = 0x00000001
_LOCK_OPEN_EXISTING = 3
_LOCK_FLAG_BACKUP_SEMANTICS = 0x02000000
_LOCK_ERR_ACCESS_DENIED = 5
_LOCK_ERR_SHARING_VIOLATION = 32
_LOCK_ERR_LOCK_VIOLATION = 33

_LOCK_API = None


def _lock_api():
    """惰性初始化 kernel32.CreateFileW 的原型（进程内只做一次）。"""
    global _LOCK_API
    if _LOCK_API is None:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                    wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD,
                                    wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        _LOCK_API = (ctypes, k32)
    return _LOCK_API


def probe_locked(path: str) -> dict:
    """探测一个路径当前是否被别的进程占用。"""
    out = {"path": path, "exists": os.path.exists(path), "kind": "file",
           "locked": None, "why": ""}
    if not out["exists"]:
        out["why"] = "路径不存在（可能已经删掉了）"
        return out
    if not _core.IS_WINDOWS:
        out["why"] = "占用探测仅支持 Windows"
        return out
    is_dir = os.path.isdir(path)
    out["kind"] = "dir" if is_dir else "file"
    ctypes, k32 = _lock_api()
    access = _LOCK_LIST_DIRECTORY if is_dir else (_LOCK_GENERIC_READ | _LOCK_GENERIC_WRITE)
    flags = _LOCK_FLAG_BACKUP_SEMANTICS if is_dir else 0
    h = k32.CreateFileW(path, access, 0, None, _LOCK_OPEN_EXISTING, flags, None)
    if (not h) or h == ctypes.c_void_p(-1).value:
        err = ctypes.get_last_error()
        if err in (_LOCK_ERR_SHARING_VIOLATION, _LOCK_ERR_LOCK_VIOLATION):
            out["locked"] = True
            out["why"] = ("有进程正打开着这个目录" if is_dir
                          else "有进程正打开着这个文件")
        elif err == _LOCK_ERR_ACCESS_DENIED:
            out["why"] = "无法判定：拒绝访问（以管理员身份运行能读到更多）"
        else:
            out["why"] = "无法判定：系统返回错误码 %d" % err
        return out
    k32.CloseHandle(h)
    out["locked"] = False
    out["why"] = "当前没有被占用"
    return out


def find_lock_holders(paths: list) -> dict:
    """探测这些路径的占用状态，并给出「可能是谁占用」的候选清单。"""
    clean: list[str] = []
    for p in (paths or []):
        if isinstance(p, str) and p.strip():
            clean.append(p.strip())
    truncated = len(clean) > LOCK_PROBE_LIMIT
    clean = clean[:LOCK_PROBE_LIMIT]

    results = [probe_locked(p) for p in clean]
    candidates: list[dict] = []
    ps_error = None
    if clean and _core.IS_WINDOWS:
        script = _core.PS_LOCK_HOLDERS.replace("__LIMIT__", str(LOCK_RAW_ROW_LIMIT))
        r = _core.run_ps(
            script, timeout=60,
            extra_env={"OC_TARGETS_JSON": json.dumps(clean, ensure_ascii=False)},
        )
        if isinstance(r, dict) and "error" in r:
            ps_error = str(r["error"])
        else:
            raw = [c for c in _core._as_list(r) if isinstance(c, dict)]
            merged: dict = {}
            for c in raw:
                key = c.get("pid")
                if key is None:
                    key = ("?", c.get("name"), c.get("exe"))
                cur = merged.get(key)
                if cur is None:
                    n = dict(c)
                    n["hits"] = 1
                    merged[key] = n
                else:
                    cur["hits"] = int(cur.get("hits") or 1) + 1
                    if c.get("strong") and not cur.get("strong"):
                        cur["strong"] = True
                        cur["why"] = c.get("why")
                        cur["target"] = c.get("target")
            candidates = list(merged.values())
            candidates.sort(key=lambda c: (not c.get("strong"),
                                           str(c.get("name") or "").lower()))
            candidates = candidates[:LOCK_CANDIDATE_LIMIT]
    return {"results": results, "candidates": candidates,
            "truncated": truncated, "ps_error": ps_error}


_PROTECTED_PROCESS_NAMES = {
    "system", "idle", "system idle process", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "svchost.exe", "dwm.exe", "explorer.exe", "conhost.exe",
    "audiodg.exe", "fontdrvhost.exe", "sihost.exe", "taskmgr.exe",
    "runtimebroker.exe", "searchindexer.exe",
}


def _pid_process_name(pid: int) -> str:
    """用 tasklist 读取指定 PID 的进程映像名（失败回空串）。"""
    if not _core.IS_WINDOWS or pid <= 4:
        return ""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=_core.CREATE_NO_WINDOW,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith('"'):
                return line.split('"', 2)[1].strip()
    except Exception:
        pass
    return ""


def stop_lock_processes(pids: list) -> dict:
    """按用户明确勾选的 PID 列表安全停止非系统关键进程（Fail-Closed）。"""
    if not _core.IS_WINDOWS:
        return {"ok": False, "error": "进程停止仅支持 Windows",
                "stopped": [], "skipped": [], "failed": []}
    self_pid = os.getpid()
    parent_pid = os.getppid()
    stopped: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    seen: set[int] = set()
    for raw in _core._as_list(pids)[:20]:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        if pid <= 4 or pid == self_pid or pid == parent_pid:
            skipped.append({"pid": pid, "name": "", "reason": "系统或本工具自身进程禁止结束"})
            continue
        pname = _pid_process_name(pid)
        if not pname:
            skipped.append({"pid": pid, "name": "", "reason": "无法确认进程名，出于安全拒绝结束"})
            continue
        if pname.lower() in _PROTECTED_PROCESS_NAMES:
            skipped.append({"pid": pid, "name": pname, "reason": "受保护的系统关键进程禁止结束"})
            continue
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=10,
                creationflags=_core.CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                stopped.append({"pid": pid, "name": pname or str(pid)})
            else:
                err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
                failed.append({"pid": pid, "name": pname or str(pid), "error": err})
        except Exception as exc:  # noqa: BLE001
            failed.append({"pid": pid, "name": pname or str(pid), "error": str(exc)})
    return {"ok": True, "stopped": stopped, "skipped": skipped, "failed": failed}
