# -*- coding: utf-8 -*-
"""一次性补丁：P2-4 测量类 PowerShell 换 os.scandir（跑完即删）"""
src = open('app.py', encoding='utf-8').read()

def rep(old, new, tag):
    global src
    assert src.count(old) == 1, f"pattern not unique/found: {tag}"
    src = src.replace(old, new)
    print(f"  ok: {tag}")

# ---- 纯 Python 测量原语（os.scandir 的 DirEntry.stat(follow_symlinks=False)
#      直接从 FindFirstFileW/FindNextFileW 缓冲取大小，零额外系统调用） ----
anchor = "def _measure_existing(t: dict, path: str, drive: str | None) -> dict:"
helpers = '''def _py_walk_files(path: str, strict: bool) -> tuple[int, int, bool]:
    """递归累加目录下的文件体积。返回 (size, files, denied)。

    P2-4：替代 PS_MEASURE_TREE / PS_STEAM_ORPHAN_SIZE 的进程内实现 ——
    每次冷启动 powershell.exe 有 180–350ms 固定开销，测量类小目标
    （几十个崩溃转储、几 MB 日志）进程开销远超磁盘 I/O 本身。
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
                    if e.is_file(follow_symlinks=False) \\
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


def _measure_existing(t: dict, path: str, drive: str | None) -> dict:'''
rep(anchor, helpers, "helpers")

rep('''    if kind == "files":
        script = (PS_MEASURE_GLOB
                  .replace("__PATH__", path.replace("'", "''"))
                  .replace("__GLOB__", str(t.get("glob", "*")).replace("'", "''")))
    else:
        script = PS_MEASURE_TREE.replace("__PATH__", path.replace("'", "''"))
    r = run_ps(script)
    if r.get("denied"):''',
'''    # P2-4：进程内测量（免 powershell.exe 冷启动，180–350ms/次）
    if kind == "files":
        r = _py_measure_glob(path, str(t.get("glob", "*")))
    else:
        r = _py_measure_tree(path)
    if r.get("denied"):''', "_measure_existing")

rep('''    r = run_ps(PS_DIR_KIND.replace("__PATH__", path.replace("'", "''")), timeout=600)
    if "error" in r:''',
'''    r = _py_dir_kind(path)   # P2-4：进程内单层清单，免 PS 冷启动
    if r.get("error") in (None, False) and False:
        pass
    if r.get("error"):''', "dir-kind call")

# 上面塞了个永假的占位 if 不优雅 —— 干脆撤掉，直接去掉 error 判断：
rep('''    r = _py_dir_kind(path)   # P2-4：进程内单层清单，免 PS 冷启动
    if r.get("error") in (None, False) and False:
        pass
    if r.get("error"):''',
'''    r = _py_dir_kind(path)   # P2-4：进程内单层清单，免 PS 冷启动
    if r.get("error"):''', "dir-kind call cleanup")

# ---- 孤儿体积：4188（分区接口） ----
rep('''    if need_size:
        # 临时清单必须放系统临时目录、且文件名唯一：
        # 1) 以前写在 BASE_DIR 下（仓库目录），与 HANDOFF 踩坑史里
        #    oc-steam-orphans.tmp 是同一模式 —— 在带安全删除钩子的宿主环境里
        #    会耗尽删除配额，钩子抛 SystemExit 直接打断请求（实测已复现：
        #    safe-delete 钩子拦下 E:\\O-Cleaner\\.oc-partition.tmp，请求被断开）。
        # 2) 固定文件名在并发请求下会互相覆盖，A 读到 B 的路径列表。
        lst = Path(tempfile.gettempdir()) / f"oc-partition-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
        try:
            lst.write_text("\\n".join(need_size[:80]), encoding="utf-8")
            rs = run_ps(PS_STEAM_ORPHAN_SIZE.replace(
                "__LIST__", str(lst).replace("'", "''")), timeout=1800)
            if "error" not in rs:
                sizes = {os.path.normpath(str(x.get("path", ""))).lower():
                         int(x.get("size") or 0)
                         for x in _as_list(rs) if isinstance(x, dict)}
                for x in cleanable + keep:
                    if x["type"] == "dir":
                        x["size"] = sizes.get(os.path.normpath(x["path"]).lower(), 0)
        except OSError:
            pass''',
'''    if need_size:
        # P2-4：进程内测量（免临时清单文件 + PS 冷启动）。临时清单文件
        # 方案曾在带安全删除钩子的宿主环境里耗尽删除配额、钩子抛
        # SystemExit 打断请求（实测复现过）—— 现已无任何落盘。
        sizes = {}
        for _p in need_size[:80]:
            sizes[os.path.normpath(_p).lower()] = _py_tree_size(_p)
        for x in cleanable + keep:
            if x["type"] == "dir":
                x["size"] = sizes.get(os.path.normpath(x["path"]).lower(), 0)''',
    "orphan partition")

