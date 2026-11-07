# -*- coding: utf-8 -*-
"""游戏平台域（P2-1 域拆分批次 D2，从 core.py 迁出）。

Steam / Epic / GOG 平台清单发现（含 VDF 解析与注册表读取）、游戏库
自定义目录、安装根与卸载残留检测、游戏中文名、游戏库趋势记录。

本模块是被 core.py「再绑定」的迁出域：core 顶部
`from ocleaner.games import ...` 保持全部既有调用点的裸名兼容；
selftest 的 monkeypatch 落在 core 命名空间，本模块对 core 侧符号
（identify / human / 测量与 WizTree 表等）经 `_core.` 惰性引用访问。
"""

from __future__ import annotations  # S-01: 3.8/3.9 下 PEP 585/604 注解求值会崩
import json
import os
import re
import threading
import time
from datetime import datetime

import ocleaner.core as _core
from ocleaner.measure import _py_tree_size
from ocleaner.store import _atomic_write, _load_json

# GAMENAMES_PATH / GAMELIBS_PATH 依赖 CONFIG_DIR（模块级求值），留在 core；
# 函数内经 _core.* 惰性引用。

def record_game_history(total: int, count: int) -> None:
    """把游戏库总体积记进趋势快照：每天一条，保留 180 天。

    挂在 /__oc__/steam 的成功路径上（found 且 total>0 才记）。
    同一天再查时，只有体积或数量变了才更新 —— 卸载/删除游戏当天就能
    反映到趋势里；平时 Steam 清单读取结果不变，不会反复写盘。"""
    if not total:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    hist = _load_json(_core.GAME_HISTORY_PATH, [], use_cache=False)
    if not isinstance(hist, list):
        hist = []
    for day in hist:
        if isinstance(day, dict) and day.get("d") == today:
            if day.get("total") == total and day.get("count") == count:
                return
            day["total"] = total
            day["count"] = count
            _atomic_write(_core.GAME_HISTORY_PATH, hist)
            return
    hist.append({"d": today, "total": total, "count": count})
    _atomic_write(_core.GAME_HISTORY_PATH, hist[-180:])

def load_game_names() -> dict:
    """游戏中文名映射（appid → 中文名）。缺失时返回空表，不影响任何功能。"""
    return _load_json(_core.GAMENAMES_PATH, {"games": {}})

def game_display_name(appid, name: str, installdir: str = "") -> tuple[str, str]:
    """返回 (显示名, 中文名)。

    为什么要单独做这个：Steam 的 appmanifest 里只有 Steam 自己语言（多为英文）
    的游戏名，本地拿不到中文名，中文用户对着一堆英文目录名很难认。
    这张映射表只影响显示 —— 不参与识别、不参与清理判断、不碰文件。

    匹配顺序：appid 精确命中 → 英文名/安装目录模糊命中 → 原名。
    有中文名时**只显示中文名**（2026-10-13 用户要求：不用忠实原名）；
    原目录名在详情弹窗的路径里仍然可见，核对不受影响。
    """
    cfg = load_game_names().get("games") or {}
    entry = None
    key = str(appid or "")
    if key and key in cfg:
        entry = cfg[key]
    if entry is None and cfg:
        low = (name or "").lower()
        low_dir = (installdir or "").lower()
        for _aid, e in cfg.items():
            for m in (e.get("match") or []):
                m = str(m).lower()
                if not m:
                    continue
                if m == low or m == low_dir:
                    entry = e
                    break
                # 模糊匹配必须带词边界：否则「civilization v」会把
                # 「civilization vi」也命中（v 是 vi 的前缀），显示名张冠李戴
                # （2026-10-14 实例：文明 VI 的目录被标成 显示名「文明 5」）。
                # 前后不能是字母数字，保证按「完整单词」命中。
                if len(m) >= 5 and re.search(
                        r"(?<![a-z0-9])" + re.escape(m) + r"(?![a-z0-9])", low):
                    entry = e
                    break
            if entry:
                break
    zh = str((entry or {}).get("zh") or "").strip()
    if not zh or zh == name:
        return name, ""
    # 原名里已经有中文就直接用原名 —— 映射表是给英文目录名准备的
    if re.search(r"[\u4e00-\u9fff]", name or ""):
        return name, zh
    return zh, zh

GAME_INSTALL_MARKERS = ("\\steamapps\\", "\\common\\", "\\steamlibrary\\",
                        "\\epic games\\", "\\gog galaxy\\",
                        "\\ubisoft game launcher\\")

