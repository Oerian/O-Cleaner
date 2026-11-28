# -*- coding: utf-8 -*-
"""分析中心域（v2.17 架构减负 Task 1，从 core.py 迁出）。

七个分析子域：
  * 目录快照与暴涨对比（_save_scan_snapshot / snapshot_diff）与
    跨层级实时搜索（fs_search）
  * 大文件排行（big_files：内置 MFT / WizTree / PowerShell 三路择优）
  * 重复大文件检测（dupfiles_scan：同体积 -> 采样哈希 -> 全量 SHA-256 三级分桶）
  * 毒瘤软件两段式清除（bloatware_scan / bloatware_sweep）
  * Junction 无损搬家（junction_move / junction_restore）
  * 应用专清（appclean_scan / appclean_clean，签名库 cache/data/core 分级）
  * 下载目录整理与归档（downloads_advice / downloads_archive / downloads_archive_undo）

本模块是被 core.py「再绑定」的迁出域：core 顶部
`from ocleaner.analysis import ...` 保持全部既有调用点的裸名兼容；
selftest / unit_test 的 monkeypatch 落在 core 命名空间，本模块对
core 侧符号（识别 / 护栏 / 删除 / 引擎 / 测量 / 进度）以及依赖
CONFIG_DIR / RUNTIME_DIR 的路径常量（--data-dir 重定向时随 core 变化）
一律经 `_core.` 惰性引用 —— monkeypatch 与数据目录重定向对本模块
调用点均可见。
"""
from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import ocleaner.core as _core

# ======================================================================
#  目录快照与暴涨对比 / 跨层级实时搜索
# ======================================================================

def _save_scan_snapshot(letter: str) -> None:
    """全盘扫描落盘一份轻量目录快照（顶层+≥200MB 前 150 个目录，几 KB）。

    供「近期暴涨」对比（两次扫描间谁在偷偷吃空间）。每盘每天一份，
    保留最近 30 天。同时持久化扩展名分布（top 30），让「类型分布」
    页签在服务重启后仍有数据可看。
    """
    cached = _core._WIZ_DIRS.get(letter)
    if not cached or not cached.get("table"):
        return
    dirs = cached["table"].get("dirs") or {}
    items = sorted(((k, v) for k, v in dirs.items()
                    if v >= 200 * 1024 * 1024), key=lambda x: -x[1])[:150]
    try:
        _core.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        file_items = []
        for _parent, fitems in (cached["table"].get("files") or {}).items():
            for name0, sz0, full0 in fitems:
                if sz0 >= 10 * 1024 * 1024:
                    file_items.append({"path": full0, "size": sz0})
        file_items.sort(key=lambda x: -x["size"])
        # 截断线：第 300 名的体积。对比时，体积低于基线截断线的文件无法
        # 确认「当时是否存在」（可能只是跌出基线清单），不算新增——否则
        # Steam 等目录会因其他文件消长而在清单间进出，把存量误判成新增
        # 。
        files_cutoff = file_items[299]["size"] if len(file_items) >= 300 else 0
        _core._atomic_write(_core.SNAPSHOTS_DIR / f"{letter}-{today}.json",
                      {"drive": letter, "date": today,
                       "dirs": [{"path": k, "size": v} for k, v in items],
                       "files": file_items[:300],
                       "files_cutoff": files_cutoff})
        try:
            # table["ext"] 的值是 [字节数, 个数] 列表（_ext_bump 维护）
            ext_items = []
            for k, v in (cached["table"].get("ext") or {}).items():
                size = v[0] if isinstance(v, (list, tuple)) else v
                if size:
                    ext_items.append((k, size))
            ext_top = sorted(ext_items, key=lambda x: -x[1])[:30]
            _core._atomic_write(_core.SNAPSHOTS_DIR / f"{letter}-ext.json",
                          {"drive": letter, "updated": today,
                           "by_ext": [{"ext": k, "size": v}
                                      for k, v in ext_top]})
        except Exception:  # noqa: BLE001 —— 扩展名缓存失败绝不拖垮扫描
            traceback.print_exc()
        for f in _core.SNAPSHOTS_DIR.glob(f"{letter}-*.json"):
            try:
                d = datetime.strptime(f.stem.split("-", 1)[1], "%Y-%m-%d")
                if (datetime.now() - d).days > 30:
                    f.unlink()
            except (ValueError, OSError):
                pass
    except OSError:
        pass


def snapshot_diff(letter: str, days: int = 7) -> dict:
    """对比最新快照与 ~days 天前快照，按增量体积降序返回目录涨跌。"""
    letter = (letter or "C").rstrip(":").upper()
    snaps = {}
    for f in _core.SNAPSHOTS_DIR.glob(f"{letter}-*.json"):
        try:
            d = datetime.strptime(f.stem.split("-", 1)[1], "%Y-%m-%d")
            snaps[d.date()] = _core._load_json(f, {})
        except (ValueError, OSError):
            continue
    if not snaps:
        return {"error": "还没有历史快照（先做一次全盘扫描，明天再来对比）",
                "diff": []}
    dates = sorted(snaps, reverse=True)
    if len(dates) < 2:
        return {"error": "只有一份快照（今天），还没法对比；明天再来看",
                "diff": []}
    latest = snaps[dates[0]]
    target = (dates[0] - timedelta(days=max(1, days)))
    past_date = min(dates[1:], key=lambda d: abs((d - target).days))
    past = snaps[past_date]
    now_map = {d["path"]: d["size"] for d in latest.get("dirs", [])}
    then_map = {d["path"]: d["size"] for d in past.get("dirs", [])}
    cur_files = {f0["path"]: f0["size"] for f0 in latest.get("files", [])}
    past_files = {f0["path"]: f0["size"] for f0 in past.get("files", [])}
    files_available = bool(latest.get("files")) and bool(past.get("files"))
    past_cutoff = past.get("files_cutoff") or 0
    new_files = ([{"path": p0, "size": s0}
                  for p0, s0 in cur_files.items()
                  if p0 not in past_files and s0 >= past_cutoff]
                 if files_available else [])
    new_files.sort(key=lambda x: -x["size"])
    # 高频回涨（v2.15 方向二补完）：沿时间线统计每个目录「较上次增长」
    # 的次数，≥3 次标记 hot —— 清理后反复回涨的缓存目录值得进定时清理。
    timeline = [snaps[d] for d in sorted(snaps)]
    growth: dict[str, int] = {}
    for prev, cur in zip(timeline, timeline[1:]):
        prev_m = {d["path"]: d["size"] for d in prev.get("dirs", [])}
        cur_m = {d["path"]: d["size"] for d in cur.get("dirs", [])}
        for path0, size0 in cur_m.items():
            prev0 = prev_m.get(path0)
            if prev0 is not None and size0 > prev0:
                growth[path0] = growth.get(path0, 0) + 1
    diff = []
    for path, size in now_map.items():
        old = then_map.get(path)
        if old is None:
            diff.append({"path": path, "delta": size, "now": size,
                         "then": None, "new": True})
        else:
            d0 = size - old
            if d0:
                diff.append({"path": path, "delta": d0, "now": size,
                             "then": old, "new": False})
    for path, size in then_map.items():
        if path not in now_map:
            diff.append({"path": path, "delta": -size, "now": None,
                         "then": size, "new": False})
    diff.sort(key=lambda x: -x["delta"])
    nf_show = new_files[:50]
    return {"ok": True, "latest_date": str(dates[0]),
            "past_date": str(past_date),
            "files_available": files_available,
            "new_files": [{"path": x0["path"], "size": x0["size"],
                           "size_h": _core.human(x0["size"])} for x0 in nf_show],
            "new_files_total": len(new_files),
            "new_files_size": _core.human(sum(x0["size"] for x0 in new_files)),
            "diff": [{"path": x["path"], "delta": x["delta"],
                      "delta_h": _core.human(abs(x["delta"]))
                      + (" ↑" if x["delta"] > 0 else " ↓"),
                      "now": x["now"] or 0, "then": x["then"] or 0,
                      "now_h": _core.human(x["now"]) if x["now"] else "—",
                      "then_h": _core.human(x["then"]) if x["then"] else "—",
                      "new": x["new"],
                      "hot": growth.get(x["path"], 0) >= 3}
                     for x in diff[:50]]}


