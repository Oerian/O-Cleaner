# -*- coding: utf-8 -*-
"""回收站管理与体积测量辅助域（从 core.py 迁出）。

包含按盘容量探测（_recycle_capacity_bytes）、$I/$R 元数据解析与枚举、
一键原位还原（recycle_restore）、按原始路径精准清理（purge_recycle_paths /
_purge_recycle_bin）及基础文件/目录体积测算（_size_of / _size_of_dir）。
"""

from __future__ import annotations

import ctypes
import os
import re
import shutil
import stat
import struct
import sys

import ocleaner.core as _core


def _recycle_capacity_bytes(drive_root: str) -> int:
    """该盘回收站的剩余可用容量（字节）。NukeOnDelete=1 或非固定盘返回 0。"""
    import winreg
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        root = drive_root if drive_root.endswith("\\") else drive_root + "\\"
        # 仅固定本地磁盘（DRIVE_FIXED == 3）有完整回收站语义；U 盘/网络盘会静默永久删除
        if k32.GetDriveTypeW(ctypes.c_wchar_p(root)) != 3:
            return 0
        guid_buf = ctypes.create_unicode_buffer(100)
        if k32.GetVolumeNameForVolumeMountPointW(
                ctypes.c_wchar_p(root), guid_buf, 100):
            vol_raw = guid_buf.value.rstrip("\\")
            m_guid = re.search(r"(\{[0-9a-fA-F-]+\})", vol_raw)
            vol_guid = m_guid.group(1) if m_guid else vol_raw
            key = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                   r"\BitBucket\Volume") + "\\" + vol_guid
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                try:
                    nuke, _ = winreg.QueryValueEx(k, "NukeOnDelete")
                    if int(nuke) != 0:
                        return 0
                except OSError:
                    pass
                mb, _ = winreg.QueryValueEx(k, "MaxCapacity")
                if isinstance(mb, int) and mb > 0:
                    cap = mb * 1024 * 1024
                    try:
                        class _SHQUERYRBINFO(ctypes.Structure):
                            _fields_ = [("cbSize", ctypes.c_uint32),
                                        ("i64Size", ctypes.c_int64),
                                        ("i64NumItems", ctypes.c_int64)]
                        s32 = ctypes.WinDLL("shell32", use_last_error=True)
                        info = _SHQUERYRBINFO(cbSize=ctypes.sizeof(_SHQUERYRBINFO))
                        if s32.SHQueryRecycleBinW(ctypes.c_wchar_p(root), ctypes.byref(info)) == 0:
                            return max(0, cap - max(0, info.i64Size))
                    except Exception:
                        pass
                    return cap
    except OSError:
        pass
    try:
        return int(shutil.disk_usage(drive_root).total * 0.05)
    except OSError:
        return 2 * 1024 * 1024 * 1024   # 兜底 2 GB


def _parse_i_file(ipath: str):
    """解析一个 $I 元数据文件 -> (原始路径, 字节数, 配对的 $R 路径)。
    解析不了（损坏/未知版本）返回 None，调用方跳过。"""
    try:
        with open(ipath, "rb") as f:
            data = f.read(2048)
    except OSError:
        return None
    if len(data) < 24:
        return None
    try:
        ver = struct.unpack_from("<q", data, 0)[0]
        size = struct.unpack_from("<q", data, 8)[0]
    except struct.error:
        return None
    if ver == 2:
        if len(data) < 28:
            return None
        try:
            (nlen,) = struct.unpack_from("<i", data, 24)
        except struct.error:
            return None
        if nlen <= 0 or nlen > 2048:
            return None
        name = data[28:28 + nlen * 2].decode("utf-16le", "ignore").rstrip("\x00")
    elif ver == 1:
        name = data[24:24 + 520].decode("utf-16le", "ignore").split("\x00")[0]
    else:
        return None
    if not name:
        return None
    rpath = os.path.join(os.path.dirname(ipath), "$R" + os.path.basename(ipath)[2:])
    return name, max(0, size), rpath


def _recycle_entries(letter: str) -> list:
    """枚举某盘回收站里全部条目（所有 SID 目录），解析 $I 元数据。"""
    root = os.path.join(f"{letter.upper()}:\\", "$Recycle.Bin")
    out = []
    if not os.path.isdir(root):
        return out
    for sid in os.listdir(root):
        siddir = os.path.join(root, sid)
        if not os.path.isdir(siddir):
            continue
        try:
            names = os.listdir(siddir)
        except OSError:
            continue
        for name in names:
            if not name.upper().startswith("$I"):
                continue
            ipath = os.path.join(siddir, name)
            info = _parse_i_file(ipath)
            if info:
                orig, size, rpath = info
                out.append({"i": ipath, "r": rpath,
                            "orig": os.path.normpath(orig).lower(), "size": size})
    return out