def _in_game_install(path: str) -> bool:
    low = "\\" + (path or "").lower()
    return any(m in low for m in GAME_INSTALL_MARKERS)

STEAM_STATE_FLAGS = {
    1: "未安装", 2: "更新中", 4: "已安装", 8: "更新待处理", 16: "已启用",
    32: "已预载", 64: "预载未解锁", 128: "已锁定", 256: "已计划更新",
    512: "需要密钥", 1024: "需要许可证", 2048: "正在校验",
}

def _parse_vdf(text: str) -> dict:
    """极简 Valve VDF（KeyValues）解析器。

    够用范围：嵌套对象、带引号的键值、转义反斜杠（"D:\\\\steam" 即 D:\\steam）。
    不支持注释等边角语法 —— 目标文件由 Steam 机器生成。
    """
    tokens: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\':
                    buf.append(text[i + 1])
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1
            tokens.append("".join(buf))
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        j = i
        while j < n and text[j] not in ' \t\r\n"{}':
            j += 1
        tokens.append(text[i:j])
        i = j

    pos = 0

    def parse_block() -> dict:
        nonlocal pos
        out: dict = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                break
            if pos + 1 >= len(tokens):
                break
            key = tok
            nxt = tokens[pos + 1]
            if nxt == "{":
                pos += 2
                out[key] = parse_block()
            else:
                out[key] = nxt
                pos += 2
        return out

    # 最外层形如  "AppState" { ... }  /  "libraryfolders" { ... }
    # 必须把外层键名重新包回去，否则它的子键会变成顶层键 ——
    # 这个 bug 曾导致 Steam 清单解析出 0 个游戏（而库路径看着正常，很难发现）。
    if len(tokens) > 1 and tokens[1] == "{":
        root_key = tokens[0]
        pos = 2
        return {root_key: parse_block()}
    if tokens and tokens[0] == "{":
        pos = 1
    return parse_block()