def fs_search(letter: str, q: str, limit: int = 80) -> dict:
    """占用排行跨层级实时搜索：在扫描缓存里按名字/路径模糊匹配目录与文件。"""
    letter = (letter or "C").rstrip(":").upper()
    cached = _core._WIZ_DIRS.get(letter)
    if not cached or not cached.get("table"):
        return {"error": f"请先对 {letter}: 盘做一次全盘扫描", "matches": []}
    q0 = (q or "").strip().lower()
    if not q0:
        return {"ok": True, "matches": []}
    out = []
    for path, size in (cached["table"].get("dirs") or {}).items():
        if q0 in os.path.basename(path).lower() or q0 in path.lower():
            out.append({"path": path, "size": size, "kind": "dir"})
            if len(out) >= limit:
                break
    if len(out) < limit:
        for parent, items in (cached["table"].get("files") or {}).items():
            for name, sz, full in items:
                if q0 in name.lower():
                    out.append({"path": full, "size": sz, "kind": "file"})
                    if len(out) >= limit:
                        break
            if len(out) >= limit:
                break
    out.sort(key=lambda x: -x.get("size", 0))
    for o in out:
        o["size_h"] = _core.human(o["size"])
    return {"ok": True, "matches": out[:limit]}


# ======================================================================
#  大文件排行 / 重复大文件检测
# ======================================================================

# ---- 大文件排行 -------------------------------------------------------
# 目录树沿「最粗的分支」逐层展开，天然偏向目录聚合；视频、镜像、安装包
# 这类真正的大头往往就是单个文件。这里用一次专门的全盘遍历把它们直接列出。
BIG_FILES_FLOOR_PS = 10 * 1024 * 1024     # PowerShell 兜底路径的体积地板
BIG_FILES_FLOOR_WIZ = 512 * 1024          # WizTree 明细全量，地板可以更低
BIG_FILES_LIMIT = 30
_BIGFILES_LOCK = threading.Lock()


def _wiz_mtime_norm(s: str) -> str:
    """WizTree CSV 修改时间 → 'YYYY-MM-DD HH:MM'；认不出返回空串。

    实测格式 '2026/11/01 01:58:55'（第 4 列，不带引号）；分隔符可能随
    系统区域变化（'-' 或 '/'），按可容错的方式手动切。
    """
    try:
        date, tm = s.strip().split(" ", 1)
        y, m, d = date.replace("-", "/").split("/")
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d} {tm[:5]}"
    except (ValueError, AttributeError):
        return ""


def _wiztree_big_files(csv_path: str, floor: int) -> list:
    """从 WizTree 导出的 CSV 里抽取文件行（目录行以 \\ 结尾，直接跳过）。

    只保留达到地板的行——C 盘 77 万行里超过 512 KB 的通常只有几千行，
    全量留内存没有压力。切分方式与 _wiztree_parse 相同：路径必被双引号
    包住，按「第一个引号闭合」切，避免路径内逗号把行切坏。
    返回 (路径, 体积, 修改时间) 三元组 —— 修改时间取第 4 列（归一化为
    'YYYY-MM-DD HH:MM'），供前端「只看近期新增的大文件」过滤。
    """
    rows: list = []
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        fh.readline()                  # WizTree 版本与捐赠提示
        fh.readline()                  # 列名
        for line in fh:
            if not line.startswith('"'):
                continue
            end = line.find('"', 1)
            if end <= 1:
                continue
            path = line[1:end]
            if path.endswith("\\"):    # 目录行
                continue
            parts = line[end + 1:].lstrip(",").split(",")
            try:
                size = int(parts[0].strip().strip('"'))
            except (ValueError, IndexError):
                continue
            if size < floor:
                continue
            rows.append((path, size,
                         _wiz_mtime_norm(parts[2]) if len(parts) > 2 else ""))
    return rows






def big_files(drive: str, limit: int = BIG_FILES_LIMIT) -> dict:
    """全盘最大的单个文件排行。只读，不删任何东西。

    三条路径：本进程是管理员且目标盘 NTFS 时走自研 MFT 引擎（2026-11-01
    起升为首选 —— 秒级完成、顺带带回修改时间、无外部依赖无临时文件）；
    非管理员或 MFT 不可用时装了 WizTree 就借它的导出（同为秒级）；
    都没有才 PowerShell 全盘遍历（与目录树的兜底路径同量级耗时）。同一个
    盘同时只跑一份，重复请求如实报「进行中」而不是静默叠扫。
    """
    letter = (drive or "C").rstrip(":").upper()
    root = letter + ":\\"
    if not _core.IS_WINDOWS or not os.path.exists(root):
        return {"error": f"盘符 {letter}: 不存在", "files": []}
    if not _BIGFILES_LOCK.acquire(blocking=False):
        return {"error": "已有大文件扫描在进行，请稍候", "files": []}
    try:
        rows: list = []
        engine = "powershell"
        # 引擎顺序（2026-11-03 定稿）：缺省 WizTree 在先（与概览扫描同一份
        # 速度缓存与用户偏好）；auto 模式下实测 MFT 明显更快才让 MFT 先。
        mft_ok = os.name == "nt" and _core.is_admin() and _core._mft_volume_ntfs(root)
        wiz_ok = _core._wiz_ok()
        pref = _core._eng_pref()
        if pref == "wiztree":
            try_wiz_first = wiz_ok
        elif pref == "mft":
            try_wiz_first = False
        else:
            try_wiz_first = wiz_ok and (not mft_ok or not _core._eng_prefer_mft(letter))

        def _wiz_big_run() -> bool:
            """WizTree 路径：导出 → 解析大文件行 → 即删 CSV。"""
            nonlocal rows, engine
            _core.prog_set("wiz", f"正在用 WizTree 读取 {letter}: 盘的 MFT 明细", 10,
                     indeterminate=True)
            csv_path, _werr = _core._wiztree_export(letter)
            if not csv_path:
                return False
            engine = "wiztree"
            try:
                rows = _core._wiztree_big_files(csv_path, BIG_FILES_FLOOR_WIZ)
            finally:
                _core._wiztree_cleanup(csv_path)
            return True

        if try_wiz_first:
            if not _wiz_big_run() and mft_ok:
                # 自研 MFT 回落路径：文件地板降到与 PS 路径一致（10 MB），
                # 内存增量可接受（万级条目 × ~100 B）；with_mtime 顺带带回
                # 修改时间供前端「只看近期」。任何失败都静默回落下一引擎。
                try:
                    _core.prog_set("mft", f"内置 MFT 引擎正在读取 {letter}: 盘的文件明细",
                             10, indeterminate=True)
                    tbl, info = _core._mft_build_table(letter, root,
                                                 file_floor_mb=10, with_mtime=True)
                    if tbl is not None:
                        engine = "mft"
                        rows = _core._mft_big_files(tbl, BIG_FILES_FLOOR_PS)
                except Exception:
                    traceback.print_exc()
                    rows, engine = [], "powershell"
        elif mft_ok:
            # MFT 首选路径：文件地板降到与 PS 路径一致（10 MB），
            # 内存增量可接受（万级条目 × ~100 B）；with_mtime 顺带带回
            # 修改时间供前端「只看近期」。任何失败都静默回落下一引擎。
            try:
                _core.prog_set("mft", f"内置 MFT 引擎正在读取 {letter}: 盘的文件明细",
                         10, indeterminate=True)
                tbl, info = _core._mft_build_table(letter, root,
                                             file_floor_mb=10, with_mtime=True)
                if tbl is not None:
                    engine = "mft"
                    rows = _core._mft_big_files(tbl, BIG_FILES_FLOOR_PS)
            except Exception:
                traceback.print_exc()
                rows, engine = [], "powershell"
            if engine != "mft" and wiz_ok:
                _wiz_big_run()
        if engine == "powershell":
            prog_file = os.path.join(
                tempfile.gettempdir(), f"oc-bigfiles-{os.getpid()}-{letter}.txt")
            if os.path.exists(prog_file):
                try:
                    os.remove(prog_file)
                except OSError:
                    pass
            script = (_core.PS_BIG_FILES
                      .replace("__ROOT__", _core._ps_sq(root))
                      .replace("__FLOOR__", str(BIG_FILES_FLOOR_PS))
                      .replace("__PROGFILE__", _core._ps_sq(prog_file)))
            _core.prog_set("walk", "正在全盘遍历大文件（未装 WizTree，耗时更长）",
                     10, indeterminate=True)
            stop = threading.Event()
            watcher = threading.Thread(target=_watch_bigfiles,
                                       args=(prog_file, stop), daemon=True)
            if prog_file:
                watcher.start()
            try:
                r = _core.run_ps(script, timeout=1800)
            finally:
                stop.set()
                if prog_file:
                    try:
                        os.remove(prog_file)
                    except OSError:
                        pass
            if isinstance(r, dict) and "error" in r:
                return {"error": r["error"], "files": []}
            items = r if isinstance(r, list) else ([] if not r else [r])
            rows = [(str(x.get("p") or ""), int(x.get("s") or 0),
                     str(x.get("m") or ""))
                    for x in items if isinstance(x, dict)]
        rows.sort(key=lambda x: -x[1])
        files = []
        for path, size, mtime in rows[:max(1, min(int(limit or BIG_FILES_LIMIT),
                                                  100))]:
            name = os.path.basename(path)
            info = _core.identify(name, path, "file")
            info.pop("match_score", None)
            files.append({"path": path, "name": name,
                          "dir": os.path.dirname(path), "size": size,
                          "mtime": mtime,
                          "title": info.get("title") or "",
                          "cleanable": bool(info.get("cleanable")),
                          "what": info.get("what") or "",
                          "tip": info.get("tip") or ""})
        return {"ok": True, "engine": engine, "files": files}
    finally:
        _BIGFILES_LOCK.release()