# ---- 孤儿体积：4492（Steam 残留） ----
rep('''        lst = Path(tempfile.gettempdir()) / "oc-steam-orphans.tmp"
        try:
            lst.write_text("\\n".join(o["path"] for o in orphans[:40]),
                           encoding="utf-8")
            rs = run_ps(PS_STEAM_ORPHAN_SIZE.replace(
                "__LIST__", str(lst).replace("'", "''")), timeout=900)
            if "error" not in rs:
                sizes = {os.path.normpath(str(s.get("path", ""))).lower():
                         int(s.get("size") or 0)
                         for s in _as_list(rs) if isinstance(s, dict)}
                for o in orphans:
                    sz = sizes.get(os.path.normpath(o["path"]).lower(), 0)
                    o["size"] = sz
                    o["size_h"] = human(sz)
                orphans = [o for o in orphans if o["size"] > 0]
                orphans.sort(key=lambda o: -o["size"])
        except OSError:
            pass''',
'''        # P2-4：进程内测量（免临时清单 + PS 冷启动）；与旧实现一致，
        # 只测前 40 个，其余体积记 0 并在下面被过滤掉。
        _sizes = {}
        for _o in orphans[:40]:
            _sizes[os.path.normpath(_o["path"]).lower()] = _py_tree_size(_o["path"])
        for o in orphans:
            sz = _sizes.get(os.path.normpath(o["path"]).lower(), 0)
            o["size"] = sz
            o["size_h"] = human(sz)
        orphans = [o for o in orphans if o["size"] > 0]
        orphans.sort(key=lambda o: -o["size"])''',
    "orphan steam")

# ---- 导入保留注释（selftest 阶段六直接引用 app.PS_STEAM_ORPHAN_SIZE） ----
rep('''from ps_scripts import (PS_BIG_FILES, PS_DIR_KIND, PS_LOCK_HOLDERS,
                        PS_MEASURE_GLOB, PS_MEASURE_TREE, PS_RECYCLE,
                        PS_STEAM, PS_STEAM_ORPHAN_SIZE, PS_TREE_WALK,''',
'''# PS_MEASURE_TREE/PS_MEASURE_GLOB/PS_DIR_KIND/PS_STEAM_ORPHAN_SIZE 的
# 测量职责已由进程内 os.scandir 实现（P2-4），脚本本体保留：
# selftest 阶段六仍直接引用 app.PS_STEAM_ORPHAN_SIZE 钉 run_ps 数组契约。
from ps_scripts import (PS_BIG_FILES, PS_DIR_KIND, PS_LOCK_HOLDERS,
                        PS_MEASURE_GLOB, PS_MEASURE_TREE, PS_RECYCLE,
                        PS_STEAM, PS_STEAM_ORPHAN_SIZE, PS_TREE_WALK,''',
    "import comment")

open('app.py', 'w', encoding='utf-8', newline='').write(src)
print("P2-4 patch done")