def discover_steam() -> dict:
    """发现 Steam 库与已安装游戏。数据全部来自 Steam 自己的清单文件。"""
    global _STEAM_ROOTS_CACHE
    empty = {"found": False, "roots": [], "libraries": [], "games": [],
             "orphans": [], "game_count": 0, "total_size": 0,
             "total_size_h": "0 B", "orphan_count": 0}
    if not _core.IS_WINDOWS:
        return empty
    r = _core.run_ps(_core.PS_STEAM, timeout=180)
    if "error" in r:
        return {**empty, "error": r["error"]}

    seen_roots: set[str] = set()
    roots: list[str] = []
    for x in _core._as_list(r.get("roots")):
        p = os.path.normpath(str(x))
        if p.lower() not in seen_roots:
            seen_roots.add(p.lower())
            roots.append(p)
    manifests = [str(x) for x in _core._as_list(r.get("manifests"))]
    if not manifests:
        return {**empty, "roots": roots}

    libraries: dict[str, dict] = {}
    games: list[dict] = []
    for entry in manifests:
        lib, _, acf_path = entry.partition("|")
        if not acf_path:
            continue
        # 库路径统一成 Windows 反斜杠形态。Steam 有的库来自 libraryfolders.vdf
        # （里面是 D:\\Steam），有的来自命令行探测（可能给 D:/Steam），
        # 混用会让后续拼出来的路径形如 d:/steam\\steamapps\\... ——
        # os.path 在 Windows 上把 '/' 当参数分隔符，这类路径在某些 API 上会静默失败。
        lib = os.path.normpath(lib.replace("/", "\\"))
        lib_key = os.path.normpath(lib).lower()
        libraries.setdefault(lib_key, {"path": lib, "apps": 0, "size": 0})
        try:
            with open(acf_path, "r", encoding="utf-8", errors="replace") as fh:
                state = _parse_vdf(fh.read()).get("AppState", {})
        except OSError:
            continue
        if not state:
            continue
        try:
            size = int(state.get("SizeOnDisk") or 0)
        except ValueError:
            size = 0
        try:
            flags = int(state.get("StateFlags") or 0)
        except ValueError:
            flags = 0
        installdir = state.get("installdir") or ""
        game_path = (os.path.join(lib, "steamapps", "common", installdir)
                     if installdir else "")
        info = _core.identify(installdir or state.get("name", ""), game_path, "dir")
        info.pop("match_score", None)

        libraries[lib_key]["apps"] += 1
        libraries[lib_key]["size"] += size
        raw_name = state.get("name") or installdir or "未命名"
        disp_name, zh_name = game_display_name(
            state.get("appid"), raw_name, installdir)
        games.append({
            "appid": state.get("appid") or "",
            "name": raw_name,              # Steam 上的原名，保持不动
            "display_name": disp_name,     # 界面显示用（有中文名时只显示中文名）
            "zh_name": zh_name,
            "installdir": installdir,
            "path": game_path,
            "exists": bool(game_path) and os.path.isdir(game_path),
            "size": size, "size_h": _core.human(size),
            "library": lib,
            "installed": bool(flags & 4),
            "state": STEAM_STATE_FLAGS.get(flags, f"状态码 {flags}"),
            "last_updated": _core._ts(state.get("LastUpdated")),
            "last_played": _core._ts(state.get("LastPlayed")),
            "buildid": state.get("buildid") or "",
            "category": info.get("category", "游戏"),
            "title": info.get("title", "Steam 游戏"),
            # 游戏本体是可清理项（与磁盘树里的判定保持一致）：没在玩的大作
            # 就是最值得回收的空间。risk 保持「高」，说明删掉的量级与影响。
            "risk": "高", "cleanable": True,
            "what": "Steam 安装的游戏本体，整个目录就是这款游戏的全部文件。",
            "advice": "没在玩的游戏可以删除来释放空间。更推荐在 Steam 客户端里"
                      "右键游戏 → 管理 → 卸载，它会同时清理库记录、不留残留；"
                      "也可以在这里直接删除整个目录，之后库里会显示「未安装」，"
                      "想再玩重新下载即可。"
                      "注意：少数游戏把存档/Mod 放在安装目录里，删前确认没有"
                      "想保留的东西（多数存档在 文档\\My Games、AppData 或云存档）。",
            "tip": "Steam 的『已安装 → 按大小排序』可按体积找出最久没玩的大作。",
        })

    # 多份 libraryfolders.vdf 会列出同一个库，同一游戏因此被读多次
    # （实测同一款游戏会被读到 3 次）。先按安装路径去重，
    # 再**用去重后的结果反推库统计** —— 否则库的「应用数/体积」会翻倍虚高，
    # 与游戏列表对不上，用户会以为哪里出错了。
    dedup: dict[str, dict] = {}
    for g in games:
        key = os.path.normpath(g["path"]).lower() if g["path"] else f"appid:{g['appid']}"
        if key not in dedup or g["size"] > dedup[key]["size"]:
            dedup[key] = g
    games = sorted(dedup.values(), key=lambda g: -g["size"])

    lib_stats: dict[str, dict] = {}
    for g in games:
        k = os.path.normpath(g["library"]).lower()
        slot = lib_stats.setdefault(k, {"path": os.path.normpath(g["library"]),
                                        "apps": 0, "size": 0})
        slot["apps"] += 1
        slot["size"] += g["size"]

    # 按盘符聚合：用户明确要求看到「D 盘 / E 盘各占了多少」。
    # 这里同时给出该盘的容量与占用率，才能回答「Steam 吃掉了这个盘多少」。
    by_drive: dict[str, dict] = {}
    disk_cache: dict[str, dict] = {}
    for v in lib_stats.values():
        letter = os.path.splitdrive(v["path"])[0].rstrip(":").upper()
        if not letter:
            continue
        d = by_drive.setdefault(letter, {"drive": letter, "apps": 0, "size": 0,
                                         "libraries": []})
        d["apps"] += v["apps"]
        d["size"] += v["size"]
        d["libraries"].append({"path": v["path"], "apps": v["apps"],
                               "size": v["size"], "size_h": _core.human(v["size"])})
    for letter, d in by_drive.items():
        info = disk_cache.setdefault(letter, _core.get_disk_info(letter))
        d["size_h"] = _core.human(d["size"])
        d["disk_total"] = info.get("total", 0)
        d["disk_total_h"] = info.get("total_h", "")
        d["disk_free_h"] = info.get("free_h", "")
        d["disk_percent"] = info.get("percent", 0)
        d["share"] = (round(d["size"] / info["total"] * 100, 1)
                      if info.get("total") else 0)
        d["libraries"].sort(key=lambda x: -x["size"])

    lib_list = sorted(
        ({**v, "size_h": _core.human(v["size"]), "exists": os.path.isdir(v["path"])}
         for v in lib_stats.values()),
        key=lambda x: -x["size"])

    # 差异核对：库里存在但 Steam 清单里没有的游戏目录 ——
    # 卸载不干净留下的残留，是最值得回收的一类
    known = {os.path.normpath(g["path"]).lower() for g in games if g["path"]}
    orphans: list[dict] = []
    for lib in lib_list:
        common = os.path.join(lib["path"], "steamapps", "common")
        if not os.path.isdir(common):
            continue
        try:
            for e in os.scandir(common):
                if not e.is_dir(follow_symlinks=False):
                    continue
                if os.path.normpath(e.path).lower() in known:
                    continue
                orphans.append({"name": e.name, "path": e.path,
                                "library": lib["path"], "size": 0,
                                "size_h": "未测量"})
        except OSError:
            continue

    # 残留目录测体积：这些是「Steam 已无记录但目录还在」的空间，
    # 通常是最值得回收的一类，不测体积就没有意义
    if orphans:
        # 固定名放系统临时目录、反复覆盖、不做删除：这里的 unlink 曾落在
        # 仓库目录且每次扫描必删一次，在带安全删除钩子的宿主环境里
        # 会耗尽删除配额并让钩子抛 SystemExit 杀死整个扫描线程
        #（表现为「扫描报错」，CI 无钩子不复现）。
        # P2-4：进程内测量（免临时清单 + PS 冷启动）；与旧实现一致，
        # 只测前 40 个，其余体积记 0 并在下面被过滤掉。
        _sizes = {}
        for _o in orphans[:40]:
            _sizes[os.path.normpath(_o["path"]).lower()] = _py_tree_size(_o["path"])
        for o in orphans:
            sz = _sizes.get(os.path.normpath(o["path"]).lower(), 0)
            o["size"] = sz
            o["size_h"] = _core.human(sz)
        orphans = [o for o in orphans if o["size"] > 0]
        orphans.sort(key=lambda o: -o["size"])

    total = sum(g["size"] for g in games)
    # Steam 根目录留给封面接口用（/__oc__/game-cover）：只在确认清单可读
    # （found）时才更新缓存，避免用半途失败的结果覆盖旧值
    _STEAM_ROOTS_CACHE = list(roots)
    return {
        "found": True, "roots": roots,
        "libraries": lib_list, "games": games,
        "by_drive": sorted(by_drive.values(), key=lambda x: -x["size"]),
        "game_count": len(games),
        "total_size": total, "total_size_h": _core.human(total),
        "orphans": orphans[:20], "orphan_count": len(orphans),
        "orphan_size": sum(o["size"] for o in orphans),
        "orphan_size_h": _core.human(sum(o["size"] for o in orphans)),
    }