def recycle_restore(paths: list) -> dict:
    """回收站一键原位还原：解析 $Recycle.Bin 的 $I/$R 移回原位并删除 $I 元数据。"""
    results = []
    by_drive: dict[str, list[str]] = {}
    for p0 in paths or []:
        p = os.path.normpath(os.path.expandvars(str(p0)))
        letter = os.path.splitdrive(p)[0].rstrip(":").upper()
        if letter:
            by_drive.setdefault(letter, []).append(p)
    for letter, plist in by_drive.items():
        entries = _recycle_entries(letter)
        idx: dict[str, list] = {}
        for e in entries:
            idx.setdefault(e["orig"], []).append(e)
        for p in plist:
            key = os.path.normcase(os.path.normpath(p))
            cand = idx.get(key)
            if not cand:
                results.append({"path": p, "ok": False,
                                "error": "回收站里没有这个原始路径的条目"})
                continue
            e = cand.pop(0)          # 同路径多次删除：逐个还原
            parent = os.path.dirname(p)
            try:
                os.makedirs(parent, exist_ok=True)
                if os.path.exists(p):
                    results.append({"path": p, "ok": False,
                                    "error": "原位置已被占用，未还原"})
                    continue
                shutil.move(e["r"], p)
                try:
                    os.remove(e["i"])
                except OSError:
                    pass
                results.append({"path": p, "ok": True,
                                "size": e["size"]})
            except OSError as exc:
                results.append({"path": p, "ok": False,
                                "error": f"{type(exc).__name__}: {exc}"})
    done = sum(1 for r in results if r["ok"])
    return {"ok": done == len(results) and bool(results), "done": done,
            "total": len(results), "results": results}


def _size_of_dir(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            try:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total


def _size_of(path: str) -> int:
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            return os.path.getsize(path)
        if os.path.isdir(path):
            return _size_of_dir(path)
    except OSError:
        pass
    return 0


def purge_recycle_paths(paths: list) -> dict:
    """把给定原始路径从回收站里精确移除（只动配对的 $I/$R，不动其它条目）。"""
    wanted = []
    for p in paths or []:
        if not isinstance(p, str) or not os.path.isabs(p):
            continue
        wanted.append(os.path.normpath(p).lower())
    wanted = list(dict.fromkeys(wanted))[:200]
    results, freed_total, matched_total = [], 0, 0
    by_letter: dict[str, list[dict]] = {}
    for w in wanted:
        letter = w[:1].upper()
        if letter not in by_letter:
            by_letter[letter] = _recycle_entries(letter)
        entries = by_letter[letter]
        hits = [e for e in entries if e["orig"] == w]
        freed = 0
        for e in hits:
            sz = e["size"] or _size_of(e["r"])
            ok_r = True
            if os.path.lexists(e["r"]):
                ok_r = _core.rm(e["r"])
            ok_i = _core.rm(e["i"]) if ok_r else False
            if ok_i and ok_r:
                freed += sz
                matched_total += 1
        if not hits:
            results.append({"path": w, "matched": 0,
                            "note": "回收站里没有这个路径的条目（可能已被清空）"})
        else:
            freed_total += freed
            results.append({"path": w, "matched": len(hits), "freed": freed})
    return {"results": results, "freed": freed_total,
            "matched": matched_total,
            "freed_h": _core.human(freed_total)}


def _purge_recycle_bin(letter: str) -> dict:
    """直接删 {letter}:\\$Recycle.Bin 下各 SID 目录里的 $I/$R 文件。"""
    root = os.path.join(f"{letter.upper()}:\\", "$Recycle.Bin")
    out = {"deleted": 0, "failed": 0, "freed": 0, "fail_notes": []}
    if not os.path.isdir(root):
        return out

    def _rm_any(p: str) -> None:
        """删一个 $I/$R 条目（可能是文件也可能是目录），失败抛 OSError。"""
        if os.path.islink(p) or _core._is_reparse_dir(p):
            os.rmdir(p) if os.path.isdir(p) else os.remove(p)
        elif os.path.isdir(p):
            def _onerr(func, fp, _exc):
                if not os.path.islink(fp) and not _core._is_reparse_dir(fp):
                    os.chmod(fp, stat.S_IWRITE)
                func(fp)
            if sys.version_info >= (3, 12):
                shutil.rmtree(p, onexc=_onerr)
            else:
                shutil.rmtree(p, onerror=_onerr)
        else:
            os.chmod(p, stat.S_IWRITE)
            os.remove(p)

    for sid in os.listdir(root):
        sid_dir = os.path.join(root, sid)
        if not os.path.isdir(sid_dir):
            continue
        try:
            names = os.listdir(sid_dir)
        except OSError:
            out["fail_notes"].append(f"{sid}\\（无法读取，权限不足）")
            continue
        for n in names:
            if n[:2].upper() not in ("$I", "$R"):
                continue
            p = os.path.join(sid_dir, n)
            try:
                sz = _size_of(p)
                _rm_any(p)
                out["deleted"] += 1
                out["freed"] += sz
            except OSError as exc:
                try:
                    ctypes.windll.kernel32.SetFileAttributesW(p, 0x80)  # NORMAL
                    sz2 = _size_of(p)
                    _rm_any(p)
                    out["deleted"] += 1
                    out["freed"] += sz2
                    continue
                except OSError:
                    pass
                out["failed"] += 1
                if len(out["fail_notes"]) < 4:
                    out["fail_notes"].append(f"{sid}\\{n}（{exc.winerror or '拒绝访问'}）")
    return out