def _watch_bigfiles(prog_file: str, stop: threading.Event) -> None:
    """PS 大文件遍历的进度上报：地板之上每多 200 个文件写一次计数。

    总数事先未知，进度只能做成「已发现 N 个」的指示而不是百分比。
    """
    while not stop.wait(0.5):
        try:
            with open(prog_file, "r", encoding="utf-8", errors="replace") as fh:
                n = int(fh.read().strip() or 0)
            _core.prog_set("walk", f"已发现 {n} 个超过门槛的大文件…", 40,
                     indeterminate=True)
        except Exception:
            pass


# ---- 重复大文件检测（#34）------------------------------------------------
# 数据源是全盘扫描留下的 WizTree/MFT 明细缓存（_core._WIZ_DIRS），不重新遍历磁盘：
# 大体积文件的清单已经有了，这里只做「同体积分组 + 采样哈希证实」两步。
# 哈希只读每个文件的首尾各 1MB —— 1GB 文件也是 2MB 的 IO，同组文件通常
# 个位数，整体秒级返回。只有哈希一致的组才上报（同体积但内容不同的不算，
# 宁可少报也不误导删除）。
DUP_FLOOR_MB = 100
DUP_MAX_GROUPS = 30
DUP_MAX_PER_GROUP = 50


def _sample_hash(path: str, chunk: int = 1 << 20) -> str | None:
    """首尾各 1MB 采样哈希；读不到（权限/占用）返回 None。"""
    try:
        sz = os.path.getsize(path)
        if sz <= chunk * 2:
            with open(path, "rb") as f:
                data = f.read()
        else:
            with open(path, "rb") as f:
                head = f.read(chunk)
                f.seek(-chunk, os.SEEK_END)
                tail = f.read(chunk)
            data = head + tail
        return hashlib.md5(data).hexdigest()
    except OSError:
        return None



def _full_sha256(path: str) -> str | None:
    """流式全文件 SHA-256；读取失败返回 None（与 _sample_hash 语义一致）。

    采样哈希（首尾 1MB）只作展示预筛；真正决定「这组互为副本」的是这里
    的全量哈希 —— 固定大小镜像/稀疏文件可能首尾相同而中间不同。
    """
    try:
        hh = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                hh.update(chunk)
        return hh.hexdigest()
    except OSError:
        return None


# ======================================================================
#  毒瘤软件两段式清除 / Junction 无损搬家
# ======================================================================

def _load_bloatware() -> dict:
    """毒瘤软件签名库：内置 config/bloatware-builtin.json + 用户 config/bloatware.json 合并。"""
    raw = _core._load_json(_core.BLOATWARE_BUILTIN_PATH, {})
    apps: list[dict] = list(raw.get("apps") or []) if isinstance(raw, dict) else []
    seen = {str(a.get("id") or "") for a in apps if isinstance(a, dict)}
    try:
        user = _core._load_json(_core.BLOATWARE_CFG_PATH, {})
        if isinstance(user, dict) and isinstance(user.get("apps"), list):
            for a in user["apps"]:
                if isinstance(a, dict) and a.get("id"):
                    if str(a["id"]) not in seen:
                        apps.append(a)
                        seen.add(str(a["id"]))
    except Exception:
        pass
    return {"apps": apps}


_UNINSTALL_REG_PREFIXES = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
)


def _parse_uninstall_reg_key(reg_key: str) -> tuple[str, str] | None:
    """解析并校验卸载注册表键，仅允许 HKLM/HKCU 下的 Uninstall 子树。
    返回 (hive_prefix, sub_path)，非法返回 None。"""
    raw = str(reg_key or "").strip().strip("\\")
    if not raw:
        return None
    parts = raw.split("\\")
    if ".." in parts:
        return None
    if parts[0].upper() in ("HKLM", "HKCU"):
        hive_prefix = parts[0].upper()
        sub_path = "\\".join(parts[1:])
    else:
        hive_prefix = "HKLM"
        sub_path = raw
    low_sub = sub_path.lower()
    ok = any(low_sub.startswith(p.lower() + "\\")
             for p in _UNINSTALL_REG_PREFIXES)
    if not ok:
        return None
    return hive_prefix, sub_path