def _epic_game_from_manifest(d: dict) -> dict | None:
    """把一份 Epic .item 清单（JSON dict）归一化成游戏记录；不是游戏的返回 None。

    实测同目录下还躺着引擎/插件/工具类清单（Unreal Engine、Quixel Bridge、
    Fab 插件），它们的共同点是 bIsApplication=False 或 CatalogNamespace="ue"，
    两条判据必须同时用：UE 本体 bIsExecutable=True 单靠可执行标记踢不掉。
    """
    if not isinstance(d, dict):
        return None
    if not d.get("bIsApplication"):
        return None
    if str(d.get("CatalogNamespace") or "").lower() == "ue":
        return None
    loc = str(d.get("InstallLocation") or "").strip()
    if len(loc) < 4 or loc[1] != ":":
        return None
    try:
        size = int(d.get("InstallSize") or 0)
    except (TypeError, ValueError):
        size = 0
    name = str(d.get("DisplayName") or d.get("AppName") or "未命名").strip()
    return {"appid": str(d.get("AppName") or ""), "name": name or "未命名",
            "path": os.path.normpath(loc), "size": size,
            "launch": str(d.get("LaunchExecutable") or "")}

def discover_epic() -> dict:
    """发现 Epic Games 已安装游戏。数据全部来自启动器自己的 .item JSON 清单，
    纯标准库读取几 KB 文件，微秒级，不需要注册表或外部进程。"""
    empty = {"found": False, "games": [], "game_count": 0,
             "total_size": 0, "total_size_h": "0 B",
             "orphans": [], "orphan_count": 0, "orphan_size_h": "0 B"}
    if not _core.IS_WINDOWS:
        return empty
    mdir = os.path.join(os.environ.get("PROGRAMDATA") or r"C:\ProgramData",
                        "Epic", "EpicGamesLauncher", "Data", "Manifests")
    try:
        names = os.listdir(mdir)
    except OSError:
        return empty

    games: list[dict] = []
    for fn in names:
        if not fn.lower().endswith(".item"):
            continue
        try:
            with open(os.path.join(mdir, fn), "r",
                      encoding="utf-8-sig", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        g = _epic_game_from_manifest(data)
        if not g:
            continue
        exists = os.path.isdir(g["path"])
        size = g["size"]
        if size <= 0 and exists:
            # 清单里没记体积（实测引擎类条目如此）才现场测量，游戏类一般都有
            size = _core._size_of_dir(g["path"])
        info = _core.identify(g["name"], g["path"], "dir")
        info.pop("match_score", None)
        games.append({
            "appid": g["appid"], "platform": "epic",
            "name": g["name"], "display_name": g["name"], "zh_name": "",
            "installdir": os.path.basename(g["path"]),
            "path": g["path"], "exists": exists,
            "size": size, "size_h": _core.human(size),
            "library": os.path.dirname(g["path"]),
            "installed": True,
            "category": "游戏", "title": "Epic 游戏",
            "risk": "高", "cleanable": False,
            "what": info.get("what") or "Epic Games 安装的游戏本体。",
            "advice": "要卸载请在 Epic Games 启动器里操作（库 → 右键游戏 → 卸载），"
                      "它会同步清掉清单记录，不留残留。",
        })

    # 同一安装位置被多份清单引用时按路径去重，保留体积大的那份
    dedup: dict[str, dict] = {}
    for g in games:
        k = g["path"].lower()
        if k not in dedup or g["size"] > dedup[k]["size"]:
            dedup[k] = g
    games = sorted(dedup.values(), key=lambda g: -g["size"])
    total = sum(g["size"] for g in games)
    orphans = _platform_orphans("epic", [g["path"] for g in games])
    return {"found": bool(games), "games": games, "game_count": len(games),
            "total_size": total, "total_size_h": _core.human(total),
            "orphans": orphans[:20], "orphan_count": len(orphans),
            "orphan_size_h": _core.human(sum(o["size"] for o in orphans))}

def _gog_game_from_values(game_id: str, vals: dict) -> dict | None:
    """把一个 GOG 注册表项的值集合归一化成游戏记录；缺关键字段返回 None。

    拆成纯函数是为了能用合成数据做单元测试 —— 本机没装 GOG 时
    注册表路径走不到，解析逻辑照样被钉死。
    """
    path = str(vals.get("path") or "").strip()
    if len(path) < 4 or path[1] != ":":
        return None
    name = str(vals.get("gameName") or "").strip() or f"GOG 游戏 {game_id}"
    return {"appid": str(game_id), "name": name, "path": os.path.normpath(path)}

def discover_gog() -> dict:
    """发现 GOG Galaxy 已安装游戏。数据来自注册表
    HKLM\\SOFTWARE\\(WOW6432Node\\)GOG.com\\Games —— GOG 客户端自己维护的清单位置。
    注册表不记体积，逐个现场测量（GOG 库通常不大，代价可接受）。"""
    empty = {"found": False, "games": [], "game_count": 0,
             "total_size": 0, "total_size_h": "0 B",
             "orphans": [], "orphan_count": 0, "orphan_size_h": "0 B"}
    if not _core.IS_WINDOWS:
        return empty
    try:
        import winreg
    except ImportError:
        return empty

    games: list[dict] = []
    for root in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root)
        except OSError:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for i in range(count):
                try:
                    sub_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, sub_name) as sub:
                        vals: dict = {}
                        j = 0
                        while True:
                            try:
                                vn, vv, _ = winreg.EnumValue(sub, j)
                                vals[vn] = vv
                                j += 1
                            except OSError:
                                break
                except OSError:
                    continue
                g = _gog_game_from_values(sub_name, vals)
                if not g:
                    continue
                exists = os.path.isdir(g["path"])
                size = _core._size_of_dir(g["path"]) if exists else 0
                info = _core.identify(g["name"], g["path"], "dir")
                info.pop("match_score", None)
                games.append({
                    "appid": g["appid"], "platform": "gog",
                    "name": g["name"], "display_name": g["name"], "zh_name": "",
                    "installdir": os.path.basename(g["path"]),
                    "path": g["path"], "exists": exists,
                    "size": size, "size_h": _core.human(size),
                    "library": os.path.dirname(g["path"]),
                    "installed": True,
                    "category": "游戏", "title": "GOG 游戏",
                    "risk": "高", "cleanable": False,
                    "what": info.get("what") or "GOG 安装的游戏本体。",
                    "advice": "要卸载请在 GOG Galaxy 里操作（已安装 → 管理 → 卸载），"
                              "或用游戏目录里的 unins*.exe。",
                })
        finally:
            winreg.CloseKey(key)

    dedup: dict[str, dict] = {}
    for g in games:
        k = g["path"].lower()
        if k not in dedup or g["size"] > dedup[k]["size"]:
            dedup[k] = g
    games = sorted(dedup.values(), key=lambda g: -g["size"])
    total = sum(g["size"] for g in games)
    orphans = _platform_orphans("gog", [g["path"] for g in games])
    return {"found": bool(games), "games": games, "game_count": len(games),
            "total_size": total, "total_size_h": _core.human(total),
            "orphans": orphans[:20], "orphan_count": len(orphans),
            "orphan_size_h": _core.human(sum(o["size"] for o in orphans))}

