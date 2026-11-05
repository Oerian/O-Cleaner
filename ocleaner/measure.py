# -*- coding: utf-8 -*-
"""os.scandir 测量原语（外部审计 P2-4，从 app.py 迁出）。

替代四个测量类 PowerShell 脚本（PS_MEASURE_TREE / PS_MEASURE_GLOB /
PS_DIR_KIND / PS_STEAM_ORPHAN_SIZE）的进程内实现：每次冷启动
powershell.exe 有 180–350ms 固定开销，测量类小目标的进程开销远超
磁盘 I/O 本身。DirEntry.stat(follow_symlinks=False) 直接从
FindFirstFileW / FindNextFileW 缓冲取文件大小，零额外系统调用。
"""
import os
import stat
import time
import fnmatch


def _py_walk_files(path: str, strict: bool) -> tuple[int, int, bool]:
    """递归累加目录下的文件体积。返回 (size, files, denied)。

    strict=True：任何访问错误立即抛 PermissionError（对应 PS 的
    -ErrorAction Stop，调用方据此把「读不到」与「空目录」区分开）；
    strict=False：跳过出错的条目继续（对应 SilentlyContinue）。
    junction/symlink 一律不跟随（follow_symlinks=False），与 PS 5.1
    Get-ChildItem -Recurse 的行为一致。
    """
    total = 0
    count = 0
    denied = False
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError:
            if strict:
                raise PermissionError(cur)
            denied = True
            continue
        for e in entries:
            try:
                if e.is_file(follow_symlinks=False):
                    total += e.stat(follow_symlinks=False).st_size
                    count += 1
                elif e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
            except OSError:
                if strict:
                    raise PermissionError(e.path)
                denied = True
    return total, count, denied


def _py_measure_tree(path: str) -> dict:
    """PS_MEASURE_TREE 的进程内替代：递归测量，带 denied 标记。"""
    try:
        total, count, denied = _py_walk_files(path, strict=True)
        return {"size": total, "files": count, "denied": denied}
    except PermissionError:
        return {"size": 0, "files": 0, "denied": True}


def _py_measure_glob(path: str, pattern: str) -> dict:
    """PS_MEASURE_GLOB 的进程内替代：非递归、按通配符过滤文件求和。"""
    total = 0
    count = 0
    denied = False
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file(follow_symlinks=False) \
                            and fnmatch.fnmatch(e.name, pattern):
                        total += e.stat(follow_symlinks=False).st_size
                        count += 1
                except OSError:
                    denied = True
    except OSError:
        denied = True
    return {"size": total, "files": count, "denied": denied}


def _py_dir_kind(path: str) -> dict:
    """PS_DIR_KIND 的进程内替代：单层清单（跳过重解析点目录）。"""
    dirs: list = []
    files: list = []
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return {"denied": True, "dirs": [], "files": []}
    for e in entries:
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            continue          # 与 PS 脚本一致：Junction/链接目录不列入
        modified = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
        if stat.S_ISDIR(st.st_mode):
            dirs.append({"name": e.name, "path": e.path, "modified": modified})
        else:
            files.append({"name": e.name, "path": e.path,
                          "size": int(st.st_size), "modified": modified})
    return {"denied": False, "dirs": dirs, "files": files}


def _py_tree_size(path: str) -> int:
    """PS_STEAM_ORPHAN_SIZE 的进程内替代：宽松递归测量（错误跳过）。"""
    try:
        total, _count, _denied = _py_walk_files(path, strict=False)
        return total
    except OSError:
        return 0