def bloatware_scan() -> dict:
    """扫描已安装的「毒瘤软件」：注册表卸载项签名匹配 + 已知目录探测。

    只读探测，不做任何删除。返回每项的官方卸载命令（UninstallString /
    QuietUninstallString）与已知残留目录（按存在与否过滤），供两段式
    清除：先跑官方卸载程序，再「清扫残留」。
    """
    apps = _load_bloatware().get("apps") or []
    found = []
    if _core.IS_WINDOWS:
        import winreg
        hives = [
            ("HKLM", winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            ("HKLM", winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            ("HKCU", winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        seen_keys = set()
        for hprefix, hive, sub in hives:
            try:
                with winreg.OpenKey(hive, sub) as root_k:
                    i = 0
                    sub_names = []
                    while True:
                        try:
                            sub_names.append(winreg.EnumKey(root_k, i))
                            i += 1
                        except OSError:
                            break
            except OSError:
                continue
            for sub_key in sub_names:
                full = f"{sub}\\{sub_key}"
                full_id = f"{hprefix}\\{full}"
                if full_id.lower() in seen_keys:
                    continue
                seen_keys.add(full_id.lower())
                try:
                    with winreg.OpenKey(hive, full) as k:
                        try:
                            disp = str(winreg.QueryValueEx(k, "DisplayName")[0])
                        except OSError:
                            continue
                        for app in apps:
                            if any(m.lower() in disp.lower()
                                   for m in app.get("match", [])):
                                try:
                                    ustr = str(winreg.QueryValueEx(
                                        k, "UninstallString")[0])
                                except OSError:
                                    ustr = ""
                                try:
                                    qstr = str(winreg.QueryValueEx(
                                        k, "QuietUninstallString")[0])
                                except OSError:
                                    qstr = ""
                                try:
                                    loc = str(winreg.QueryValueEx(
                                        k, "InstallLocation")[0])
                                except OSError:
                                    loc = ""
                                found.append({
                                    "id": app["id"], "name": app["name"],
                                    "display": disp, "reg_key": full_id,
                                    "uninstall": ustr, "quiet": qstr,
                                    "install_location": loc,
                                    "dirs": app.get("dirs", []),
                                })
                                break
                except OSError:
                    continue
    # 已知残留目录探测（扫描全部真实存在的盘符，不再误探 A:/B: 或漏掉数据盘）
    drive_roots = [f"{d['letter']}:\\" for d in _core.list_drives()] or ["C:\\"]
    prog_variants = ["Program Files", "Program Files (x86)"]
    for app in apps:
        hits = []
        for d in app.get("dirs", []):
            for root in drive_roots:
                for pf in prog_variants:
                    cand = os.path.join(root, pf, d)
                    if os.path.isdir(cand):
                        hits.append(cand)
        if hits:
            hit = next((f for f in found if f.get("id") == app["id"]), None)
            if hit:
                hit["residue_dirs"] = [h for h in hits
                                       if h not in hit.get("residue_dirs", [])]
            else:
                found.append({"id": app["id"], "name": app["name"],
                              "display": app["name"], "reg_key": "",
                              "uninstall": "", "quiet": "",
                              "install_location": "",
                              "residue_dirs": hits})
    return {"ok": True, "found": found}


def bloatware_sweep(reg_key: str, dirs: list) -> dict:
    """清扫残留：已知目录走护栏链+回收站，注册表键精准删除。

    两段式清除的第二段 —— 官方卸载程序跑完之后调用。目录经完整
    _core.check_deletable(allow_user_data=True) 护栏链（系统目录/自保护/Junction
    仍硬拒绝）并移入回收站（可恢复）；注册表只删白名单内的 Uninstall 子键，
    绝不模糊匹配或越界删除。
    """
    results = []
    for d in dirs or []:
        norm = os.path.normpath(d)
        try:
            if not os.path.isdir(norm):
                results.append({"path": norm, "ok": True, "note": "已不存在"})
            elif _core._precheck_locked_files(norm):
                results.append({"path": norm, "ok": False,
                                "error": "存在被占用的文件（先跑官方卸载程序）"})
            else:
                ok, why = _core.check_deletable(norm, "tree", allow_user_data=True)
                if not ok:
                    results.append({"path": norm, "ok": False, "error": why})
                else:
                    ok2, why2 = _core.rm_trash(norm)
                    results.append({"path": norm, "ok": bool(ok2),
                                    "trashed": bool(ok2),
                                    "error": None if ok2 else (why2 or "移入回收站失败")})
        except Exception as exc:  # noqa: BLE001
            results.append({"path": norm, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    reg_done = 0
    reg_error = ""
    if reg_key and _core.IS_WINDOWS:
        parsed = _parse_uninstall_reg_key(reg_key)
        if not parsed:
            reg_error = "reg_key 不在 Uninstall 白名单子树内，已拒绝删除"
        else:
            import winreg
            hprefix, sub_path = parsed
            hive = (winreg.HKEY_LOCAL_MACHINE if hprefix == "HKLM"
                    else winreg.HKEY_CURRENT_USER)
            parent_sub, _, leaf_key = sub_path.rpartition("\\")
            def _reg_delete_tree(root_h, sub: str) -> None:
                with winreg.OpenKey(root_h, sub, 0,
                                    winreg.KEY_ENUMERATE_SUB_KEYS | winreg.DELETE) as cur_k:
                    while True:
                        try:
                            child = winreg.EnumKey(cur_k, 0)
                        except OSError:
                            break
                        _reg_delete_tree(root_h, sub + "\\" + child)
                winreg.DeleteKey(root_h, sub)
            try:
                _reg_delete_tree(hive, sub_path)
                reg_done += 1
            except OSError as exc:
                reg_error = f"注册表键删除失败（可能需要管理员权限）：{exc}"
    all_ok = all(r.get("ok") for r in results) and not reg_error
    return {"ok": all_ok, "results": results, "reg_deleted": reg_done,
            "reg_error": reg_error}


def junction_move(src: str, dst: str, merge: bool = False,
                  rename_conflict: bool = True) -> dict:
    """C 盘无损搬家：目录整体迁移到目标盘并原位建 NTFS Junction 透明映射。

    安全序列（每步可回退）：
    1. 占用预检（被锁文件直接 409 级失败）；
    2. 整树拷贝到目标位置（dst 已存在且 merge=True 时逐文件合并，
       同名冲突自动重命名 name(1).ext）；
    3. 双向校验（文件数 + 总字节数 100% 一致才继续）；
    4. 原目录改名暂存为 src.oc-mv-backup（失败可改回）；
    5. 原位置创建 Junction 指向新位置；
    6. 校验通过后经 _core.rm_tree_strict 删除暂存（自带断电恢复日志）；
    7. 记录 config/moved-junctions.json，支持 /__oc__/junction-restore 一键迁回。
    """
    src = os.path.normpath(os.path.expandvars(src))
    dst = os.path.normpath(os.path.expandvars(dst))
    if not os.path.isabs(src) or not os.path.isdir(src):
        return {"error": "源目录无效"}
    if os.path.islink(src) or _core._is_reparse_dir(src):
        return {"error": "源目录本身已经是 Junction 或符号链接，无需再次搬家"}
    if not os.path.isabs(dst) or not os.path.splitdrive(dst)[0]:
        return {"error": "目标路径无效"}
    if os.path.normcase(src) == os.path.normcase(dst):
        return {"error": "源与目标相同"}
    if os.path.splitdrive(src)[0].upper() == os.path.splitdrive(dst)[0].upper():
        return {"error": "目标必须在不同盘符（同盘直接移动即可，无需搬家）"}
    if os.path.exists(dst) and not merge:
        return {"error": "目标已存在（勾选「合并」可并入现有目录）"}
    # 护栏链前置（三轮审计补强）：受保护目录/个人数据域/盘根等一律拒绝
    # ——搬家虽不删数据，但给 C:\Windows 建映射等于破坏系统。
    ok, why = _core.check_deletable(src)
    if not ok:
        return {"error": why or "护栏拦截"}
    for _r, _ds, _fs in os.walk(src):
        for _d in _ds:
            if _core._is_reparse_dir(os.path.join(_r, _d)):
                return {"error": f"源目录内包含嵌套 Junction/符号链接（{_d}），为防丢失链接结构已中止搬家"}
    locked = _core._precheck_locked_files(src)
    if locked:
        return {"error": "源目录存在被占用的文件，搬家已中止",
                "locked": locked[:10]}
    src_est_bytes = 0
    for _r, _ds, _fs in os.walk(src):
        for _f in _fs:
            try:
                src_est_bytes += os.path.getsize(os.path.join(_r, _f))
            except OSError:
                pass
    try:
        dst_drive = os.path.splitdrive(dst)[0] + "\\"
        if shutil.disk_usage(dst_drive).free < src_est_bytes + 16 * 1024 * 1024:
            return {"error": f"目标盘 {dst_drive} 可用空间不足"}
    except OSError:
        pass
    # 1) 拷贝（支持合并 + 冲突自动重命名）
    file_count = [0]
    byte_count = [0]
    dst_existed = os.path.exists(dst)
    try:
        Path(dst).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"error": f"无法在目标位置创建目录（权限不足或路径非法）：{exc}"}
    try:
        for root, dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target_root = os.path.normpath(
                os.path.join(dst, rel)) if rel != "." else Path(dst)
            os.makedirs(target_root, exist_ok=True)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(target_root, f)
                if os.path.exists(d):
                    if not rename_conflict:
                        return {"error": f"目标已存在同名文件：{d}"}
                    stem, ext = os.path.splitext(f)
                    k = 1
                    while os.path.exists(d):
                        d = os.path.join(target_root, f"{stem}({k}){ext}")
                        k += 1
                shutil.copy2(s, d)
                file_count[0] += 1
                byte_count[0] += os.path.getsize(d)
    except OSError as exc:
        if not dst_existed:
            _core.rm(dst)
        return {"error": f"拷贝到目标盘失败（原目录未动）：{exc}"}
    # 2) 双向校验
    src_count = src_bytes = 0
    for root, _dirs, files in os.walk(src):
        src_count += len(files)
        for f in files:
            try:
                src_bytes += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    if src_count != file_count[0] or src_bytes != byte_count[0]:
        if not dst_existed:
            _core.rm(dst)
        return {"error": f"拷贝校验不一致（源 {src_count} 文件/{src_bytes} 字节，"
                         f"目标 {file_count[0]} 文件/{byte_count[0]} 字节），"
                         "原目录未动，请检查目标盘空间"}
    # 3) 原目录改名暂存 → 建 Junction → 校验
    backup = src.rstrip("\\/") + ".oc-mv-backup"
    if os.path.exists(backup):
        return {"error": f"暂存目录 {backup} 已存在，请先处理上次残留"}
    try:
        os.rename(src, backup)
    except OSError as exc:
        return {"error": f"源目录被占用或无法改名，原目录未动：{exc}"}
    try:
        import _winapi
        _winapi.CreateJunction(str(dst), str(src))
        if not os.path.isdir(src):
            raise RuntimeError("Junction 建立后无法访问")
        probe = os.listdir(src)
        if not isinstance(probe, list):
            raise RuntimeError("Junction 内容异常")
    except Exception as exc:  # noqa: BLE001
        restored_ok = False
        try:
            if os.path.lexists(src) and (_core._is_reparse_dir(src) or os.path.islink(src)):
                os.rmdir(src)
            if not os.path.lexists(src) and os.path.exists(backup):
                os.rename(backup, src)
                restored_ok = True
        except OSError:
            pass
        if restored_ok:
            return {"error": f"Junction 建立失败，已还原原目录：{exc}"}
        return {"error": f"Junction 建立失败且未能自动还原，完整原数据位于 {backup}：{exc}"}
    # 4) Junction 验证通过后立即落盘搬家记录（先于清备份，避免部分残留时丢失记录）
    try:
        with _core._lock:
            recs = _core._load_json(_core.MOVED_JUNCTIONS_PATH, [], use_cache=False)
            if not isinstance(recs, list):
                recs = []
            recs.insert(0, {"src": src, "dst": str(dst), "merged": bool(merge),
                            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
            _core._atomic_write(_core.MOVED_JUNCTIONS_PATH, recs[:50])
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    # 5) 删除暂存（走 _core.rm_tree_strict：断电可恢复）
    full, _partial, note = _core.rm_tree_strict(backup)
    if not full:
        return {"ok": True, "note": f"搬家完成，但暂存目录删除不彻底（{note}）",
                "src": src, "dst": str(dst)}
    return {"ok": True, "src": src, "dst": str(dst),
            "files": file_count[0], "bytes_h": _core.human(byte_count[0])}


def junction_restore(src: str) -> dict:
    """一键迁回：先安全拷贝到同盘暂存并校验，再摘 Junction 原子改名迁回。"""
    src = os.path.normpath(os.path.expandvars(src))
    with _core._lock:
        recs = _core._load_json(_core.MOVED_JUNCTIONS_PATH, [], use_cache=False)
    rec = next((r for r in (recs if isinstance(recs, list) else [])
                if os.path.normcase(str(r.get("src"))) == os.path.normcase(src)), None)
    if not rec:
        return {"error": "搬家记录里没有这个目录"}
    dst = str(rec.get("dst"))
    if not os.path.isdir(dst):
        return {"error": f"目标目录已不存在：{dst}"}
    if not os.path.islink(src) and not _core._is_reparse_dir(src):
        return {"error": "源位置不是 Junction（可能已迁回）"}
    try:
        if os.path.normcase(os.path.realpath(src)) != os.path.normcase(os.path.realpath(dst)):
            return {"error": "源 Junction 当前指向与记录不一致，出于安全中止迁回"}
    except OSError:
        return {"error": "无法验证源 Junction 指向"}
    locked = _core._precheck_locked_files(dst)
    if locked:
        return {"error": "目标目录内存在被占用的文件，请先关闭相关程序后再迁回",
                "locked": locked[:10]}
    dst_count = dst_bytes = 0
    for _r, _ds, _fs in os.walk(dst):
        dst_count += len(_fs)
        for _f in _fs:
            try:
                dst_bytes += os.path.getsize(os.path.join(_r, _f))
            except OSError:
                pass
    try:
        src_drive = os.path.splitdrive(src)[0] + "\\"
        if shutil.disk_usage(src_drive).free < dst_bytes + 16 * 1024 * 1024:
            return {"error": f"原盘 {src_drive} 可用空间不足，无法迁回"}
    except OSError:
        pass

    tmp_src = src.rstrip("\\/") + f".oc-restore-{os.getpid()}"
    if os.path.lexists(tmp_src):
        _core.rm(tmp_src)
    copied_cnt = copied_bytes = 0
    try:
        os.makedirs(tmp_src, exist_ok=True)
        for root, _dirs, files in os.walk(dst):
            rel = os.path.relpath(root, dst)
            troot = os.path.join(tmp_src, rel) if rel != "." else tmp_src
            os.makedirs(troot, exist_ok=True)
            for f in files:
                sp = os.path.join(root, f)
                dp = os.path.join(troot, f)
                shutil.copy2(sp, dp)
                copied_cnt += 1
                copied_bytes += os.path.getsize(dp)
        if copied_cnt != dst_count or copied_bytes != dst_bytes:
            raise OSError("拷贝校验不一致")
    except OSError as exc:
        _core.rm(tmp_src)
        return {"error": f"迁回拷贝阶段失败（原 Junction 与目标数据均完好）：{exc}"}

    try:
        os.rmdir(src)
    except OSError as exc:
        _core.rm(tmp_src)
        return {"error": f"Junction 摘除失败：{exc}"}
    try:
        os.rename(tmp_src, src)
    except OSError as exc:
        try:
            __import__("_winapi").CreateJunction(dst, src)
        except OSError:
            pass
        return {"error": f"原子迁回改名失败（暂存位于 {tmp_src}，目标数据仍在 {dst}）：{exc}"}

    if not rec.get("merged"):
        _core.rm_tree_strict(dst)
    with _core._lock:
        cur_recs = _core._load_json(_core.MOVED_JUNCTIONS_PATH, [], use_cache=False)
        if not isinstance(cur_recs, list):
            cur_recs = []
        _core._atomic_write(_core.MOVED_JUNCTIONS_PATH,
                      [r for r in cur_recs
                       if os.path.normcase(str(r.get("src"))) != os.path.normcase(src)])
    return {"ok": True, "src": src}


# ======================================================================
#  应用专清 / 下载目录整理建议
# ======================================================================

# ---- 应用专清（v2.15 方向三，v2.17 外置为 config/appclean.json）----
# kind 语义：cache=纯缓存可放心清；data=用户数据按需处理；core=核心不可动
def _load_appclean_apps() -> list[dict]:
    """从 config/appclean.json 读取应用专清签名库（热更新，无需重启）。"""
    raw = _core._load_json(_core.APPCLEAN_CFG_PATH, {})
    if isinstance(raw, dict) and isinstance(raw.get("apps"), list):
        return [a for a in raw["apps"] if isinstance(a, dict) and a.get("id")]
    return []


def appclean_scan(drive: str | None = None) -> dict:
    """应用专清扫描（v2.15 方向三）：对每个已知应用逐子项测体积，
    标注 cache/data/core 三类语义 —— 「哪些删了只清缓存、哪些是接收的
    文件、哪些绝对不能碰」一目了然。只读不删；清理走 /__oc__/appclean/clean。"""
    out = []
    drv = (drive or "").rstrip(":").upper()
    for app in _load_appclean_apps():
        base_dirs: list[str] = []
        seen_b: set[str] = set()
        for b in app.get("bases", []):
            cand = os.path.normpath(os.path.expandvars(b))
            matched = glob_dirs(cand) if "*" in cand else (
                [cand] if os.path.isdir(cand) else [])
            for m0 in matched:
                if drv and not m0.upper().startswith(f"{drv}:"):
                    continue
                k0 = os.path.normcase(m0)
                if k0 not in seen_b:
                    seen_b.add(k0)
                    base_dirs.append(m0)
        if not base_dirs:
            continue
        items = []
        seen_paths: set[str] = set()
        total = 0
        for base_found in base_dirs:
            for it in app.get("items", []):
                ipath = os.path.normpath(os.path.expandvars(
                    os.path.join(base_found, it["sub"])))
                if it["sub"] and "*" in it["sub"]:
                    hits = glob_dirs(ipath)
                else:
                    hits = [ipath] if os.path.isdir(ipath) or os.path.isfile(ipath) else []
                hits = [h for h in hits if os.path.normcase(h) not in seen_paths]
                if not hits:
                    continue
                # 递归测真实体积（_core._py_tree_size 走 os.scandir 进程内统计，
                # 避免 shallow=True 把多层缓存目录误算成 0 B）
                sz = sum((_core._py_tree_size(h) if os.path.isdir(h)
                          else (os.path.getsize(h) if os.path.isfile(h) else 0))
                         for h in hits)
                if sz == 0:
                    continue
                for h in hits:
                    seen_paths.add(os.path.normcase(h))
                total += sz
                items.append({"label": it["label"], "kind": it["kind"],
                              "path": hits[0], "paths": hits,
                              "size": sz, "size_h": _core.human(sz),
                              "advice": it.get("advice", "")})
        if items:
            out.append({"id": app["id"], "name": app["name"],
                        "base": base_dirs[0], "total": total,
                        "total_h": _core.human(total), "items": items})
    out.sort(key=lambda a: -a["total"])
    return {"ok": True, "apps": out}


def _appclean_cache_allowed(path: str) -> bool:
    """校验待清路径是否精确命中 config/appclean.json 中的 kind=='cache' 条目。"""
    norm = os.path.normcase(os.path.normpath(path))
    for app in _load_appclean_apps():
        for b in app.get("bases", []):
            cand = os.path.normpath(os.path.expandvars(b))
            bases = glob_dirs(cand) if "*" in cand else (
                [cand] if os.path.isdir(cand) else [])
            for base_found in bases:
                for it in app.get("items", []):
                    if it.get("kind") != "cache":
                        continue
                    ipath = os.path.normpath(os.path.expandvars(
                        os.path.join(base_found, it["sub"])))
                    hits = glob_dirs(ipath) if (it["sub"] and "*" in it["sub"]) else [ipath]
                    for h in hits:
                        if os.path.normcase(os.path.normpath(h)) == norm:
                            return True
    return False


def appclean_clean(paths: list[str]) -> dict:
    """应用专清安全清理：仅放行签名库中的 cache 项，经护栏校验后移入回收站。"""
    results = []
    trashed_bytes = 0
    for p0 in paths or []:
        norm = os.path.normpath(os.path.expandvars(str(p0).strip()))
        item = {"path": norm, "ok": False, "trashed": False, "error": None}
        if not os.path.isabs(norm) or not os.path.exists(norm):
            item["error"] = "路径不存在或无效"
        elif not _appclean_cache_allowed(norm):
            item["error"] = "拒绝：目标不在应用专清 cache 白名单内"
        else:
            ok, why = _core.check_deletable(norm, "tree", allow_user_data=True)
            if not ok:
                item["error"] = why or "护栏拦截"
            else:
                sz0 = _core._py_tree_size(norm) if os.path.isdir(norm) else _core._size_of(norm)
                ok2, why2 = _core.rm_trash(norm)
                item["ok"] = bool(ok2)
                item["trashed"] = bool(ok2)
                if ok2:
                    item["size"] = sz0
                    trashed_bytes += sz0
                else:
                    item["error"] = why2 or "移入回收站失败"
        results.append(item)
    done = sum(1 for r in results if r["ok"])
    _APPCLEAN_CACHE.clear()
    try:
        _core.append_history({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": "应用专清（移入回收站）",
            "freed": 0,
            "freed_h": "0 B",
            "results": [{"label": os.path.basename(r["path"]) or r["path"],
                         "path": r["path"], "freed": 0,
                         "trashed": 1 if r["trashed"] else 0,
                         "failed": 0 if r["ok"] else 1,
                         "error": r.get("error")} for r in results],
        })
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    first_err = next((r["error"] for r in results if r.get("error")), "")
    return {"ok": done == len(results) and bool(results), "done": done,
            "total": len(results), "trashed_bytes": trashed_bytes,
            "trashed_h": _core.human(trashed_bytes),
            "error": "" if (done == len(results) and results) else first_err,
            "results": results}


def glob_dirs(pattern: str) -> list:
    """通配符展开出的目录清单（如 QQ 多账号包目录）。"""
    import glob as _glob
    return [g for g in _glob.glob(pattern) if os.path.isdir(g)]




DUP_PROGRESS: dict = {"done": 0, "total": 0, "phase": "idle"}
_BIG_CACHE: dict = {}          # (盘符, limit) -> (时间戳, 结果)；随新扫描失效
_APPCLEAN_CACHE: dict = {}     # drive_key -> (时间戳, 结果)；TTL 10 分钟
_BLOAT_CACHE: dict = {"ts": 0.0, "r": None}      # TTL 10 分钟
_DUP_CACHE: dict = {}   # letter -> (时间戳, 结果)；会话内避免重复全量哈希


def dupfiles_scan(drive: str, floor_mb: int = DUP_FLOOR_MB) -> dict:
    # 会话内缓存（5 分钟）：全量哈希代价高，重复打开弹窗不应重算；
    # 重新全盘扫描会重建 _core._WIZ_DIRS，缓存随新扫描自动失效（时间戳对比）。
    letter0 = (drive or "C").rstrip(":").upper()
    cached = _core._WIZ_DIRS.get(letter0)
    hit = _DUP_CACHE.get(letter0)
    if (hit and cached and hit[0] >= cached.get("at", 0)
            and time.time() - hit[0] < 300
            and hit[2] == floor_mb):
        return hit[1]
    """在扫描缓存里找「体积相同且首尾采样一致」的大文件组。

    返回 groups（按可回收体积降序）：每组带 dup 文件数、可回收体积
    （= 文件大小 × 多余份数）与成员清单；成员里 dup=true 的文件互相重复。
    """
    letter = (drive or "C").rstrip(":").upper()
    cached = _core._WIZ_DIRS.get(letter)
    if not cached or not cached.get("table"):
        return {"error": f"请先对 {letter}: 盘做一次全盘扫描"
                         "（重复检测基于扫描明细缓存，不重复读盘）",
                "groups": []}
    floor = max(1, int(floor_mb)) * 1024 * 1024
    by_size: dict[int, list] = {}
    seen_inodes: set[tuple[int, int]] = set()
    for _parent, items in cached["table"]["files"].items():
        for name, sz, full in items:
            if sz >= floor:
                try:
                    st = os.lstat(full)
                    if os.path.islink(full) or (getattr(st, "st_file_attributes", 0) & 0x400):
                        continue
                    if st.st_ino:
                        ino_key = (st.st_dev, st.st_ino)
                        if ino_key in seen_inodes:
                            continue
                        seen_inodes.add(ino_key)
                except OSError:
                    continue
                by_size.setdefault(sz, []).append((full, name))
    cand = [(sz, fs) for sz, fs in by_size.items() if len(fs) >= 2]
    cand.sort(key=lambda x: -(x[0] * len(x[1])))
    cand = cand[:DUP_MAX_GROUPS]
    DUP_PROGRESS["done"] = 0
    DUP_PROGRESS["total"] = 0
    DUP_PROGRESS["phase"] = "hashing"

    groups = []
    # 两阶段进度（2026-11-10）：阶段一全部采样分桶（快，不计入进度），
    # 得出确定的全量哈希总数后阶段二逐文件核验 —— total 一次到位，
    # 不再出现 2/2 → 2/4 的递增式迷惑进度（设计约束）。
    work: list[tuple[int, list]] = []
    total0 = 0
    for sz, fs in cand:
        fs = fs[:DUP_MAX_PER_GROUP]
        # 三级分桶（三轮审计 P0 修复）：同体积 → 采样哈希预筛 → 全文件
        # SHA-256 二次证实。全量哈希防「首尾相同、中间不同」的固定大小
        # 镜像/稀疏文件误判；只对采样命中的候选做（用户手动触发）。
        hashes: dict[str, list[tuple[str, str]]] = {}
        for full, name in fs:
            h = _sample_hash(full)
            if h:
                hashes.setdefault(h, []).append((full, name))
        for pairs in hashes.values():
            if len(pairs) >= 2:
                work.append((sz, pairs))
                total0 += len(pairs)
    DUP_PROGRESS["total"] = total0
    DUP_PROGRESS["done"] = 0
    for sz, pairs in work:
        fulls: dict[str, list[tuple[str, str]]] = {}
        for full, name in pairs:
            DUP_PROGRESS["done"] += 1
            fh = _full_sha256(full)
            if fh:
                fulls.setdefault(fh, []).append((full, name))
        for fpairs in fulls.values():
            if len(fpairs) < 2:
                continue
            members = []
            for full, name in fpairs:
                try:
                    mt = os.path.getmtime(full)
                    mth = datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")
                except OSError:
                    mt, mth = 0.0, ""
                members.append({"path": full, "name": name, "dup": True,
                                "mtime": mt, "mtime_h": mth})
            n_dup = len(members)
            # 跨目录标注：副本分布在
            # 不同父目录＝大概率是程序/应用自带的多份拷贝（如 Telegram
            # 各实例目录的 nw.dll）——字节一致但功能上是依赖，不是冗余。
            # 前端据此打 ⚠ 并把跨目录组排除出一键全选。
            parents0 = {os.path.dirname(os.path.abspath(f0)).lower()
                        for f0, _n0 in fpairs}
            xdir = len(parents0) > 1
            groups.append({
                "size": sz, "size_h": _core.human(sz),
                "dup_count": n_dup,
                "wasted": sz * (n_dup - 1), "wasted_h": _core.human(sz * (n_dup - 1)),
                "files": members, "xdir": xdir,
            })
    groups.sort(key=lambda g: -g["wasted"])
    DUP_PROGRESS["phase"] = "done"
    result = {"ok": True, "drive": letter, "floor_mb": floor_mb,
              "groups": groups}
    _DUP_CACHE[letter] = (time.time(), result, floor_mb)
    return result


# ---- 下载目录整理建议（#35）----------------------------------------------
# 下载目录是「装完就忘」的重灾区：安装包、镜像、压缩包堆成山。这里按
# 知识库把顶层文件归类聚合，给出可执行的整理建议（不自动删 —— 建议卡
# 只负责说清楚「什么能删、什么该转移」，动作留给用户）。
def _downloads_dir() -> str:
    """真实下载目录：先查注册表（用户可能改过位置），回落 ~/Downloads。"""
    if _core.IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\User Shell Folders") as k:
                val, _t = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
            val = os.path.expandvars(str(val))
            if os.path.isdir(val):
                return val
        except OSError:
            pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


# 建议文案按分类给：只挑下载目录里真实常见、且建议明确的几类，
# 其余分类有数据但不强行给建议（宁缺毋滥）。
_DOWNLOAD_HINTS = {
    "安装包": "装完即可删 —— 重装随时能再下载",
    "镜像": "确认不再挂载/刻录后可删",
    "压缩包": "确认解压完整后可删，或转移到存储盘",
    "文档": "建议归档到文档目录",
    "视频": "大视频建议转移到存储盘",
}


def downloads_advice(limit_names: int = 3) -> dict:
    """扫描下载目录顶层，按知识库分类聚合，产出整理建议卡数据。"""
    root = _core._downloads_dir()
    if not os.path.isdir(root):
        return {"error": f"下载目录不存在：{root}", "path": root}
    agg: dict[str, dict] = {}
    total_files = 0
    total_size = 0
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        return {"error": f"无法读取下载目录：{e}", "path": root}
    for e in entries:
        try:
            if e.is_dir():
                # 子目录只统计体积（可能是解压产物，单独一组提示）
                sz = _dir_size(e.path, shallow=True)
                slot = agg.setdefault("子文件夹", {
                    "category": "子文件夹", "count": 0, "size": 0,
                    "cleanable": False, "names": []})
                slot["count"] += 1
                slot["size"] += sz
                # 2026-11-10：子文件夹也提供条目（勾选=归档/迁移整个文件夹）
                if len(slot.get("items") or []) < 50:
                    slot.setdefault("items", []).append(
                        {"name": e.name, "path": e.path, "size": sz,
                         "size_h": _core.human(sz), "is_dir": True})
                continue
            sz = e.stat().st_size
        except OSError:
            continue
        total_files += 1
        total_size += sz
        info = _core.identify(e.name, e.path, "file")
        cat = info.get("category") or "未识别"
        slot = agg.setdefault(cat, {
            "category": cat, "count": 0, "size": 0,
            "cleanable": bool(info.get("cleanable")), "names": []})
        slot["count"] += 1
        slot["size"] += sz
        if info.get("cleanable"):
            slot["cleanable"] = True
        if len(slot["names"]) < limit_names:
            slot["names"].append(e.name)
        if len(slot.get("items") or []) < 50:
            slot.setdefault("items", []).append(
                {"name": e.name, "path": e.path, "size": sz,
                 "size_h": _core.human(sz)})
    groups = sorted(agg.values(), key=lambda g: -g["size"])
    for g in groups:
        g["size_h"] = _core.human(g["size"])
        if g["category"] in _DOWNLOAD_HINTS:
            g["hint"] = _DOWNLOAD_HINTS[g["category"]]
    # 建议卡头部的一句话总结：最有行动价值的那一条
    headline = ""
    for g in groups:
        if g["category"] in _DOWNLOAD_HINTS and g["size"] > 0:
            headline = (f"{g['category']} {g['count']} 个共 {g['size_h']}，"
                        + _DOWNLOAD_HINTS[g["category"]])
            break
    return {"ok": True, "path": root,
            "total_files": total_files, "total_size": total_size,
            "total_size_h": _core.human(total_size),
            "headline": headline, "groups": groups}


def _dir_size(path: str, shallow: bool = True, budget: list | None = None) -> int:
    """目录体积；shallow=True 时只算一层（下载目录子目录够用了）。"""
    total = 0
    try:
        with os.scandir(path) as it:
            for e in it:
                try:
                    if e.is_file():
                        total += e.stat().st_size
                    elif not shallow and e.is_dir():
                        total += _dir_size(e.path, shallow=False)
                except OSError:
                    continue
    except OSError:
        pass
    return total


# ======================================================================
#  下载目录按类型归档（可整批撤销）
# ======================================================================

def downloads_archive(root: str | None = None,
                      only: list[str] | None = None) -> dict:
    """下载目录按类型归档（v2.15 方向五后半）：顶层文件移入分类子目录。

    分类名与知识库识别一致；同名冲突自动重命名；移动清单写入
    downloads-archive-log.json，支持 /__oc__/downloads-archive/undo 一键
    整批撤销。子目录与隐藏文件不参与归档。
    """
    root = root or _core._downloads_dir()
    if not os.path.isdir(root):
        return {"error": f"下载目录不存在：{root}"}
    moves = []
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        return {"error": f"无法读取下载目录：{e}"}
    only_set = {os.path.normpath(os.path.abspath(p0)).lower() for p0 in only} \
        if only else None
    for e in entries:
        try:
            if e.name.startswith("."):
                continue
            # 未显式指定 only 时只归档顶层文件，避免把已有分类子目录再次嵌套进「未识别」
            if only_set is None:
                if not e.is_file(follow_symlinks=False):
                    continue
            else:
                if not e.is_file(follow_symlinks=False) and not e.is_dir(follow_symlinks=False):
                    continue
                if os.path.normpath(os.path.abspath(e.path)).lower() not in only_set:
                    continue
            kind0 = "dir" if e.is_dir(follow_symlinks=False) else "file"
            info = _core.identify(e.name, e.path, kind0)
            cat = "子文件夹归档" if kind0 == "dir" else (info.get("category") or "未识别")
            cat_dir = cat if re.fullmatch(r"[\w\u4e00-\u9fff -]+", cat) \
                else "其他"
            target_dir = os.path.join(root, cat_dir)
            if os.path.normcase(os.path.normpath(e.path)) == os.path.normcase(os.path.normpath(target_dir)):
                continue
            os.makedirs(target_dir, exist_ok=True)
            dst = os.path.join(target_dir, e.name)
            k = 1
            stem, ext = os.path.splitext(e.name)
            while os.path.exists(dst):
                dst = os.path.join(target_dir, f"{stem}({k}){ext}")
                k += 1
            shutil.move(e.path, dst)
            moves.append({"name": e.name, "from": e.path, "to": dst,
                          "category": cat_dir})
        except OSError as exc:
            moves.append({"name": e.name, "error":
                          f"{type(exc).__name__}: {exc}"})
    moved = [m for m in moves if "error" not in m]
    try:
        with _core._lock:
            log = _core._load_json(_core.ARCHIVE_LOG_PATH, [], use_cache=False)
            if not isinstance(log, list):
                log = []
            if moved:
                log.insert(0, {"time": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"), "moves": moved})
            _core._atomic_write(_core.ARCHIVE_LOG_PATH, log[:20])
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return {"ok": True, "moved": len(moved),
            "errors": len(moves) - len(moved),
            "by_category": sorted({m["category"] for m in moved})}


def downloads_archive_undo() -> dict:
    """撤销最近一次归档：按日志把文件移回下载目录顶层。"""
    with _core._lock:
        log = _core._load_json(_core.ARCHIVE_LOG_PATH, [], use_cache=False)
        if not isinstance(log, list) or not log:
            return {"error": "没有可撤销的归档记录"}
        session = log[0]
        restored = 0
        errors = []
        remaining = []
        for m in session.get("moves", []):
            try:
                if not os.path.exists(m["to"]):
                    errors.append(f"{m['name']}：目标文件已不在")
                elif os.path.exists(m["from"]):
                    errors.append(f"{m['name']}：原位置已存在同名项，未覆盖")
                    remaining.append(m)
                else:
                    shutil.move(m["to"], m["from"])
                    restored += 1
            except OSError as exc:
                errors.append(f"{m['name']}：{exc}")
                remaining.append(m)
        if remaining:
            session["moves"] = remaining
        else:
            log.pop(0)
        _core._atomic_write(_core.ARCHIVE_LOG_PATH, log)
    return {"ok": restored > 0 and not errors, "restored": restored, "errors": errors}

# ======================================================================
#  磁盘趋势预测：预计剩余可用天数 + 高频回涨预警（v2.18 Task 3）
# ======================================================================
TREND_FORECAST_MIN_DAYS = 7     # 少于 7 天样本不做预测
TREND_FORECAST_WINDOW = 30      # 线性回归窗口（天）
TREND_FORECAST_WARN_DAYS = 30   # 剩余 ≤30 天才上前端预警横幅
TREND_FORECAST_CRIT_DAYS = 14   # 剩余 ≤14 天红色强调
TREND_FORECAST_MIN_SLOPE = 1024 * 1024   # 每天净减超过 1 MB 才谈「写满」


def trend_forecast(drive: str | None = None) -> dict:
    """基于 disk-trend.json 的剩余空间线性回归，预计「多少天后盘满」。

    数据源是 record_disk_snapshot 每天每盘一条的 (total, free) 记录
    （同日空间变化才更新，能反映真实消耗速率）。只读不写；样本不足、
    空间回升或消耗极低时如实返回 days_left=None，绝不危言耸听。
    同时附带 snapshot_diff 的 hot 目录（清理后反复回涨 ≥3 次），
    供前端预警横幅一并展示。
    """
    letter = (drive or "C").rstrip(":").upper()
    out = {"ok": True, "drive": letter, "samples": 0,
           "free": None, "free_h": "", "slope_per_day": 0, "slope_h": "",
           "days_left": None, "level": "none", "hot_dirs": []}
    hist = _core._load_json(_core.DISK_TREND_PATH, [], use_cache=False)
    by_date: dict = {}
    for day in hist if isinstance(hist, list) else []:
        if not isinstance(day, dict):
            continue
        rec = (day.get("u") or {}).get(letter)
        if isinstance(rec, dict) and isinstance(rec.get("free"), (int, float)):
            try:
                d0 = datetime.strptime(str(day.get("d") or ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            by_date[d0] = float(rec["free"])
    samples = sorted(by_date.items())[-TREND_FORECAST_WINDOW:]
    out["samples"] = len(samples)
    if not samples:
        out["reason"] = "还没有趋势数据（每天首次刷新会记录一次）"
        return out
    out["free"] = round(samples[-1][1])
    out["free_h"] = _core.human(out["free"])
    if len(samples) < TREND_FORECAST_MIN_DAYS:
        out["reason"] = (f"样本不足（{len(samples)}/{TREND_FORECAST_MIN_DAYS}"
                         " 天，连用几天后可预测）")
        return out
    t0 = samples[0][0]
    xs = [(d1 - t0).days for d1, _ in samples]
    ys = [f for _, f in samples]
    n0 = len(xs)
    mx = sum(xs) / n0
    my = sum(ys) / n0
    denom = sum((x - mx) ** 2 for x in xs)
    if not denom:
        return out
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    out["slope_per_day"] = round(slope)
    if slope < -TREND_FORECAST_MIN_SLOPE:
        days_left = out["free"] / (-slope)
        if days_left <= 3650:
            out["days_left"] = int(days_left)
            if days_left <= TREND_FORECAST_CRIT_DAYS:
                out["level"] = "crit"
            elif days_left <= TREND_FORECAST_WARN_DAYS:
                out["level"] = "warn"
            else:
                out["level"] = "info"
            out["slope_h"] = _core.human(-slope)
        else:
            out["reason"] = "按当前速率盘满还很久，无需预警"
    else:
        out["reason"] = "剩余空间稳定或回升，无需预警"
    # 高频回涨目录（snapshot_diff 的 hot 标记）一并带给横幅
    try:
        sd = snapshot_diff(letter, days=7)
        hot = [x for x in (sd.get("diff") or [])
               if x.get("hot") and (x.get("delta") or 0) > 0]
        out["hot_dirs"] = [{"path": x["path"], "delta_h": x.get("delta_h", "")}
                           for x in hot[:5]]
    except Exception:  # noqa: BLE001 —— 预警数据绝不拖垮主流程
        traceback.print_exc()
    return out