_ORPHAN_NAME_EXCLUDES = {"launcher", "launchers", "epic games launcher"}

_ORPHAN_ROOTS_CACHE: dict = {"key": None, "roots": {}}

def platform_install_roots() -> dict[str, set[str]]:
    """Epic / GOG「安装位置的可能父目录」（小写归一化），60 秒缓存。

    孤儿检测与 clean-game 的二次识别共用这一份 —— 两边对「哪里算安装根」
    的认识必须一致，否则前端列出来的删不掉，或后端允许的没被列出来。"""
    key = time.time() // 60
    if _ORPHAN_ROOTS_CACHE["key"] == key:
        return _ORPHAN_ROOTS_CACHE["roots"]
    roots: dict[str, set[str]] = {"epic": set(), "gog": set()}
    if _core.IS_WINDOWS:
        mdir = os.path.join(os.environ.get("PROGRAMDATA") or r"C:\ProgramData",
                            "Epic", "EpicGamesLauncher", "Data", "Manifests")
        try:
            for fn in os.listdir(mdir):
                if not fn.lower().endswith(".item"):
                    continue
                try:
                    with open(os.path.join(mdir, fn), "r",
                              encoding="utf-8-sig", errors="replace") as fh:
                        d = json.load(fh)
                except (OSError, ValueError):
                    continue
                g = _epic_game_from_manifest(d)
                if g and os.path.isdir(g["path"]):
                    roots["epic"].add(os.path.normpath(
                        os.path.dirname(g["path"])).lower())
        except OSError:
            pass
        roots["epic"].add(r"c:\program files\epic games")
        try:
            import winreg
            for hive in (r"SOFTWARE\WOW6432Node\GOG.com\Games",
                         r"SOFTWARE\GOG.com\Games"):
                try:
                    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, hive)
                except OSError:
                    continue
                try:
                    for i in range(winreg.QueryInfoKey(k)[0]):
                        try:
                            with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                                p, _ = winreg.QueryValueEx(sub, "path")
                        except OSError:
                            continue
                        p = str(p or "").strip()
                        if len(p) >= 4 and os.path.isdir(p):
                            roots["gog"].add(os.path.normpath(
                                os.path.dirname(os.path.normpath(p))).lower())
                finally:
                    winreg.CloseKey(k)
        except (ImportError, OSError):
            pass
        roots["gog"].add(r"c:\gog games")
        roots["gog"].add(r"c:\program files (x86)\gog galaxy\games")
    _ORPHAN_ROOTS_CACHE["key"] = key
    _ORPHAN_ROOTS_CACHE["roots"] = roots
    return roots

def _platform_orphans(plat: str, known_paths: list[str]) -> list[dict]:
    """平台安装根下的直接子目录中，不在该平台记录里的 = 疑似残留。"""
    roots = platform_install_roots().get(plat) or set()
    known = {os.path.normpath(p).lower() for p in known_paths if p}
    out: list[dict] = []
    seen: set[str] = set()
    for root in sorted(roots):
        try:
            with os.scandir(root) as it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    if e.name.lower() in _ORPHAN_NAME_EXCLUDES:
                        continue
                    low = os.path.normpath(e.path).lower()
                    if low in known or low in seen:
                        continue
                    seen.add(low)
                    out.append({"name": e.name, "path": e.path,
                                "size": 0, "size_h": "未测量"})
        except OSError:
            continue
    for o in out:
        sz, _trunc = _core._size_of_dir_budgeted(o["path"], max_entries=50000)
        o["size"] = sz
        o["size_h"] = _core.human(sz)
    out = [o for o in out if o["size"] > 0]
    out.sort(key=lambda o: -o["size"])
    return out

def discover_games() -> dict:
    """全平台游戏库汇总（Steam + Epic + GOG），供 /__oc__/games 与磁盘树标注使用。

    Steam 走自己的完整负载（含库/残留/分盘统计），Epic/GOG 以统一游戏记录并入，
    每条记录都带 platform 字段，前端按平台分组显示。"""
    steam = discover_steam()
    epic = discover_epic()
    gog = discover_gog()
    games: list[dict] = []
    for g in (steam.get("games") or []):
        games.append({**g, "platform": "steam"})
    games.extend(epic.get("games") or [])
    games.extend(gog.get("games") or [])
    games.sort(key=lambda g: -g["size"])
    total = sum(g["size"] for g in games)
    return {
        "found": bool(games), "games": games, "game_count": len(games),
        "total_size": total, "total_size_h": _core.human(total),
        "platforms": {
            "steam": {"found": bool(steam.get("found")),
                      "game_count": steam.get("game_count", 0),
                      "total_size_h": steam.get("total_size_h", "0 B")},
            "epic": {"found": bool(epic.get("found")),
                     "game_count": epic.get("game_count", 0),
                     "total_size_h": epic.get("total_size_h", "0 B"),
                     "orphans": epic.get("orphans", []),
                     "orphan_count": epic.get("orphan_count", 0),
                     "orphan_size_h": epic.get("orphan_size_h", "0 B")},
            "gog": {"found": bool(gog.get("found")),
                    "game_count": gog.get("game_count", 0),
                    "total_size_h": gog.get("total_size_h", "0 B"),
                    "orphans": gog.get("orphans", []),
                    "orphan_count": gog.get("orphan_count", 0),
                    "orphan_size_h": gog.get("orphan_size_h", "0 B")},
        },
        "steam": steam,   # 完整 Steam 负载原样透传，面板继续用它渲染库/残留/分盘
    }

_GAME_DIRS_CACHE: dict = {"ts": 0.0, "dirs": {}, "libs": set()}

_STEAM_COMMON_CACHE: dict = {"ts": 0.0, "common": set(), "libs": set()}

_GAMELIBS_LOCK = threading.Lock()

_GAMELIB_CACHE: dict = {}          # 归一化路径 -> 快照（体积 + 顶层子项）

_GAMELIB_CACHE_TTL = 600.0         # 10 分钟：游戏库体积不会秒级变化

def load_game_libs() -> list[dict]:
    """读取用户自定义游戏库。结构 {"name","path"}；path 存原始大小写
    （显示用），比较用归一化 key。畸形条目直接丢弃。"""
    data = _load_json(_core.GAMELIBS_PATH, {"libs": []}, use_cache=False)
    libs = data.get("libs", []) if isinstance(data, dict) else []
    out = []
    for g in libs:
        if not isinstance(g, dict):
            continue
        name = str(g.get("name") or "").strip()
        p = str(g.get("path") or "").replace("/", "\\").strip().rstrip("\\")
        if not name or not p:
            continue
        out.append({"name": name, "path": p, "key": os.path.normpath(p).lower()})
    return out

def save_game_libs(libs: list[dict]) -> None:
    _atomic_write(_core.GAMELIBS_PATH, {"libs": libs})

def _gamelib_children(path: str) -> list[dict]:
    """自定义库的顶层子项。有 WizTree 表查表（O(1)、带真实体积）；
    没有就 scandir：目录给不出体积（标 approx，size 0），文件有精确大小。"""
    letter = os.path.splitdrive(path)[0].rstrip(":").upper()
    tbl = _core._wiz_table(letter)
    out: list[dict] = []
    if tbl:
        for k in _core._wiztree_children(tbl, path, limit=60):
            out.append({"name": k["name"], "path": k["path"],
                        "size": int(k.get("size") or 0), "type": "dir",
                        "approx": False})
    else:
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            out.append({"name": e.name, "path": e.path,
                                        "size": 0, "type": "dir", "approx": True})
                        elif e.is_file(follow_symlinks=False):
                            out.append({"name": e.name, "path": e.path,
                                        "size": e.stat().st_size,
                                        "type": "file", "approx": False})
                    except OSError:
                        continue
        except OSError:
            pass
        out.sort(key=lambda x: -x["size"])
    return out[:60]

def gamelib_snapshot(path: str) -> dict:
    """单个自定义库的展示快照：总体积 + 顶层子项，带缓存。

    体积优先查 WizTree 内存表（该盘扫过一次就有了，毫秒级）；
    没有表才现场走盘，带条目预算 —— 超预算给**下限**并标 partial，
    前端按「约」处理。快照缓存 10 分钟，重复进页面不再重复走盘。"""
    key = os.path.normpath(path).lower()
    now = time.time()
    with _GAMELIBS_LOCK:
        c = _GAMELIB_CACHE.get(key)
        if c and now - c["at"] < _GAMELIB_CACHE_TTL:
            return c
    letter = os.path.splitdrive(path)[0].rstrip(":").upper()
    tbl = _core._wiz_table(letter)
    size: int | None = None
    partial = False
    if tbl:
        size = tbl.get(key)
        if size is None:
            size = tbl.get(key + "\\")
        if size is not None:
            size = int(size)
    if size is None:
        size, partial = _core._size_of_dir_budgeted(path, max_entries=120000)
    snap = {"size": size, "size_h": _core.human(size), "partial": bool(partial),
            "children": _gamelib_children(path)}
    with _GAMELIBS_LOCK:
        _GAMELIB_CACHE[key] = {**snap, "at": now}
    return snap

def steam_common_dirs() -> tuple[set[str], set[str]]:
    """Steam 各库的 steamapps\\common 目录与库根目录（小写归一化）。

    返回 (common 集合, 库根集合)。占用树用它判定「这个目录是不是游戏本体」，
    删除游戏的端点也用它做**服务端二次确认** —— 前端传来的路径必须后端自己
    也认得出是游戏本体，否则一律拒绝（不能让页面拥有任意目录删除能力）。
    60 秒缓存：读清单只要几毫秒，但树识别与删除校验都会高频调用。"""
    now = time.time()
    if now - _STEAM_COMMON_CACHE["ts"] < 60:
        return _STEAM_COMMON_CACHE["common"], _STEAM_COMMON_CACHE["libs"]
    common: set[str] = set()
    libs: set[str] = set()
    try:
        ds = discover_steam()
        for g in (ds.get("games") or []):
            lib = str(g.get("library") or "")
            if lib:
                libs.add(os.path.normpath(lib).lower())
        for lib in libs:
            common.add(os.path.normpath(os.path.join(lib, "steamapps", "common")).lower())
    except Exception:
        pass
    _STEAM_COMMON_CACHE.update({"ts": now, "common": common, "libs": libs})
    return common, libs

def game_install_dirs() -> tuple[dict, set]:
    """Epic/GOG 游戏安装目录索引：({小写路径: 平台}, {库目录小写路径})。

    磁盘占用树与逐层拆子项都要用它标注「这是游戏本体」。读清单只要几毫秒，
    但 GOG 要现场测体积，所以加 60 秒缓存避免每次展开子项都重扫注册表。
    Steam 不走这里：它有自己更精确的 steamapps\\common 双亲判定。"""
    now = time.time()
    # 注意不要写 `and _GAME_DIRS_CACHE["dirs"]`：没装 Epic/GOG 的机器上目录集恒为空，
    # 加了这个条件等于「空结果永不缓存」，每次展开子项都重扫注册表 + 测体积。
    if now - _GAME_DIRS_CACHE["ts"] < 60:
        return _GAME_DIRS_CACHE["dirs"], _GAME_DIRS_CACHE["libs"]
    dirs: dict[str, str] = {}
    libs: set[str] = set()
    for plat, disc in (("epic", discover_epic), ("gog", discover_gog)):
        try:
            for g in (disc().get("games") or []):
                p = str(g.get("path") or "")
                if not p:
                    continue
                k = os.path.normpath(p).lower()
                dirs[k] = plat
                lib = os.path.dirname(k)
                if lib and len(lib) > 3:   # 库目录是盘符根时不收，防全盘误标
                    libs.add(lib)
        except Exception:
            continue
    _GAME_DIRS_CACHE.update({"ts": now, "dirs": dirs, "libs": libs})
    return dirs, libs
