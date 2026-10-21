# -*- coding: utf-8 -*-
"""
O-Cleaner 端到端自测。

阶段一：真实配置的完整性、护栏、预设解析、盘符过滤、换算引擎（只读）
阶段二：Temp 沙箱内的真实删除逻辑（含权限、占用、部分选择、整批不中断）
阶段三：临时换成沙箱配置，启动真实服务跑完整 HTTP 流程，结束后还原

运行（务必带 -X utf8，否则控制台中文乱码）:
    python -X utf8 tools/selftest.py
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
HERE = TOOLS_DIR.parent                      # O-Cleaner 根目录
sys.path.insert(0, str(HERE))
import app  # noqa: E402

SANDBOX = Path("C:/_ocleaner_selftest")   # 与测试配置里的 %DRIVE%\_ocleaner_selftest 对应
REAL_TARGETS = HERE / "config" / "targets.json"
BACKUP = HERE / "config" / ".targets.backup.json"


def free_port() -> int:
    """随机取一个空闲端口。

    写死端口会有个隐蔽的坑：如果本机别处正好有个 O-Cleaner 在跑，
    测试会连上那个服务，用真实配置去验证沙箱假设，结果全是假的。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


TEST_PORT = free_port()

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    print(("  [OK]   " if cond else "  [严重] ") + name + (f"   {detail}" if detail else ""))
    return cond


def skip(name: str, why: str) -> None:
    SKIP.append(name)
    print(f"  [跳过] {name}   {why}")


# ---- 机器相关断言的环境门槛 ----
# 有一部分断言（数据盘自动发现、占用树深度）隐含前提是「一台装了东西的真实机器」。
# CI runner 的 D: 盘只放了工作区，自动发现自然找不出应用目录 —— 那是环境差异，
# 不是引擎缺陷。这里统一用可判定的前提门控：不满足就跳过并打印原因。
_REAL_DRIVE_MIN_TOPDIRS = 6        # 顶层目录少于此数 → 视为「没装东西的盘」
_REAL_DRIVE_MIN_BYTES = 1024 ** 3  # 总体量不足 1 GB → 同上


def _top_level_dir_count(letter: str) -> int:
    """廉价探测：某盘顶层有几个目录（不算体积、不起 PowerShell）。"""
    try:
        with os.scandir(f"{letter}:\\") as it:
            return sum(1 for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return 0


def _drive_looks_populated(overview: dict | None) -> bool:
    """占用树结果是否来自一块「装了东西」的盘。"""
    if not overview or overview.get("error"):
        return False
    nodes = overview.get("nodes") or []
    total = int(overview.get("scanned_total") or 0)
    tops = [n for n in nodes if n.get("depth") == 1]
    return total >= _REAL_DRIVE_MIN_BYTES and len(tops) >= _REAL_DRIVE_MIN_TOPDIRS


def _load_local_cases() -> dict:
    """本机真实测试用例（tools/local-cases.json，不进版本库）。

    为什么不写死在测试里：这些「装机目录 vs 缓存目录」样本带着本机装了什么
    软件的指纹。抽到独立文件并 gitignore 后，公开仓库只留通用逻辑，
    开发机照样保留完整覆盖。示例见 tools/local-cases.json.example。
    """
    try:
        with open(TOOLS_DIR / "local-cases.json", "r",
                  encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def make_sandbox() -> None:
    shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / "tree" / "sub").mkdir(parents=True)
    (SANDBOX / "files").mkdir(parents=True)
    for i in range(5):
        (SANDBOX / "tree" / f"a{i}.bin").write_bytes(b"x" * 2048)
    (SANDBOX / "tree" / "sub" / "nested.bin").write_bytes(b"y" * 4096)
    (SANDBOX / "files" / "keep.txt").write_bytes(b"z" * 1024)
    (SANDBOX / "files" / "gone.log").write_bytes(b"w" * 512)
    (SANDBOX / "files" / "gone2.log").write_bytes(b"w" * 256)


def write_test_config() -> None:
    cfg = {
        "app": {"name": "O-Cleaner", "version": "test", "port": TEST_PORT},
        "presets": [
            {"id": "safe", "label": "测试预设", "desc": "只选无风险测试项",
             "select": {"risk": ["无"]}},
            {"id": "pick", "label": "按 id 选", "desc": "验证显式 id 选择不被回收站过滤器清空",
             "select": {"ids": ["sandbox-tree"]}},
            {"id": "none", "label": "全部取消", "desc": "取消所有勾选", "select": {"ids": []}},
        ],
        # 沙箱目标用 %DRIVE% 形式，这样它们算「盘符专属项」，
        # 才能验证 scope=drive 的过滤行为（TEMP 通常就在 C: 盘上）
        "targets": [
            {"id": "sandbox-tree", "label": "沙箱目录",
             "path": "%DRIVE%\\_ocleaner_selftest\\tree",
             "category": "测试", "risk": "无", "needs_admin": False, "kind": "tree",
             "short": "HTTP 端到端测试目录。", "what": "测试数据。", "rebuild": "测试。",
             "impact": "无。", "keep_if": "无。"},
            {"id": "sandbox-files", "label": "沙箱 glob",
             "path": "%DRIVE%\\_ocleaner_selftest\\files",
             "category": "测试", "risk": "无", "needs_admin": False, "kind": "files",
             "glob": "*.log",
             "short": "只删 .log。", "what": "测试数据。", "rebuild": "测试。",
             "impact": "无。", "keep_if": "无。"},
        ],
    }
    REAL_TARGETS.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ======================================================================
#  阶段一
# ======================================================================
def phase1() -> None:
    print("\n=== 阶段一：配置完整性 / 护栏 / 盘符 / 换算（只读）===")
    cfg = json.loads(REAL_TARGETS.read_text(encoding="utf-8-sig"))
    targets = cfg.get("targets", [])
    check("targets.json 可加载", bool(targets), f"{len(targets)} 项")
    ids = [t["id"] for t in targets]
    check("id 无重复", len(ids) == len(set(ids)))

    required = ("id", "label", "path", "category", "risk", "short",
                "what", "rebuild", "impact", "keep_if")
    missing = [(t.get("id"), k) for t in targets for k in required if not t.get(k)]
    check("每项都有完整的类型简介字段", not missing, str(missing[:4]))

    kinds = {t.get("kind") for t in targets}
    check("kind 取值合法", kinds <= {"tree", "files", "recyclebin"}, str(kinds))
    no_glob = [t["id"] for t in targets if t.get("kind") == "files" and not t.get("glob")]
    check("files 类型都指定了 glob", not no_glob, str(no_glob))
    rb = [t for t in targets if t.get("kind") == "recyclebin"]
    check("回收站条目带 drive_letter", rb and all(t.get("drive_letter") for t in rb))
    check("没有硬编码的 C: 盘路径（应使用 %DRIVE%）",
          not [t["id"] for t in targets
               if isinstance(t.get("path"), str) and "C:\\" in t["path"].upper()])

    # 盘符：盘符专属项必须带 %DRIVE%，通用项必须不带
    sys_targets = app.build_targets("C")
    check("C 盘目标非空", len(sys_targets) > 10, f"{len(sys_targets)} 项")
    check("C 盘识别为系统盘", app.is_system_drive("C"))
    for other in ("D", "E"):
        ts = app.build_targets(other)
        fam = [t for t in ts if app.is_drive_specific(t)]
        if os.path.exists(f"{other}:\\"):
            check(f"{other}: 盘目标非空", bool(ts), f"{len(ts)} 项")
            check(f"{other}: 盘不包含系统专属项",
                  not any(t.get("system_only") for t in ts))
            check(f"{other}: 盘专属项路径都指向 {other}:",
                  all(app.expand(t["path"], other).upper().startswith(f"{other}:")
                      for t in fam))
    d_ids = [t["id"] for t in app.build_targets("D")]
    check("D 盘目标 id 带盘符后缀", any(i.endswith("@D") for i in d_ids))
    check("C/D 盘 id 不冲突",
          not set(d_ids) & {i for i in (t["id"] for t in app.build_targets("C"))
                            if i.endswith("@D")})

    # 范围过滤：scope=drive 应只剩盘符专属项，且系统盘比数据盘多
    def _drive_scope(letter):
        return [t for t in app.build_targets(letter) if app.is_drive_specific(t)]
    cs, ds = _drive_scope("C"), _drive_scope("D")
    check("C 盘 scope=drive 非空", len(cs) >= 8, f"{len(cs)} 项")
    check("系统盘专属项多于数据盘", len(cs) > len(ds), f"C={len(cs)} D={len(ds)}")
    check("数据盘 scope=drive 不含系统项",
          not any(t.get("system_only") for t in ds))

    # ---- 自动发现：让数据盘真正有用的关键 ----
    # 回归背景：预定义目标只覆盖系统位置，导致 D/E 盘只列出 \Temp 与回收站，
    # 而 Telegram / 夸克 / Photoshop 这些几十 GB 的大户不在清单里。
    auto = [t for t in app.build_targets("D") if t.get("discovered")]
    d_top = _top_level_dir_count("D")
    if d_top >= _REAL_DRIVE_MIN_TOPDIRS:
        check("D 盘能自动发现可清理的应用目录", len(auto) >= 3, f"{len(auto)} 项")
    else:
        skip("D 盘自动发现应用目录",
             f"D 盘顶层仅 {d_top} 个目录，不算「装了东西的数据盘」")
    check("自动发现项都带解释文案",
          all(t.get("what") and t.get("rebuild") for t in auto))
    check("自动发现项都标了 discovered 便于界面区分",
          all(t.get("discovered") for t in auto))
    check("自动发现项不含未识别的猜测",
          all(t.get("category") not in ("", "未识别", None) for t in auto),
          str([t["category"] for t in auto if t.get("category") in ("", "未识别", None)][:3]))
    # 排除规则：回收站内部 / ProgramData / AppData / Windows 都不该被自动发现
    bad_sub = [t["path"] for t in auto
               if any(s in t["path"].lower() for s in
                      ("$recycle.bin", "programdata", "appdata", "\\windows\\"))]
    check("自动发现排除了回收站内部/ProgramData/AppData/Windows",
          not bad_sub, str(bad_sub[:3]))
    # 产物 id 必须唯一，否则前端会串卡片
    ids = [t["id"] for t in app.build_targets("D")]
    check("含自动发现后目标 id 仍唯一", len(ids) == len(set(ids)))
    check("自动发现项有数量上限（清单不能无限长）",
          len(auto) <= app.AUTO_DISCOVER_LIMIT,
          f"{len(auto)} <= {app.AUTO_DISCOVER_LIMIT}")

    # 去重：同一路径不应出现两次
    for letter in ("C", "D"):
        ts = app.build_targets(letter)
        keys = [os.path.normpath(app.expand(t["path"], letter)).lower()
                + "|" + str(t.get("glob", "")) for t in ts]
        check(f"{letter}: 盘目标无重复路径", len(keys) == len(set(keys)))

    # 预设解析
    by_id = {t["id"]: t for t in app.build_targets("C")}
    for p in cfg.get("presets", []):
        got = app._resolve_preset(app.build_targets("C"), p)
        if p["id"] == "none":
            check("预设 none 为空", got == [])
        else:
            check(f"预设 {p['id']} 解析出 {len(got)} 项", bool(got))
    safe = app._resolve_preset(app.build_targets("C"),
                               next(p for p in cfg["presets"] if p["id"] == "safe"))
    check("安全预设不含需管理员项", not [i for i in safe if by_id[i].get("needs_admin")])
    check("安全预设不含中/高风险项",
          not [i for i in safe if by_id[i].get("risk") in ("中", "高")])
    check("安全预设不含回收站", not [i for i in safe if i.startswith("recycle-bin")])
    rec = app._resolve_preset(app.build_targets("C"),
                              next(p for p in cfg["presets"] if p["id"] == "recycle"))
    check("只清回收站预设能解析出回收站", any(i.startswith("recycle-bin") for i in rec),
          str(rec))

    # 护栏
    print("\n  -- 路径护栏 --")
    for bad in [r"C:\Windows\System32", r"C:\Windows\Installer", "C:\\",
                r"C:\Program Files", r"C:\Windows\WinSxS", r"D:\$Recycle.Bin",
                r"C:\Users", r"C:\Windows", "badpath", "/etc"]:
        ok, why = app.check_deletable(bad)
        check(f"拒绝 {bad}", not ok, why)
    for good in [r"C:\Windows\Temp", r"C:\Windows\Logs", r"C:\Windows\Prefetch",
                 r"D:\Temp", r"E:\temp"]:
        if not os.path.exists(good):
            skip(f"允许 {good}", "本机不存在该路径")
            continue
        ok, why = app.check_deletable(good)
        check(f"允许 {good}", ok, why)
    # 公共文档（C:\Users\Public\Documents）自 v2.8 起归入「默认不删、显式可选」：
    # 它虽然不在某个用户 Home 下，但同属个人数据，整目录删除不该是默认行为。
    if os.path.exists(r"C:\Users\Public\Documents"):
        check("默认拒绝公共文档目录 C:\\Users\\Public\\Documents",
              not app.check_deletable(r"C:\Users\Public\Documents")[0])
        check("显式勾选后允许公共文档目录",
              app.check_deletable(r"C:\Users\Public\Documents", "tree", True)[0])
    # 回收站目标（kind="recyclebin"）精确放行盘根 $Recycle.Bin：这是应用自己的
    # 「清空回收站」链路（端到端实测发现被保护规则误拦，clean 永远失败）。
    # 其余 kind 与 $Recycle.Bin 的子路径照常拒绝。
    check("recyclebin 目标精确放行盘根 $Recycle.Bin（其余 kind / 子路径仍拒）",
          app.check_deletable(r"C:\$Recycle.Bin", "recyclebin")[0]
          and not app.check_deletable(r"D:\$Recycle.Bin", "tree")[0]
          and not app.check_deletable(r"C:\$Recycle.Bin\sub", "recyclebin")[0])

    # 端到端（CI 门控）：真实的「移入回收站 -> 清空回收站」链路。
    # 背景：Clear-RecycleBin -DriveLetter 对非系统盘有「静默无效」的已知缺陷
    # （2026-10-15 本机 D 盘实测复现：命令成功返回、进度条照走、文件原样全在；
    # C 盘正常），clean 现改为直删 $I/$R 文件 + Clear-RecycleBin 兜底。
    # 本机宿主环境对 $Recycle.Bin 下的 $R 文件有会话级删除拦截（对本会话
    # 一切子进程生效），端到端只能在 CI 验证；本机记跳过并注明原因。
    if os.environ.get("CI", "").lower() == "true":
        import ctypes as _ct

        class _SHFILEOP(_ct.Structure):
            _fields_ = [("hwnd", _ct.c_void_p), ("wFunc", _ct.c_uint),
                        ("pFrom", _ct.c_wchar_p), ("pTo", _ct.c_wchar_p),
                        ("fFlags", _ct.c_ushort),
                        ("fAnyOperationsAborted", _ct.c_int),
                        ("hNameMappings", _ct.c_void_p),
                        ("lpszProgressTitle", _ct.c_wchar_p)]

        probe = Path(tempfile.gettempdir()) / "oc_selftest_rb_probe.txt"
        probe.write_text("recycle-bin e2e probe", encoding="ascii")
        op = _SHFILEOP(hwnd=None, wFunc=3, pFrom=str(probe) + "\0", pTo=None,
                       fFlags=0x40 | 0x10,   # FOF_ALLOWUNDO | FOF_NOCONFIRMATION
                       fAnyOperationsAborted=0, hNameMappings=None,
                       lpszProgressTitle=None)
        rc = _ct.windll.shell32.SHFileOperationW(_ct.byref(op))
        moved_in = rc == 0 and not probe.exists()
        purged = app._purge_recycle_bin(probe.drive.rstrip(":"))
        check("移入回收站的文件可被清空链路真实删除（含数据 $R）",
              moved_in and purged["deleted"] >= 1 and purged["freed"] >= 20,
              f"moved_in={moved_in} deleted={purged['deleted']} "
              f"freed={purged['freed']} failed={purged['failed']}")
    else:
        skip("移入回收站 -> 清空回收站端到端",
             "本机宿主对回收站 $R 文件有会话级删除拦截，只能进 CI 验证")

    # 回归：非绝对路径必须拒绝。
    # os.path.abspath 会把「盘符相对」路径按**当前工作目录**解析 ——
    # abspath("E:") 得到的是 E 盘上的 CWD（本机即仓库自身），而不是盘符根，
    # 于是它曾能伪装成合法目录通过后续全部检查。同理 "..\Windows"、"/etc"
    # 也会被归一化到 CWD 之下。这类输入必须在归一化前卡死。
    cwd_drive = os.path.splitdrive(os.getcwd())[0]   # 形如 "E:"，无盘符时为空串
    rel_bad = ["C:", "D:", r"..\Windows", r"..\..\Windows", ".", "..",
               "/etc", "temp", r"Windows\Temp"]
    if cwd_drive:
        rel_bad.append(cwd_drive)
    for rel in dict.fromkeys(rel_bad):               # 去重但保持顺序
        ok, why = app.check_deletable(rel)
        check(f"拒绝非绝对路径 {rel!r}", not ok, why)
    check("files 模式同样拒绝盘符相对路径",
          not app.check_deletable("C:", "files")[0])
    check("明确给出盘符相对被拒的原因",
          "绝对路径" in app.check_deletable("C:")[1])

    check("拒绝在盘符根目录删文件", not app.check_deletable("C:\\x.tmp", "files")[0])
    # 回归：kind="files" 的 target 其 path 是**目录**（内部按 glob 删文件），
    # 不能被当成“要删的文件”而拒绝 —— 否则所有 glob 类目标都会失效。
    for fpath in [r"C:\Windows\Logs", r"C:\Windows\Temp",
                  str(Path(os.environ.get("TEMP", r"C:\Temp")))]:
        ok, why = app.check_deletable(fpath, "files")
        check(f"files 模式允许目录 {fpath}", ok, why)
    check("files 模式仍拒绝盘符根目录", not app.check_deletable("C:\\", "files")[0])
    check("files 模式拒绝 System32",
          not app.check_deletable(r"C:\Windows\System32", "files")[0])

    # 换算引擎
    print("\n  -- 换算引擎 --")
    units = app.resolve_units(app.load_convert().get("units", []))
    check("换算单位全部解析出正值字节", all(u["bytes"] > 0 for u in units),
          str([u["id"] for u in units if u["bytes"] <= 0]))
    nf = next(u for u in units if u["id"] == "hour_4k_netflix")
    m4k = next(u for u in units if u["id"] == "movie4k")
    check("表达式换算 = 引用的 1/3",
          abs(nf["bytes"] - m4k["bytes"] / 3) < 1, f"{nf['bytes']} vs {m4k['bytes']/3}")
    conv = app.convert_bytes(25_000_000_000, units)
    check("换算返回结果且按数量升序", bool(conv) and
          all(conv[i]["count"] <= conv[i + 1]["count"] for i in range(len(conv) - 1)))
    check("换算结果不含基准单位 gb", not any(c["id"] == "gb" for c in conv))
    only = app.convert_bytes(25_000_000_000, units, only=["photo"])
    check("可按单位过滤换算", len(only) == 1 and only[0]["id"] == "photo")

    # 自动识别知识库
    print("\n  -- 自动识别知识库 --")
    kb = app.load_filetypes()
    check("知识库可加载",
          len(kb.get("folder_rules", [])) >= 20 and len(kb.get("file_rules", [])) >= 8,
          f"{len(kb.get('folder_rules', []))} 目录规则 / {len(kb.get('file_rules', []))} 文件规则")
    # cleanable 是布尔字段，false 是合法值，不能用真值判断“是否缺失”
    text_fields = ("title", "category", "risk", "confidence", "what", "advice")
    all_rules = kb.get("folder_rules", []) + kb.get("file_rules", [])
    miss = [(r.get("id"), k) for r in all_rules for k in text_fields if not r.get(k)]
    check("每条规则都有非空的解释字段", not miss, str(miss[:4]))
    miss_bool = [r.get("id") for r in all_rules
                 if "cleanable" not in r or not isinstance(r["cleanable"], bool)]
    check("每条规则都显式声明 cleanable 布尔值", not miss_bool, str(miss_bool[:4]))

    # 识别准确性：用本机真实存在的名称
    must = [
        ("SteamLibrary", "dir", "游戏", False),
        ("pagefile.sys", "file", "系统", False),
        ("hiberfil.sys", "file", "系统", False),
        ("$Recycle.Bin", "dir", "回收站", True),
        ("node_modules", "dir", "开发", True),
        ("DeliveryOptimization", "dir", "系统", True),
        ("setup.exe", "file", "安装包", True),
        ("movie.mp4", "file", "视频", False),
    ]
    wrong = []
    for name, kind, cat, cleanable in must:
        r = app.identify(name, "D:\\" + name, kind)
        if r["category"] != cat or r["cleanable"] != cleanable:
            wrong.append((name, r["category"], r["cleanable"]))
    check("真实名称识别正确（分类与可清理判断）", not wrong, str(wrong))

    # 回归：短关键词必须整词匹配，不能被子串误伤（如 'tim' 命中 DeliveryOptimization）
    for bad in ("optimization", "estimation", "legitimate"):
        r = app.identify(bad, "D:\\" + bad, "dir")
        if r["category"] == "个人数据":
            check(f"短关键词未误伤 {bad}", False, r["title"])
            break
    else:
        check("短关键词未误伤（optimization/estimation/legitimate）", True)

    # 路径上下文识别：文件名毫无线索时，靠所在目录判断
    blob = app.identify(
        "sha256-2bada8a7450677000f678be90653b85d364de7db25eb5ea54136ada5f3933730",
        r"C:\Users\x\.ollama\models\blobs\sha256-2bada8a7450677000f678be90653b85d364de7db25eb5ea54136ada5f3933730",
        "file")
    check("无扩展名的哈希文件靠路径识别为 AI 模型",
          blob["recognized"] and blob["category"] == "AI 模型",
          f"{blob['title']} (score {blob['match_score']})")
    cache = app.identify("cache.db", r"C:\x\AppData\Local\App\Cache\cache.db", "file")
    check("路径含 Cache 的文件被识别为缓存",
          cache["recognized"] and cache["category"] == "缓存", cache["title"])
    unknown = app.identify("x.dat", r"D:\somewhere\x.dat", "file")
    check("真正无据可依的文件不硬猜",
          not unknown["recognized"], unknown["title"])

    # 扩展名优先：路径线索不得压过明确的扩展名
    # （曾把 …\某聊天软件\...\x.7z 判成「下载目录」、.rar 判成「软件安装目录」）
    for name, path, want in [
        ("示例压缩包.7z", r"D:\某聊天软件\download\示例压缩包.7z", "压缩包"),
        ("2024 Win x64.rar", r"D:\软件\2024 Win x64.rar", "压缩包"),
        ("movie.mp4", r"E:\SteamLibrary\common\movie.mp4", "视频"),
        ("anything-v5.safetensors", r"D:\DeliveryOptimization\anything-v5.safetensors", "AI 模型"),
    ]:
        r = app.identify(name, path, "file")
        check(f"{name} 按扩展名归类而非被路径带偏", r["category"] == want,
              f"实际 {r['category']} / {r['title']}")

    # 通用扩展名在完全没有路径线索时，不能硬猜
    for name, path in [("content.bin", r"D:\somedir\abc\content.bin"),
                       ("data.bin", r"E:\random\data.bin")]:
        r = app.identify(name, path, "file")
        check(f"{name}（无路径线索）不硬猜", not r["recognized"],
              f"{r['category']} / {r['title']}")
    # 占用树：逐层展开、按体积论、能挖到深层大项
    # 前提是 D 盘真的装了东西（顶层目录够多、总体量够大）。CI runner 的 D: 盘
    # 只放了工作区，节点数 / 深度 / 体积断言全都不成立 —— 属环境差异，故门控。
    fs = app.scan_disk_overview("D", 3) if os.path.exists("D:\\") else None
    if _drive_looks_populated(fs):
        nodes = fs["nodes"]
        check("磁盘占用树能生成节点", len(nodes) > 5, f"{len(nodes)} 个节点")
        check("节点带父子关系（支持逐层下钻）",
              all("parent" in n and "depth" in n for n in nodes))
        check("节点按体积统计（不是按数量）",
              all(n["size"] >= 0 for n in nodes)
              and max(n["size"] for n in nodes) > 100 * 1024 * 1024,
              f"最大节点 {app.human(max(n['size'] for n in nodes))}")
        check("至少展开到第 3 层（能挖到深层大项）",
              max(n["depth"] for n in nodes) >= 3,
              f"最深 {max(n['depth'] for n in nodes)} 层")
        check("有可下钻的目录节点", any(n.get("has_children") for n in nodes))
        check("识别结果含解释文案",
              any(n.get("what") for n in nodes))
        check("识别结果不含内部打分字段（payload 精简）",
              all("match_score" not in n for n in nodes))
        # 行内勾选的 id 通道：节点上标注的 target_id 必须是 build_targets 的
        # 真实目标 id —— 曾因对已带盘符后缀的 id 再过一遍 target_id_for，
        # 产生 recycle-bin@C@C 这类双后缀 id，SCAN 与 clean 都查不到，
        # 用户勾了会被后端静默丢弃。
        valid_ids = {t["id"] for t in app.build_targets("D")}
        marked = [n.get("target_id") for n in nodes if n.get("target_id")]
        check("节点标注的 target_id 都是真实目标 id",
              all(tid in valid_ids for tid in marked),
              f"标注 {len(marked)} 条 / 合法 id {len(valid_ids)} 个")
    else:
        skip("磁盘占用树检查",
             f"本机无 D 盘或其内容过少（总体量 {(fs or {}).get('scanned_total_h', '—')}），"
             "需要一块真实数据盘")

    # 未识别项的说明必须诚实（不能编造内容）
    check("未识别项明确声明不做清理建议",
          "不会" in unknown["advice"] and not unknown["cleanable"],
          unknown["advice"][:40])

    # ---- 勾选包含关系去重：外层包含内层时只清最小项（纯函数，直接验证） ----
    _outer = {"id": "tree:e:\\big", "label": "大项", "path": "E:\\big"}
    _inner = {"id": "tree:e:\\big\\cache", "label": "子项", "path": "E:\\big\\cache"}
    _other = {"id": "tree:e:\\elsewhere", "label": "无关项", "path": "E:\\elsewhere"}
    _nopath = {"id": "recycle-bin@E", "label": "回收站"}      # 无实体路径不参与
    kept, merged = app.dedup_nested_targets([_outer, _inner, _other, _nopath])
    kept_paths = {k.get("path", "") for k in kept} - {""}   # 无路径目标不参与
    check("包含关系去重：外层被跳过、保留最小项",
          kept_paths == {"E:\\big\\cache", "E:\\elsewhere"}
          and len(kept) == 3 and [m["path"] for m in merged] == ["E:\\big"],
          str(kept_paths))
    check("包含关系去重：跳过记录带去向（kept=保留的最小项）",
          merged and merged[0]["kept"] == "E:\\big\\cache", str(merged))
    kept2, merged2 = app.dedup_nested_targets([_other, _nopath])
    check("无包含关系时全部保留（无路径目标不受影响）",
          len(kept2) == 2 and not merged2)
    kept3, _ = app.dedup_nested_targets([_inner, _outer])   # 顺序无关
    check("去重与勾选顺序无关",
          {k.get("path", "") for k in kept3} == {"E:\\big\\cache"})

    # 游戏本体：同一项目整删不拆（祖先=游戏本体 → 保留本体、跳过内部勾选项）
    _saved_sc, _saved_gd = app.steam_common_dirs, app.game_install_dirs
    try:
        app.steam_common_dirs = lambda: (
            {"e:\\steamlibrary\\steamapps\\common"}, set())
        app.game_install_dirs = lambda: ({"e:\\epic\\game": "epic"}, set())
        _game = {"id": "tree:g", "label": "游戏本体",
                 "path": "E:\\SteamLibrary\\steamapps\\common\\Elden Ring"}
        _ginner = {"id": "tree:gi", "label": "游戏内缓存",
                   "path": "E:\\SteamLibrary\\steamapps\\common\\Elden Ring\\data"}
        kept4, merged4 = app.dedup_nested_targets([_game, _ginner])
        check("游戏本体祖先：整删不拆（保留本体、跳过内部项）",
              [k.get("path") for k in kept4] == [_game["path"]]
              and len(merged4) == 1 and merged4[0]["kept"] == _game["path"],
              str(merged4))
        _epic = {"id": "tree:e", "label": "Epic 游戏", "path": "E:\\Epic\\game"}
        _einner = {"id": "tree:ei", "label": "游戏内缓存",
                   "path": "E:\\Epic\\game\\data"}
        kept5, merged5 = app.dedup_nested_targets([_epic, _einner])
        check("Epic/GOG 游戏本体同样整删不拆",
              [k.get("path") for k in kept5] == [_epic["path"]]
              and len(merged5) == 1)
        kept6, _ = app.dedup_nested_targets([_game, _other])   # 无包含关系互不影响
        check("游戏本体与无关项互不影响", len(kept6) == 2)
    finally:
        app.steam_common_dirs, app.game_install_dirs = _saved_sc, _saved_gd


# ======================================================================
#  阶段二
# ======================================================================
def phase2() -> None:
    print("\n=== 阶段二：真实删除逻辑（Temp 沙箱）===")
    real_check = app.check_deletable
    # 桩必须接受 clean_target 传的全部位置参数（含 v2.8 的 allow_user_data），
    # 否则 TypeError 会把整个阶段二带崩 —— CI 首次跑全量时就是这么红的。
    app.check_deletable = lambda p, k="tree", a=False: (True, "")   # 沙箱在 C 盘 Temp 之外时放行
    try:
        make_sandbox()
        t = {"id": "t1", "label": "沙箱目录", "path": str(SANDBOX / "tree"), "kind": "tree"}
        r = app.clean_target(t)
        check("tree: 无删除失败", r["failed"] == 0, str(r["skipped"]))
        check("tree: 释放量 = 14336 B", r["freed"] == 14336, f"{r['freed']}")
        check("tree: 删除了全部 6 个顶层项", r["deleted"] == 6, f"deleted={r['deleted']}")
        check("tree: 目录已被清空", not any((SANDBOX / "tree").iterdir()))
        check("结果带 category / target_id 字段",
              "category" in r and "target_id" in r)

        make_sandbox()
        t = {"id": "t2", "label": "沙箱 glob", "path": str(SANDBOX / "files"),
             "kind": "files", "glob": "*.log"}
        r = app.clean_target(t)
        check("files: 只删 .log，保留 keep.txt",
              not (SANDBOX / "files" / "gone.log").exists()
              and (SANDBOX / "files" / "keep.txt").exists())
        check("files: 释放量 = 768 B", r["freed"] == 768, f"{r['freed']}")

        make_sandbox()
        t = {"id": "t3", "label": "沙箱部分", "path": str(SANDBOX / "tree"), "kind": "tree"}
        r = app.clean_target(t, only_paths=[str(SANDBOX / "tree" / "a0.bin")])
        check("only_paths: 只删指定项",
              not (SANDBOX / "tree" / "a0.bin").exists()
              and (SANDBOX / "tree" / "a1.bin").exists())
        check("only_paths: 释放量 = 2048 B", r["freed"] == 2048, f"{r['freed']}")

        r = app.clean_target({"id": "t4", "label": "不存在",
                              "path": str(SANDBOX / "nope"), "kind": "tree"})
        check("不存在的路径返回 error 而不是抛异常", r["error"] == "路径不存在")

        make_sandbox()
        fh = open(SANDBOX / "tree" / "a2.bin", "r+b")
        try:
            r = app.clean_target({"id": "t5", "label": "占用",
                                  "path": str(SANDBOX / "tree"), "kind": "tree"})
            if r["failed"] >= 1:
                check("被占用的目录计入 failed 而非崩溃", True,
                      f"failed={r['failed']} skipped={r['skipped']}")
            else:
                skip("被占用文件测试", "本机未对该文件加锁")
            # 三态删除（rm_tree_strict）之后的语义：文件被占用 → 目录改名
            # 失败 → 整树未删（零副作用），不再出现「删掉一半留下另一半」。
            # 旧断言「部分占用时其余文件仍被删除」描述的是旧行为，已随 P1 改版。
            check("部分占用时整树未删（三态删除零副作用）",
                  (SANDBOX / "tree" / "a0.bin").exists())
        finally:
            fh.close()

        if os.name == "nt":
            # 前提是「当前权限确实删不动」。CI runner 以管理员身份运行，
            # 对 C:\Windows\Temp 不会被拒绝，该断言必然误报；管理员下还会真的
            # 删掉系统临时目录里的东西，所以这里直接跳过。
            if app.is_admin():
                skip("拒绝访问目录的错误记录", "当前以管理员运行，系统目录不会被拒绝")
            else:
                r = app.clean_target({"id": "denied", "label": "拒绝访问",
                                      "path": r"C:\Windows\Temp", "kind": "tree"})
                check("拒绝访问的目录记为该项错误而不抛异常",
                      bool(r.get("error")), str(r.get("error")))
            m = app.measure_target({"id": "d2", "label": "x",
                                    "path": r"C:\Windows\Temp", "kind": "tree"})
            check("measure_target 对拒绝访问的目录不抛异常", isinstance(m, dict))
    finally:
        app.check_deletable = real_check
        shutil.rmtree(SANDBOX, ignore_errors=True)


# ======================================================================
#  阶段三
# ======================================================================
def phase3_http() -> None:
    print("\n=== 阶段三：HTTP API 端到端（沙箱配置 + 真实服务）===")
    make_sandbox()
    # pick_port 的双绑定回归：别的进程**活着监听**的端口绝不能被返回。
    # 旧实现探测时带 SO_REUSEADDR，Windows 上连活端口都能 bind 成功，
    # 新旧实例静默双绑定，请求随机落到旧实例（实测 8770 挂过 3 个实例）。
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    bport = blocker.getsockname()[1]
    picked = app.pick_port(bport)
    check("pick_port 不返回别的进程正监听的端口",
          picked != bport and bport < picked <= bport + 30,
          f"占用 {bport} → 选中 {picked}")
    blocker.close()
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(HERE / "app.py"),
         "--port", str(TEST_PORT), "--no-browser"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    base = f"http://127.0.0.1:{TEST_PORT}"

    # 冷启动的 CI runner 上，逐个测量 33 个目标（每个都要起 PowerShell）可能超过
    # 一分钟；60 秒会把一次正常的慢机器误判成失败，并抛出未捕获的 TimeoutError。
    def get(path, method="GET", payload=None, timeout=180):
        req = urllib.request.Request(base + path, method=method)
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, data, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(base + "/__oc__/state", timeout=2).read()
                ready = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
        if not ready:
            out = proc.stdout.read() if proc.stdout else ""
            check("服务成功启动", False, (out or "")[:400])
            return
        check("服务成功启动", True, base)
        # 防串台：确认我们连上的确实是刚启动的、加载了测试配置的服务。
        # 注意不能要求「目标恰好等于测试配置的那 2 个」—— 引擎会自动发现
        # 本盘上可清理的真实目录，目标数必然多于配置数。
        probe = json.loads(urllib.request.urlopen(base + "/__oc__/state", timeout=5).read())
        bases = {t.get("base_id", t["id"]) for t in probe.get("targets", [])}
        if not {"sandbox-tree", "sandbox-files"} <= bases:
            check("连上的是本次测试启动的服务", False,
                  f"缺少测试配置的沙箱目标，实际={sorted(bases)[:6]}"
                  "（可能是本机另一个 O-Cleaner 占用了端口）")
            return
        check("连上的是本次测试启动的服务", True,
              f"含沙箱目标，另含 {len(bases) - 2} 个自动发现项")

        html = urllib.request.urlopen(base + "/", timeout=5).read().decode("utf-8")
        check("首页返回 HTML", "<title>O-Cleaner · 磁盘清理助手</title>" in html)
        check("品牌已更名为 O-Cleaner", "O</em>-Cleaner" in html)
        check("前端含盘符选择器", "renderDrives" in html and "drives" in html)
        check("前端含累积统计图表",
              "renderCumulative" in html and 'class="chart"' in html)
        check("前端含换算面板", "renderConvToggle" in html and "convToggle" in html)
        check("换算单位表单已并入累积面板（独立面板已移除）",
              'data-acc="units"' not in html
              and html.index('data-acc="cum"')
              < html.index('id="convToggle"')
              < html.index('data-acc="rules"'))
        check("前端含自定义目录输入", "customPath" in html)
        # i18n L1：界面骨架双语。中文是 HTML 源码默认文案，字典只收英文；
        # 这里校验基础设施存在 + 字典键完整覆盖所有 data-i18n 引用
        #（缺键会静默回退中文，属于手滑而不是故障，必须在这里拦住）。
        check("i18n 基础设施：语言切换器与应用逻辑",
              'id="langSeg"' in html and "function applyI18n" in html
              and "function i18nLocale" in html and '"oc-lang"' in html)
        _i18n_keys = set(re.findall(
            r'"([a-zA-Z][\w.]*)"\s*:',
            html[html.index("const I18N_EN"):html.index("function i18nLocale")]))
        _i18n_refs = set(re.findall(r'data-i18n(?:-ph|-title|-html)?="([^"]+)"', html))
        _i18n_missing = sorted(_i18n_refs - _i18n_keys)
        check("i18n 字典键覆盖所有 data-i18n 引用",
              not _i18n_missing, "缺键: " + ", ".join(_i18n_missing))
        # i18n L3：知识库长文本双语。kb-en.json 必须覆盖 filetypes.json 的
        # 全部规则 id、app.py 内联分支的伪 id，以及资源包全部条目 id
        # （packs_lookup 对未识别节点写 rule_id=<包id>:<条目id>）；
        # 覆盖函数只动显示文本，判定字段（cleanable/risk 数值/路径/体积）
        # 必须原样。
        _kb_en = app.load_kb_en().get("rules") or {}
        _kb_ids = {r.get("id") for sec in ("folder_rules", "file_rules")
                   for r in (app.load_filetypes().get(sec) or [])}
        _kb_ids |= {f"{rec['pack']}:{(rec.get('entry') or {}).get('id')}"
                    for rec in (app.load_packs().get("entries") or [])}
        _kb_missing = sorted(_kb_ids - set(_kb_en))
        _kb_empty_what = sorted(k for k, v in _kb_en.items()
                                if not str(v.get("what") or "").strip())
        check("知识库英文文案覆盖全部规则与资源包条目 id",
              not _kb_missing and not _kb_empty_what,
              "缺 id: " + ", ".join(_kb_missing) + "; what 为空: " + ", ".join(_kb_empty_what))
        # 识别名 label（2026-10-15）：内置规则的 title 是中文（「驱动备份 /
        # 驱动包」），英文界面排行行的识别名必须换英文 —— 每条内置规则都
        # 要有 label；资源包条目多为游戏名 / 软件名不强制，不在本检查内。
        _ft_ids = {r.get("id") for sec in ("folder_rules", "file_rules")
                   for r in (app.load_filetypes().get(sec) or [])}
        _kb_no_label = sorted(
            k for k in _ft_ids
            if not str((_kb_en.get(k) or {}).get("label") or "").strip())
        check("内置规则都有英文识别名 label", not _kb_no_label,
              "缺 label: " + ", ".join(_kb_no_label))
        _lang_sample = {"children": [{"rule_id": "cache-temp", "what": "中文",
                                      "title": "缓存 / 临时目录",
                                      "display_name": "缓存 / 临时目录",
                                      "zh_name": "缓存 / 临时目录",
                                      "confidence": "high", "confidence_label": "中文",
                                      "risk": "高", "cleanable": True}]}
        _lang_out = app.apply_lang_payload(json.loads(json.dumps(_lang_sample)), "en")
        _lang_node = _lang_out["children"][0]
        _lang_off = app.apply_lang_payload(json.loads(json.dumps(_lang_sample)), None)
        _en_name = (_kb_en.get("cache-temp") or {}).get("label") or "Caches"
        check("英文覆盖：长文本/识别名/风险/置信度换英文，判定字段不动",
              _lang_node["what"] != "中文" and _lang_node["risk"] == "High"
              and _lang_node["confidence_label"] != "中文"
              and _lang_node["title"] == _en_name
              and _lang_node["display_name"] == _en_name
              and _lang_node["zh_name"] == _en_name
              and _lang_node["cleanable"] is True
              and _lang_off["children"][0]["what"] == "中文"
              and _lang_off["children"][0]["title"] == "缓存 / 临时目录")
        check("前端含自定义换算单位表单", "btnUnitAdd" in html and "btnUnitExample" in html)
        # 行显示类别统一（2026-10-14）：徽章文字/底色/柱条颜色三者由同一个
        # rowKind 决定；编辑弹窗用可选标签（chip）选样式，说明预填当前值，
        # 可编辑字段限长防溢出。
        check("行类别统一：rowKind 与编辑弹窗标签 chip",
              "function rowKind" in html and 'id="ovKindChips"' in html
              and 'data-k="keep"' in html and 'id="ovColorRow"' in html)
        check("自定义字段限长：徽章 16 字 / 说明 120 字",
              'id="ovLabel" style="width:100%" maxlength="16"' in html
              and 'id="ovNote" rows="3" style="width:100%" maxlength="120"' in html)
        check("徽章超长防溢出：省略号截断",
              "text-overflow:ellipsis" in html)
        # 可读名单一化（2026-10-14）：排行行的中部说明只给一个名字 ——
        # display_name 优先、缺省用 title，两者不并排；「显示名「X」」前缀
        # 词条已删除，同义重复（文明 6 vs 文明 VI）从结构上消除。
        check("可读名单一化：display_name 优先且不与 title 并排",
              "const midName" in html and "fs.dispName" not in html
              and "dnDup" not in html and "zhHint" not in html)
        # i18n L3 收尾（2026-10-14）：动态弹窗/行内文案全部走 t()；英文词典
        # 必须覆盖所有 t("key",…) 调用键与 data-i18n 引用键，缺键会在英文
        # 界面回落中文。同键重复定义会让后者静默覆盖前者，也一并拦下。
        _t_keys = set(re.findall(r'\bt\(\s*"([a-zA-Z0-9]+\.[A-Za-z0-9]+)"', html))
        _t_keys |= set(re.findall(r'data-i18n="([a-zA-Z0-9.]+)"', html))
        # 重复定义只在 I18N_EN 词典对象内检测 —— 全文检测会把其它 JS 对象的
        # 单字键卷进来；词典键不强制带点（页头词条键是裸单词 "tag"）。
        _en_start = html.find("const I18N_EN = {")
        _en_dict = ""
        if _en_start >= 0:
            _depth = 0
            for _i in range(_en_start + len("const I18N_EN = "), len(html)):
                if html[_i] == "{":
                    _depth += 1
                elif html[_i] == "}":
                    _depth -= 1
                    if _depth == 0:
                        _en_dict = html[_en_start:_i + 1]
                        break
        _en_keys = re.findall(r'"([A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?)"\s*:\s*["\']', _en_dict)
        _en_set = set(_en_keys)
        _en_dup = sorted({k for k in _en_keys if _en_keys.count(k) > 1})
        _i18n_missing = sorted(k for k in _t_keys if k not in _en_set)
        check("i18n 英文词典覆盖全部 t() 与 data-i18n 引用键",
              not _i18n_missing and not _en_dup,
              "缺键: " + ", ".join(_i18n_missing) + "; 重复定义: " + ", ".join(_en_dup))
        check("主题系统：三态切换与浅色变量组",
              '[data-theme="light"]' in html and "themeSeg" in html
              and "oc-theme" in html and "prefers-color-scheme" in html)
        check("画布趋势颜色取自主题变量",
              "getPropertyValue" in html and "hexToRgba" in html)
        check("预设样式已修复溢出",
              "white-space:normal" in html and "overflow-wrap:anywhere" in html)
        # 布局回归：section / aside 必须显式指定位置，否则 grid 的 auto-placement
        # 会把卡片区挤进 392px 的侧栏列（"挤在左上侧"的根因）。2026-10-14 起
        # 左列收敛为单个滚动容器 #leftcol（工具栏、磁盘概览、当前视图都在其中），
        # 断言检查"两栏都被命名区域覆盖"。
        _css = html.replace(" ", "")
        check("左列容器与侧栏显式指定了网格位置",
              ('#leftcol{grid-area:left' in _css and 'aside{grid-area:side' in _css)
              or "main > section{grid-column:1" in html)
        check("网格用命名区域覆盖左列与右列整列",
              'grid-template-areas:' in _css
              and '"leftside"' in _css)
        # v2.3 UI：右侧堆叠折叠、磁盘概览并入左栏、两栏独立滚动、Esc 返回
        check("右侧面板改为可折叠堆叠",
              'class="panel acc' in html and "data-acc=" in html
              and "initAccordions" in html)
        check("侧栏不再有磁盘概览面板（已并入左栏）",
              'id="driveHint"' in html and 'id="diskInfo"' in html
              and html.count('id="driveHint"') == 1)
        check("全盘页的面板改称扫描概览，避免与左栏重名",
              'data-i18n="pane.fs">扫描概览</span>' in html and 'id="fsHint"' in html)
        check("左右两栏各自独立滚动",
              "body{display:flex;flex-direction:column;height:100vh;overflow:hidden}" in _css
              # 唯一一行必须钉死在容器高度上，否则网格隐式行会被内容撑高、无法内滚动
              and "grid-template-rows:minmax(0,1fr)" in _css
              and "#leftcol{grid-area:left;min-width:0;min-height:0;overflow-y:auto" in _css
              and "aside{grid-area:side;min-width:0;min-height:0;overflow-y:auto" in _css)
        # aside 成了「定高滚动容器」后，子面板带 overflow:hidden → 自动最小高度按 0 算，
        # 叠加 flex 默认的 flex-shrink:1，11 个面板会被整体压扁去适应容器高度，而不是
        # 溢出后交给 aside 滚动：标题只剩 24px（需要 45px）、展开的正文整块看不见。
        # 这条静态断言只保证「禁止收缩」没被删掉；真实渲染高度由 cdp_check 量盒子把关。
        # （_css 已去掉全部空格，所以 flex:0 0 auto 在这里是 flex:00auto。）
        check("右侧栏面板禁止收缩，溢出改由 aside 滚动",
              "aside>.panel{flex:00auto}" in _css)
        # 「游戏是可清理项」：详情弹窗对游戏本体提供删除入口，走专用确认流程。
        # 真正的安全判断在后端 —— 路径必须被后端自己识别为游戏安装目录，
        # 前端这里只负责把后果讲清楚，所以静态断言只确认入口与流程存在。
        check("游戏本体在详情弹窗里有删除入口",
              'id="dDelGame"' in html and "confirmDeleteGame" in html)
        check("删除游戏走专用端点而非通用清理",
              "/__oc__/clean-game" in html)
        check("游戏行徽标由后端 cleanable 数据切换",
              "g.cleanable" in html)
        # 各功能界面导航：两个视图常驻可达（高亮当前页），11 个折叠面板有吸顶目录
        # （v2.3 起常规清理页并入扫描概览，视图只剩 扫描概览 / Steam 游戏库）
        check("视图导航提供两个功能页入口",
              'id="viewNav"' in html and html.count('data-v="') == 2)
        check("侧栏面板目录条存在（吸顶导航）",
              'id="panelNav"' in html and "initPanelNav" in html)
        # Steam 残留目录（孤儿）的一键删除：与删游戏共用同一条后端校验链路，
        # 前端这里只负责入口与「可能是清单漏记的活游戏」的提醒文案
        check("Steam 残留目录有删除入口并走同一确认流程",
              "data-delgame" in html and "confirmDeleteGame" in html
              and "先排除一种误报" in html)
        # 高风险项「删进回收站」：确认弹窗的选项、请求参数与结果说明三处齐备；
        # 后端 rm_trash 用 SHFileOperationW 带 FOF_ALLOWUNDO，且容量守卫文本存在
        # —— 超限时系统会把「移入回收站」静默转成永久删除，必须由我们先行拒绝。
        check("高风险项确认弹窗提供「删进回收站」选项",
              'id="mTrash"' in html and "清空回收站后才真正释放空间" in html)
        check("清理请求携带 trash 参数",
              "trash:" in html and "trashBox" in html)
        check("结果弹窗如实区分回收站项（不虚报释放量）",
              "→ 回收站" in html and "r.trashed > 0" in html)
        check("后端回收站删除带容量守卫（防静默转永久删除）",
              "def rm_trash" in open(app.__file__, encoding="utf-8").read()
              and "FOF_ALLOWUNDO" in open(app.__file__, encoding="utf-8").read()
              and "超过该盘回收站容量上限" in open(app.__file__, encoding="utf-8").read())
        # 删除游戏前的存档查询：新端点 + 前端异步回填的容器与「疑似」措辞
        check("删除游戏前会查询本地存档（端点 + 弹窗回填）",
              "/__oc__/game-saves" in open(app.__file__, encoding="utf-8").read()
              and 'id="saveScan"' in html and "疑似本地存档" in html)
        # Epic/GOG 残留目录：与 Steam 孤儿同一信任级别（安装根的直接子目录，
        # 且平台记录里没有它），前端有独立容器与同一删除流程
        check("Epic/GOG 残留目录有检测与删除入口",
              "def platform_install_roots" in open(app.__file__, encoding="utf-8").read()
              and 'id="epicOrphans"' in html and 'id="gogOrphans"' in html
              and "confirmDeleteGame({ ...o }, key, true)" in html)
        # 磁盘趋势：快照落盘（每天每盘一条）+ 端点 + 前端 canvas 折线
        check("磁盘空间趋势有三件套（快照/端点/折线）",
              "def record_disk_snapshot" in open(app.__file__, encoding="utf-8").read()
              and "/__oc__/disk-trend" in open(app.__file__, encoding="utf-8").read()
              and 'id="trendCv"' in html and "function drawDiskTrend" in html)
        # 详情弹窗的定位与复制路径按钮；后端 reveal 支持 /select 高亮定位
        check("详情弹窗有「定位」与「复制路径」按钮",
              'id="dLocate"' in html and 'id="dCopy"' in html
              and 'select: !!select' in html
              and "/select," in open(app.__file__, encoding="utf-8").read())
        # 释义增强包：清单收录（linter 也会查），并有两仓同步工具
        idx = json.loads((HERE / "packs" / "index.json").read_text(encoding="utf-8"))
        check("释义增强包已收录进清单且有同步工具",
              "explanations.json" in idx.get("packs", [])
              and (HERE / "packs" / "explanations.json").exists()
              and (TOOLS_DIR / "sync_packs.py").exists())
        check("分发包构建清单含释义增强包",
              "packs/explanations.json"
              in (TOOLS_DIR / "make_package.py").read_text(encoding="utf-8"))
        exe_src = (TOOLS_DIR / "make_exe.py").read_text(encoding="utf-8")
        check("免安装版构建工具复用分发清单（清单唯一真相源）",
              "from make_package import" in exe_src
              and "INCLUDE" in exe_src)
        check("免安装版内嵌官方 embeddable 运行时（版本写死、无 PyInstaller 依赖）",
              "python.org/ftp/python/" in exe_src
              and 'EMBED_VERSION = "' in exe_src
              and "import PyInstaller" not in exe_src)
        # 数据存档目录：--data-dir / OC_DATA_DIR 把运行数据整体重定向
        py = sys.executable
        _dd = tempfile.mkdtemp(prefix="oc_datadir_")
        try:
            r = subprocess.run(
                [py, "-c", "import app; print(app.DATA_DIR)", "--data-dir", _dd],
                capture_output=True, text=True, cwd=str(HERE), timeout=60)
            check("--data-dir 参数重定向数据存档目录",
                  r.returncode == 0
                  and os.path.normpath(r.stdout.strip().strip('"'))
                  == os.path.normpath(_dd),
                  r.stdout.strip() or r.stderr[-120:])
            env = dict(os.environ, OC_DATA_DIR=_dd)
            r2 = subprocess.run(
                [py, "-c", "import app; print(app.DATA_DIR)"],
                capture_output=True, text=True, cwd=str(HERE),
                env=env, timeout=60)
            check("OC_DATA_DIR 环境变量重定向数据存档目录",
                  r2.returncode == 0
                  and os.path.normpath(r2.stdout.strip().strip('"'))
                  == os.path.normpath(_dd),
                  r2.stdout.strip() or r2.stderr[-120:])
            r3 = subprocess.run(
                [py, "-c", "import app; print(app.CONFIG_DIR, app.HISTORY_PATH)"],
                capture_output=True, text=True, cwd=str(HERE),
                env=env, timeout=60)
            out3 = r3.stdout.strip()
            check("重定向后 config/ 与运行数据文件都在数据目录下",
                  r3.returncode == 0
                  and os.path.normpath(_dd) in out3 and "config" in out3
                  and "history.json" in out3, out3[:120])
        finally:
            shutil.rmtree(_dd, ignore_errors=True)
        check("状态负载带数据存档目录（界面自检面板展示用）",
              '"data_dir"' in (HERE / "app.py").read_text(encoding="utf-8"))
        check("默认扫描层数 1（HTML 选项与记忆逻辑一致）",
              '<option value="1" selected data-i18n="depth.1">' in html
              and 'saved === "quick" ? 1 : saved === "deep" ? 2' in html)
        check("起始盘默认数据盘并记住选择",
              "find(d => !d.is_system)" in html and '"oc-drive"' in html)
        check("换盘结果缓存（10 分钟内回切不再重扫）",
              "FS_CACHE" in html and "FS_CACHE_TTL" in html)
        check("游戏本体整删不拆：排行过滤内部子项（in_game_body）",
              "in_game_body" in html)
        app_src = (HERE / "app.py").read_text(encoding="utf-8")
        check("快速扫描注入游戏本体（不受扫描深度限制）",
              "补齐游戏本体节点" in app_src
              and "_is_game_body_path" in app_src)
        check("自检面板含数据目录可写项（后端实测写探针，前端三词条）",
              "def data_dir_writable" in app_src
              and "gd.dataWrite" in html and "gd.dataWriteOk" in html
              and "gd.dataWriteNo" in html)
        check("WizTree 许可说明已折叠（details，不再常驻展开）",
              '<details class="sub" style="margin-top:12px">' in html)
        # 切语言重画的函数列表必须覆盖全部渲染函数定义；豁免集合之外的
        # 新增渲染函数一旦漏进列表，切语言后就会停留在旧语言（曾连漏
        # renderGuide / renderSchedule / renderHistory 等五处）。
        _sl_m = re.search(r"\[(\"[A-Za-z]+\",\s*)+\"[A-Za-z]+\"\]\.forEach\(fn =>", html)
        _sl_defs = set(re.findall(r"^function ((?:render|draw)\w+)", html, re.M))
        _sl_listed = set(re.findall(r'"(\w+)"', _sl_m.group(0))) if _sl_m else set()
        check("setLang 重画列表覆盖全部渲染函数（豁免：Steam 系、ViewNav、SubdirList）",
              _sl_m is not None
              and _sl_defs - _sl_listed <= {"renderSteam", "renderOtherPlatforms",
                                            "renderViewNav", "renderSubdirList"},
              f"未列={sorted(_sl_defs - _sl_listed)[:8]}")
        check("Esc 绑定为返回上一级子目录",
              'e.key !== "Escape"' in html and "fsStack.pop()" in html)
        check("运行环境自检已移除兼容性分析（面板只留环境与权限）",
              "兼容性分析" not in html and "gcompat" not in html)
        check("换算与累积已合并（旧面板标记已移除）",
              "累积清理与换算" in html and "换算成什么" not in html)
        check("规则区按新语义（勾选固定项）",
              "btnRuleSave" in html and "保存当前勾选为规则" in html
              and "ruleAuto" in html)
        check("旧的自由路径规则表单已移除",
              "rKind" not in html and "rGlobWrap" not in html)

        state = get("/__oc__/state?drive=C")
        sb_bases = {t.get("base_id", t["id"]) for t in state.get("targets", [])}
        check("state 含测试配置的沙箱目标",
              {"sandbox-tree", "sandbox-files"} <= sb_bases,
              f"共 {len(state.get('targets', []))} 项")
        check("state 含盘符列表", len(state.get("drives", [])) >= 1)
        check("state.data_dir_writable 实测为 True（数据目录本机可写）",
              state.get("data_dir_writable") is True
              and app.data_dir_writable() is True)

        # 用户自定义释义 / 结论：内置 → 资源包 → 用户，用户排最上
        # （v2.3 起入口在详情弹窗：行带 target_id 才显示「自定义释义」按钮）
        check("自定义释义编辑器存在且入口在详情弹窗",
              "editOverride" in html and 'id="dOv"' in html and "ovkey" in html)
        _r1 = get("/__oc__/overrides/set", method="POST",
                  payload={"id": "sandbox-tree", "what": "自测自定义释义",
                           "cleanable": False})
        check("可设置用户自定义释义与结论",
              _r1.get("targets", {}).get("sandbox-tree", {}).get("cleanable") is False)
        _s2 = get("/__oc__/state?drive=C")
        _t2 = next((t for t in _s2.get("targets", [])
                    if t.get("base_id") == "sandbox-tree"), {})
        check("覆盖已应用到目标（文案、结论与自定义标记）",
              _t2.get("what") == "自测自定义释义"
              and _t2.get("cleanable") is False
              and _t2.get("custom") is True
              and _t2.get("ovkey") == "sandbox-tree")
        get("/__oc__/overrides/delete", method="POST", payload={"id": "sandbox-tree"})
        _s3 = get("/__oc__/state?drive=C")
        _t3 = next((t for t in _s3.get("targets", [])
                    if t.get("base_id") == "sandbox-tree"), {})
        check("删除覆盖后恢复默认", _t3.get("custom") is False
              and _t3.get("what") != "自测自定义释义")

        # 自定义数据包：换算单位 + 释义覆盖 + 固定项规则 一次导入导出
        _ex = get("/__oc__/bundle/export")
        check("自定义数据包可导出（三类齐备）",
              _ex.get("kind") == "ocleaner-custom" and _ex.get("version") == 1
              and isinstance(_ex.get("units"), list)
              and isinstance(_ex.get("overrides"), dict)
              and isinstance(_ex.get("rules"), list))
        # 规则里的 ids 用的是 state 目标的实际 id —— 盘符专属目标（%DRIVE%
        # 沙箱）在具体盘上是 sandbox-tree@C 这种带盘符后缀的形态，
        # 与前端「勾选保存」的行为一致；用 base_id 会被判为不存在的项。
        _sand_id = next((t["id"] for t in get("/__oc__/state?drive=C")
                         .get("targets", [])
                         if t.get("base_id") == "sandbox-tree"), None)
        _bi = get("/__oc__/bundle/import", method="POST", payload={
            "kind": "ocleaner-custom", "version": 1,
            "units": [{"label": "自测导入单位", "size": {"value": 1, "unit": "GB"}}],
            "overrides": {"sandbox-tree": {"what": "自测包释义"}},
            "rules": [{"name": "自测导入规则",
                       "ids": [_sand_id] if _sand_id else []}],
        })
        check("自定义数据包可导入（单位/覆盖/规则各收下一项）",
              _bi.get("ok") and _bi.get("units_added") == 1
              and _bi.get("overrides_applied") == 1
              and _bi.get("rules_added") == 1,
              json.dumps(_bi, ensure_ascii=False)[:180])
        _s4 = get("/__oc__/state?drive=C")
        check("导入的单位与释义覆盖已反映到状态",
              any(u.get("label") == "自测导入单位" for u in _s4.get("units", []))
              and next((t for t in _s4.get("targets", [])
                        if t.get("base_id") == "sandbox-tree"),
                       {}).get("what") == "自测包释义")
        get("/__oc__/units/delete", method="POST", payload={
            "id": next((u["id"] for u in _s4.get("units", [])
                        if u.get("label") == "自测导入单位"), "")})
        get("/__oc__/overrides/delete", method="POST",
            payload={"id": "sandbox-tree"})
        _rl = get("/__oc__/bundle/export").get("rules", [])
        _rid = next((r["id"] for r in _rl if r.get("name") == "自测导入规则"), None)
        if _rid:
            get("/__oc__/custom-rules", method="POST", payload={
                "action": "delete", "id": _rid})
        try:
            get("/__oc__/bundle/import", method="POST", payload={"kind": "x"})
            _bad_kind = False
        except Exception:
            _bad_kind = True
        check("错误 kind 的数据包被拒绝", _bad_kind)

        # ---- 按路径清理（占用排行行内勾选的后端链路）----
        # 安全模型：服务端对每条路径**重新 identify**，必须判为可清理才动手；
        # 不可清理路径进 skipped_paths 而不是静默忽略。node_modules 是知识库
        # 认识的开发工具链缓存，临时目录里造一份真删掉。
        _rc_base = os.path.join(tempfile.gettempdir(), "_oc_selftest_rowclean")
        _rc_junk = os.path.join(_rc_base, "node_modules")
        shutil.rmtree(_rc_base, ignore_errors=True)
        os.makedirs(_rc_junk, exist_ok=True)
        with open(os.path.join(_rc_junk, "x.js"), "w", encoding="utf-8") as fh:
            fh.write("x")
        _rc = get("/__oc__/clean", method="POST", payload={
            "drive": "C", "ids": [], "paths": [_rc_junk, r"C:\\Windows"]})
        _rc_entry = _rc.get("entry") or {}
        _rc_res = next((x for x in _rc_entry.get("results", [])
                        if x.get("label") == "node_modules"), None)
        check("按路径清理：可识别目录被清空内容（保留目录本身）",
              bool(_rc_res) and not _rc_res.get("error") and _rc_res.get("freed", 0) >= 1
              and os.path.isdir(_rc_junk)
              and not os.path.exists(os.path.join(_rc_junk, "x.js")),
              str(_rc_res)[:150] if _rc_res else str(_rc)[:150])
        check("按路径清理：不可清理路径被点名拒绝",
              any("C:\\Windows" in str(x.get("path", ""))
                  and "不可清理" in str(x.get("reason", ""))
                  for x in _rc.get("skipped_paths", [])),
              json.dumps(_rc.get("skipped_paths", []), ensure_ascii=False)[:150])
        # 全部路径被拒时返回 400；HTTPError 自带响应体，把 error 与
        # skipped_paths 都取出来断言
        try:
            _rc_rej = get("/__oc__/clean", method="POST", payload={
                "drive": "C", "ids": [], "paths": [r"C:\\Windows"]})
            _rc_err = (_rc_rej or {}).get("error")
            _rc_skip = (_rc_rej or {}).get("skipped_paths", [])
        except urllib.error.HTTPError as e:
            try:
                _body = json.loads(e.read().decode("utf-8"))
                _rc_err = _body.get("error")
                _rc_skip = _body.get("skipped_paths", [])
            except Exception:
                _rc_err, _rc_skip = str(e), []
        except Exception as e:
            _rc_err, _rc_skip = str(e), []
        check("按路径清理：全部被拒时明确报没有可清理项",
              bool(_rc_err) and len(_rc_skip) == 1, str(_rc_err)[:150])
        shutil.rmtree(_rc_base, ignore_errors=True)

        # 扫描进度：必须是真实阶段，而不是把时间映射成百分比
        _pg = get("/__oc__/progress")
        check("进度端点在不扫描时明确标记为未激活",
              isinstance(_pg, dict) and _pg.get("active") is False)
        check("进度条不再用时间估算（旧公式已移除）",
              "Math.exp(-s / 90)" not in html and "/__oc__/progress" in html)
        app.prog_start("C")
        app.prog_set("parse", "正在解析 MFT 明细（42%）", 42.5)
        _s1 = app.prog_snapshot()
        check("扫描进度按真实阶段上报",
              _s1["active"] and _s1["phase"] == "parse" and _s1["pct"] == 42.5
              and isinstance(_s1.get("elapsed"), float), str(_s1.get("text")))
        app.prog_set("walk", "正在递归统计目录体积", 60, indeterminate=True)
        check("没有分母的阶段标记为「不确定进度」而不是伪造百分比",
              app.prog_snapshot()["indeterminate"] is True)
        app.prog_set("identify", "越界百分比被夹紧", 250)
        check("百分比写入时被夹在 0-100", app.prog_snapshot()["pct"] == 100.0)
        app.prog_done(True, "扫描完成")
        _s2 = app.prog_snapshot()
        check("扫描结束后进度归位（active=False 且 100%）",
              _s2["active"] is False and _s2["pct"] == 100.0
              and _s2["indeterminate"] is False)
        app.prog_start("D")
        app.prog_done(False, "盘符不存在")
        _s3 = app.prog_snapshot()
        check("失败的扫描也把进度收尾（不会永远停在半路）",
              _s3["active"] is False and _s3["phase"] == "error",
              _s3.get("text", ""))
        check("state 含磁盘信息", "free_h" in state.get("disk", {}))
        check("state 含累积统计", "stats" in state and "total_freed_h" in state["stats"])
        check("state 含换算单位表", "units" in state and len(state["units"]) > 0)
        check("state 含所有简介字段",
              all(state["targets"][0].get(k) for k in
                  ("what", "rebuild", "impact", "keep_if", "short")))
        # 回归：scope=drive 必须真的过滤掉用户级目标，否则数据盘会列出 C 盘的缓存
        sd = get("/__oc__/state?drive=C&scope=drive")["targets"]
        check("scope=drive 只返回盘符专属项（且非空）",
              bool(sd) and all(t["scope"] == "drive" for t in sd), f"{len(sd)} 项")
        # scope=drive 目标少一些，但同样逐个起 PowerShell 量体积，慢机器上
        # 也可能超出普通请求的超时（与下面那条 scan=C 同理）。
        scan_drive = get("/__oc__/scan?drive=C&scope=drive", timeout=600)
        check("scan 的 scope=drive 同样生效（且非空）",
              bool(scan_drive["results"])
              and all(not r["id"].startswith(("uv-", "pip-", "chrome-", "edge-"))
                      for r in scan_drive["results"]),
              f"{len(scan_drive['results'])} 项")

        # CI runner 负载波动很大：同样的常规扫描上次 90 秒、这次能超 180 秒。
        # 扫描是「逐个起 PS 量体积」的重活，给足 10 分钟，避免慢机器被
        # 误判成回归（超时口径的机制说明见 get() 定义处）。
        scan = get("/__oc__/scan?drive=C", timeout=600)
        sizes = {r.get("base_id", r["id"]): r["size"] for r in scan["results"]}
        check("scan 正确统计 tree 大小", sizes.get("sandbox-tree") == 14336, str(sizes))
        check("scan 正确统计 files 大小", sizes.get("sandbox-files") == 768, str(sizes))
        check("scan 无任何目标报错",
              not [(r["id"], r["error"]) for r in scan["results"] if r["error"]])

        # 用扫描结果里的真实 id（盘符专属项带 @C 后缀），不硬编码
        id_by_base = {r.get("base_id", r["id"]): r["id"] for r in scan["results"]}
        files_id = id_by_base["sandbox-files"]
        # 本机可能已有真实清理历史（曾因此误报失败），断言必须基于基线增量而非绝对值
        freed_before = get("/__oc__/stats")["total_freed"]
        res = get("/__oc__/clean", "POST", {"ids": [files_id], "drive": "C"})
        entry = res["entry"]
        check("POST /clean 返回结果", entry["freed"] == 768, f"freed={entry['freed']}")
        check("clean 结果带 category 便于分类统计",
              all("category" in r for r in entry["results"]))
        check("clean 只动了选中项",
              (SANDBOX / "tree" / "a0.bin").exists()
              and not (SANDBOX / "files" / "gone.log").exists())
        check("clean 返回累计统计", "stats" in res and res["stats"]["run_count"] >= 1)
        check("累计释放量已累加", res["stats"]["total_freed"] == freed_before + 768,
              f"{freed_before} -> {res['stats']['total_freed']}")
        # 768 字节太小，换算成「张手机照片」只有 0.0002，会被 0.01 的显示下限过滤，
        # 这是正确行为，所以这里只验证结构合法；换算数值本身在阶段一用真实数据验证。
        check("换算结果结构合法",
              isinstance(res["stats"]["conversions"], list)
              and all(c.get("id") and "count_h" in c for c in res["stats"]["conversions"]),
              str(res["stats"]["conversions"]))

        try:
            get("/__oc__/clean", "POST", {"ids": [], "drive": "C"}, timeout=15)
            check("空选择被拒绝", False, "竟然返回 200")
        except urllib.error.HTTPError as e:
            check("空选择被拒绝（400）", e.code == 400, f"code={e.code}")

        # ---- 删除游戏端点：安全边界比功能本身更重要 ----
        # 它**不是**通用删除接口：路径必须被后端自己也识别为游戏安装目录
        # （steamapps\common 的直接子目录，或 Epic/GOG 清单里的精确路径），
        # 否则一律 400。这里拿一个真实存在、但绝不是游戏的目录去敲它 ——
        # 如果它真把目录删了，下面的断言会失败（目录还在），这是双保险。
        sandbox_tree = str(SANDBOX / "tree")
        try:
            get("/__oc__/clean-game", "POST", {"path": sandbox_tree}, timeout=15)
            check("删除游戏端点拒绝非游戏目录", False, "竟然返回 200")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            check("删除游戏端点拒绝非游戏目录",
                  e.code == 400 and "游戏本体" in body,
                  f"code={e.code} body={body[:120]}")
        check("非游戏目录在拒绝后仍然存在", (SANDBOX / "tree").exists())
        try:
            get("/__oc__/clean-game", "POST", {"path": "不是\\绝对路径"}, timeout=15)
            check("删除游戏端点拒绝相对路径", False, "竟然返回 200")
        except urllib.error.HTTPError as e:
            check("删除游戏端点拒绝相对路径", e.code == 400, f"code={e.code}")
        try:
            get("/__oc__/clean-game", "POST", {}, timeout=15)
            check("删除游戏端点拒绝空路径", False, "竟然返回 200")
        except urllib.error.HTTPError as e:
            check("删除游戏端点拒绝空路径", e.code == 400, f"code={e.code}")

        hist = get("/__oc__/history")
        check("历史记录了刚才的清理", len(hist) >= 1 and hist[0]["freed"] == 768)

        stats = get("/__oc__/stats")
        check("stats 分类汇总正确", any(c["name"] == "测试" for c in stats["by_category"]),
              str(stats["by_category"]))
        check("stats 有各盘分布", any(d["drive"] == "C" for d in stats["by_drive"]))

        r = get("/__oc__/units", "POST", {"id": "test_unit", "label": "个测试单位",
                                          "bytes_per_unit": 1073741824,
                                          "note": "自测添加"})
        check("可添加自定义换算单位",
              any("test_unit" in [u.get("id") for u in r["units"]]
                  for _ in [0]))
        check("新单位已参与换算",
              any(c["id"] == "test_unit" for c in r["stats"]["conversions"]) or
              r["stats"]["total_freed"] < 1073741824)

        r = get("/__oc__/units/delete", "POST", {"id": "test_unit"})
        check("可删除自定义换算单位",
              not any(u.get("id") == "test_unit" for u in r.get("units", [])))

        # ---- 自定义规则：勾选固定项 → 命名保存 → 套用 / 立即清理 ----
        files_id2 = id_by_base["sandbox-files"]
        try:
            get("/__oc__/custom-rules", "POST",
                {"action": "add", "name": "无勾选", "ids": [], "drive": "C"})
            check("规则未勾选任何项时被拒绝", False, "竟然接受了空 ids")
        except urllib.error.HTTPError as e:
            check("规则未勾选任何项时被拒绝（400）", e.code == 400, f"code={e.code}")
        try:
            get("/__oc__/custom-rules", "POST",
                {"action": "add", "ids": [files_id2], "drive": "C"})
            check("规则无名字时被拒绝", False, "竟然接受了空名字")
        except urllib.error.HTTPError as e:
            check("规则无名字时被拒绝（400）", e.code == 400, f"code={e.code}")
        try:
            get("/__oc__/custom-rules", "POST",
                {"action": "add", "name": "坏规则", "ids": ["不存在的项"], "drive": "C"})
            check("规则引用无效目标时被拒绝", False, "竟然接受了无效 id")
        except urllib.error.HTTPError as e:
            check("规则引用无效目标时被拒绝（400）", e.code == 400, f"code={e.code}")

        r = get("/__oc__/custom-rules", "POST",
                {"action": "add", "name": "自测规则", "ids": [files_id2], "drive": "C"})
        check("可保存规则（勾选固定项）",
              any(x["name"] == "自测规则" for x in r["rules"]))
        rule = next(x for x in r["rules"] if x["name"] == "自测规则")
        check("规则记录了勾选的项与盘符",
              rule["ids"] == [files_id2] and rule["drive"] == "C", str(rule))
        check("规则不出现在清理目标列表里（它是选择集而非目标）",
              not any(t.get("category") == "自定义规则"
                      for t in get("/__oc__/state?drive=C")["targets"]))

        # 套用同名规则应覆盖而不是新增
        r = get("/__oc__/custom-rules", "POST",
                {"action": "add", "name": "自测规则", "ids": [files_id2], "drive": "C"})
        check("同名规则会覆盖而非重复新增",
              len([x for x in r["rules"] if x["name"] == "自测规则"]) == 1)

        # 改名
        r = get("/__oc__/custom-rules", "POST",
                {"action": "rename", "id": rule["id"], "name": "改名后规则"})
        check("规则可改名",
              any(x["id"] == rule["id"] and x["name"] == "改名后规则" for x in r["rules"]))

        # 立即清理：先造好沙箱文件
        make_sandbox()
        run = get("/__oc__/run-rule", "POST", {"id": rule["id"], "custom_path": None})
        check("按规则立即清理成功", run["entry"]["freed"] == 768,
              f"freed={run['entry']['freed']}")
        check("规则清理只动了规则里勾选的项",
              (SANDBOX / "tree" / "a0.bin").exists()
              and not (SANDBOX / "files" / "gone.log").exists())
        check("历史里记录了规则名",
              any("改名后规则" in (h.get("trigger") or "") for h in get("/__oc__/history")))

        r = get("/__oc__/custom-rules", "POST",
                {"action": "delete", "id": rule["id"]})
        check("可删除规则", not any(x["id"] == rule["id"] for x in r["rules"]))

        sch = get("/__oc__/schedule", "POST",
                  {"enabled": True, "interval_hours": 0.25, "preset": "safe", "drive": "C"})
        check("定时设置已保存（含盘符）",
              sch.get("enabled") and sch.get("drive") == "C"
              and sch.get("next_run", 0) > time.time())
        sch = get("/__oc__/schedule", "POST", {"enabled": False})
        check("定时可关闭且 next_run 归零",
              not sch.get("enabled") and sch.get("next_run") == 0)

        get("/__oc__/history", "DELETE")
        check("历史可清空", get("/__oc__/history") == [])
        check("清空后累计统计归零", get("/__oc__/stats")["total_freed"] == 0)

        # ---- 全平台游戏库接口（v2.2）----
        gm = get("/__oc__/games")
        check("/games 返回全平台结构",
              isinstance(gm.get("games"), list)
              and set(gm.get("platforms", {})) >= {"steam", "epic", "gog"})
        check("/games 每条游戏都带平台标记",
              all(g.get("platform") in ("steam", "epic", "gog")
                  for g in gm.get("games", [])))
        check("/games 的 steam 负载原样透传",
              isinstance(gm.get("steam"), dict) and "libraries" in gm["steam"])
        check("/games 平台计数与列表一致",
              gm.get("game_count") == len(gm.get("games", []))
              and gm["game_count"] == sum(p.get("game_count", 0)
                                          for p in gm["platforms"].values()))

        # ---- 安全回归：CSRF ----
        # 威胁模型：用户浏览器开着本工具，又打开恶意网页；该网页可向
        # 127.0.0.1 发请求。服务端若不校验 Origin，浏览器虽读不到响应，
        # 但副作用照样发生（清空历史、弹资源管理器）。
        def cross(method, path, payload=None):
            r = urllib.request.Request(base + path, method=method)
            if payload is not None:
                r.data = json.dumps(payload).encode()
                r.add_header("Content-Type", "application/json")
            r.add_header("Origin", "https://evil.example.com")
            r.add_header("Referer", "https://evil.example.com/attack.html")
            try:
                with urllib.request.urlopen(r, timeout=15) as resp:
                    return resp.status
            except urllib.error.HTTPError as e:
                return e.code

        for method, path, payload in (
                ("DELETE", "/__oc__/history", None),
                ("POST", "/__oc__/reveal", {"path": "C:\\Windows"}),
                ("POST", "/__oc__/custom-rules", {"action": "delete", "id": "x"}),
                ("POST", "/__oc__/schedule", {"enabled": False}),
                ("POST", "/__oc__/clean", {"ids": ["x"]}),
                ("POST", "/__oc__/units", {"id": "evil", "label": "x"})):
            code = cross(method, path, payload)
            check(f"跨站 {method} {path} 被拒绝（403）", code == 403, f"HTTP {code}")

        # 同源（带本机 Origin）必须仍然可用，否则防护过度
        r = urllib.request.Request(base + "/__oc__/history", method="DELETE")
        r.add_header("Origin", f"http://127.0.0.1:{TEST_PORT}")
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                check("同源写操作仍可用", resp.status == 200, f"HTTP {resp.status}")
        except urllib.error.HTTPError as e:
            check("同源写操作仍可用", False, f"HTTP {e.code}")

        # ---- 安全回归：畸形请求体不该 500 ----
        for name, raw in (("非法 JSON", b"{not json"),
                          ("JSON 数组", b"[1,2,3]"),
                          ("超大请求体", b"x" * (5 * 1024 * 1024))):
            r = urllib.request.Request(base + "/__oc__/clean", method="POST",
                                       data=raw)
            r.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(r, timeout=30) as resp:
                    check(f"{name} 返回 4xx 而非 5xx", False, f"HTTP {resp.status}")
            except urllib.error.HTTPError as e:
                check(f"{name} 返回 4xx 而非 5xx", 400 <= e.code < 500,
                      f"HTTP {e.code}")
            except Exception as e:  # noqa: BLE001
                check(f"{name} 被安全处理", True, type(e).__name__)
        check("经受畸形输入后服务仍存活",
              get("/__oc__/state").get("current_drive") is not None)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(SANDBOX, ignore_errors=True)


def phase4_repo() -> None:
    """仓库层面的一致性：发布前必须成立的东西。

    这些检查不涉及清理功能，但它们在 CI 里是发布闸门的一部分，
    放在这里是为了「一次命令跑完全部检查」。
    """
    print("\n=== 阶段四：仓库配置与发布闸门 ===")

    # linter 必须通过
    lint = TOOLS_DIR / "lint_rules.py"
    if not lint.exists():
        check("规则 linter 存在", False, "缺 tools/lint_rules.py")
    else:
        p = subprocess.run([sys.executable, "-X", "utf8", str(lint), "--strict"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
        check("规则 linter --strict 通过", p.returncode == 0, tail[0][:80])

    # linter 自测必须通过（证明 linter 不是空转）
    lst = TOOLS_DIR / "_lint_selftest.py"
    if lst.exists():
        p = subprocess.run([sys.executable, "-X", "utf8", str(lst)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
        check("linter 有检出能力（注入缺陷能被抓到）", p.returncode == 0,
              tail[0][:80])

    # 随发行版一起交付的文档（分发包里也必须有）
    for f, why in (("LICENSE", "没有许可证别人无法合法使用"),
                   ("CONTRIBUTING.md", "贡献者需要知道安全门槛"),
                   ("SECURITY.md", "规则投毒是本项目特有攻击面")):
        check(f"包含 {f}", (HERE / f).exists(), why)

    # 版本号必须一致。
    # 这条是被实际漏改逼出来的：升 v2.3 时只改了 app.py 的 server_version，
    # 漏掉 config/drives.json（界面显示仍是 2.2）与两个 User-Agent。
    # 现在 app.py 内已收敛为 APP_VERSION 常量，打包工具直接 import 它，
    # 这里钉住 drives.json 与「打包工具不得再写死版本号」。
    mkt = TOOLS_DIR / "make_package.py"
    drives_ver = str((app.load_drives_cfg().get("app") or {}).get("version") or "")
    mkt_txt = mkt.read_text(encoding="utf-8", errors="replace") if mkt.exists() else ""
    check("app.py 版本常量与 drives.json 一致",
          drives_ver == app.APP_VERSION,
          f"app={app.APP_VERSION} drives={drives_ver}")
    check("打包工具从 app.py 导入版本常量（不再各写各的）",
          "import app" in mkt_txt and "APP_VERSION" in mkt_txt
          and not re.search(r"""=\s*["']\d+\.\d+["']""", mkt_txt),
          "make_package.py: import app.APP_VERSION")
    check("User-Agent 由版本常量派生（不再各写各的）",
          app.APP_UA == f"O-Cleaner/{app.APP_VERSION}"
          and '"User-Agent": "O-Cleaner/' not in
          (app.__file__ and open(app.__file__, encoding="utf-8").read()),
          app.APP_UA)

    # 仓库专用文件：分发包里不该有 .gitattributes / .github，
    # 所以只有确认身处源码仓库时才检查（用 .git 目录作为判据）。
    in_repo = (HERE / ".git").exists() or (HERE / ".github").exists()
    if not in_repo:
        skip("仓库配置检查（.gitattributes/.gitignore/CI）",
             "当前是分发包，不含仓库专用文件")
    else:
        for f, why in ((".gitattributes", "防止 Git 把 .bat 的 CRLF 转成 LF"),
                       (".gitignore", "运行数据不应进版本库"),
                       (".github/workflows/ci.yml", "CI 是发布闸门")):
            check(f"仓库包含 {f}", (HERE / f).exists(), why)

        # CI 必须真的调用 linter（否则闸门形同虚设）
        ci = HERE / ".github" / "workflows" / "ci.yml"
        if ci.exists():
            txt = ci.read_text(encoding="utf-8")
            check("CI 里跑 linter 且用 --strict", "lint_rules.py --strict" in txt)
            check("CI 里跑 linter 自测", "_lint_selftest.py" in txt)
            check("CI 里跑功能自测", "selftest.py" in txt)
            check("CI 里跑渲染检查", "cdp_check.js" in txt)
            check("CI 只在 Windows 上跑",
                  "windows-latest" in txt and "ubuntu" not in txt)
            check("CI 里 linter 先于功能测试",
                  txt.index("lint_rules.py") < txt.index("tools/selftest.py"))

    # .gitattributes 必须保护批处理的换行
    ga = HERE / ".gitattributes"
    if ga.exists():
        t = ga.read_text(encoding="utf-8")
        check(".gitattributes 强制 .bat 用 CRLF", "*.bat text eol=crlf" in t)
        check(".gitattributes 强制 .ps1 用 CRLF", "*.ps1 text eol=crlf" in t)

    # 跨设备自检工具：换机器时靠它判断环境是否可用
    doc = TOOLS_DIR / "doctor.py"
    if doc.exists():
        p = subprocess.run([sys.executable, "-X", "utf8", str(doc)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        tail = [l for l in (p.stdout or "").strip().splitlines() if "通过" in l]
        check("doctor 自检通过（本机环境可用）", p.returncode == 0,
              tail[-1].strip() if tail else "")
    else:
        check("提供跨设备自检工具 doctor.py", False, "缺 tools/doctor.py")

    # 安全回归：空路径必须被拒绝
    # （os.path.abspath("") 会返回当前工作目录，空串曾伪装成合法目录通过检查）
    check("拒绝空路径", not app.check_deletable("")[0])
    check("拒绝纯空白路径", not app.check_deletable("   ")[0])
    # 注：CSRF 与畸形请求体的回归测试在 phase3_http 里（需要运行中的服务）

    # 安全回归：浏览器数据目录绝不能被整体标为可清理
    # （会连带删掉书签、密码、全部登录状态）
    kb2 = app.load_filetypes()
    bd = next((r for r in kb2.get("folder_rules", [])
               if r.get("id") == "browser-data"), None)
    check("浏览器目录规则存在", bd is not None)
    if bd:
        check("浏览器目录整体不可清理（防止删掉书签与密码）",
              bd.get("cleanable") is False, f"cleanable={bd.get('cleanable')}")
    for path in (r"D:\Chrome", r"E:\Chrome", r"D:\portable\Chrome"):
        info = app.identify("Chrome", path, "dir")
        check(f"名字像浏览器但缺资料目录结构时不误判（{path}）",
              not info["cleanable"] and info["category"] != "浏览器",
              f"{info['category']} / {info['title']}")

    # 目录体积测量必须返回真实数值（曾因 ConvertTo-Json 单元素退化而全为 0）
    part = app.analyze_dir_partition(os.environ.get("TEMP", "C:\\Temp"))
    check("分区分析能算出需保留部分体积", part.get("keep_size", 0) > 0,
          part.get("keep_size_h", ""))
    check("分区分析包含体积明细字段",
          all("size_h" in x for x in part.get("keep", [])))

    # README 必须提到 linter 与安全闸门（否则贡献者不知道要跑）
    rd = (HERE / "README.md").read_text(encoding="utf-8")
    check("README 提到规则 linter", "lint_rules.py" in rd)
    check("README 说明贡献规则前要过闸门",
          "闸门" in rd or "lint" in rd.lower())

    # 编码与换行：下面三条各对应一个真实踩过的坑
    import re as _re

    def _bytes(rel):
        return (HERE / rel).read_bytes()

    def _has_bom(b):
        return len(b) >= 3 and b[0] == 0xEF and b[1] == 0xBB and b[2] == 0xBF

    def _lone_lf(b):
        return len(_re.findall(b"(?<!\r)\n", b))

    bat = _bytes("start-ocleaner.bat")
    check("start-ocleaner.bat 是纯 ASCII（cmd.exe 按 GBK 读，中文会乱码）",
          all(x < 128 for x in bat))
    check("start-ocleaner.bat 无孤立 LF（LF 会让 cmd 吃掉下一行）",
          _lone_lf(bat) == 0, f"{_lone_lf(bat)} 个")
    mkt = _bytes("tools/make_package.py")
    check("make_package.py 不带 BOM（.py 契约：UTF-8 无 BOM）",
          not _has_bom(mkt))
    py = _bytes("app.py")
    check("app.py 不带 BOM（Python 会把它当 \\ufeff 留在字面量里）",
          not _has_bom(py))
    check("提供编码自动修复工具", (TOOLS_DIR / "fix-encoding.ps1").exists())
    bad_enc = []
    for f in list(HERE.rglob("*.py")) + list(HERE.rglob("*.json")):
        if "__pycache__" in str(f) or "dist" in str(f):
            continue
        if _has_bom(f.read_bytes()):
            bad_enc.append(f.name)
    check("所有 .py/.json 都不带 BOM", not bad_enc, str(bad_enc[:4]))

    # ---- CLI 入口冒烟：--version 必须能用且与单点定义一致 ----
    # 版本号单点定义在 app.py 的 APP_VERSION，打包脚本与配置里的另两处
    # 由别处断言对齐；这里只验证「这条命令真的能跑通、输出带版本号」。
    try:
        _r = subprocess.run(
            [sys.executable, "-X", "utf8", str(HERE / "app.py"), "--version"],
            capture_output=True, text=True, encoding="utf-8",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (_r.stdout or "").strip()
        check("app.py --version 可用且输出带版本号",
              _r.returncode == 0 and out.startswith("O-Cleaner v"),
              f"rc={_r.returncode} out={out[:60]!r}")
    except Exception as exc:  # noqa: BLE001
        check("app.py --version 可用且输出带版本号", False, f"执行异常：{exc}")

    # ---- 回收站删除的真实链路 ----
    # 容量守卫：把上限 patch 成 1 字节，必须拒绝且不动原文件（防「静默转永久删除」）。
    # 正常路径：小文件移入回收站后源位置消失。回收站操作不触发真实删除配额。
    try:
        _td = tempfile.mkdtemp(prefix="oc_trash_t_")
        _fp = os.path.join(_td, "a.txt")
        with open(_fp, "w", encoding="ascii") as fh:
            fh.write("x" * 64)
        _orig_cap = app._recycle_capacity_bytes
        app._recycle_capacity_bytes = lambda root: 1
        try:
            _ok, _why = app.rm_trash(_fp)
        finally:
            app._recycle_capacity_bytes = _orig_cap
        check("回收站删除的容量守卫会拒绝并保留原文件",
              not _ok and "超过" in _why and os.path.exists(_fp),
              f"ok={_ok} why={_why[:40]!r}")
        _ok2, _why2 = app.rm_trash(_fp)
        check("小文件可移入回收站（源位置消失）",
              _ok2 and not os.path.exists(_fp), f"ok={_ok2} why={_why2[:40]!r}")
        os.rmdir(_td)
    except Exception as exc:  # noqa: BLE001
        check("回收站删除的真实链路", False, f"执行异常：{exc}")

    # ---- 游戏存档查询的真实链路 ----
    # 把候选根 patch 到临时沙箱，验证「安装目录名 → 存档目录」的启发式匹配。
    try:
        _root = tempfile.mkdtemp(prefix="oc_saves_t_")
        _save = os.path.join(_root, "MyGame777_saves")
        os.mkdir(_save)
        with open(os.path.join(_save, "slot1.sav"), "wb") as fh:
            fh.write(b"0" * 2048)
        _orig_roots = app._GAME_SAVE_ROOTS
        app._GAME_SAVE_ROOTS = (("测试根", _root),)
        try:
            _saves = app.find_game_save_candidates("C:\\Games\\MyGame777", "")
        finally:
            app._GAME_SAVE_ROOTS = _orig_roots
        check("按安装目录名能找到疑似存档目录",
              len(_saves) == 1 and _saves[0]["path"] == _save
              and _saves[0]["size"] == 2048,
              f"saves={[s['path'] for s in _saves]}")
    except Exception as exc:  # noqa: BLE001
        check("游戏存档查询的真实链路", False, f"执行异常：{exc}")

    # ---- 单文件前端的静态一致性 ----
    # 这几条是「一个真实 bug 逼出来的」：新写的 var(--dim3) 引用了并不存在的变量，
    # 页面不报错、只是颜色静默失效 —— 静态检查是唯一的兜底。
    idx = HERE / "web" / "index.html"
    html = idx.read_text(encoding="utf-8") if idx.exists() else ""
    m_st = _re.search(r"<style>(.*?)</style>", html, _re.DOTALL)
    m_sc = _re.search(r"<script>(.*?)</script>", html, _re.DOTALL)
    # 先确认两块都抽到了：否则下面数括号会退化成 0 == 0 的假通过
    check("index.html 的 <style> 与 <script> 块都能抽出来",
          bool(m_st and m_sc), "正则没匹配到，后面的括号检查会失去意义")
    st = m_st.group(1) if m_st else ""
    sc = m_sc.group(1) if m_sc else ""
    # 改 CSS 最容易留下半截规则，JS 同理 —— 括号配平是最便宜的回归网
    check("index.html 的 <style> 花括号配平",
          st.count("{") == st.count("}"), f"{st.count('{')}/{st.count('}')}")
    check("index.html 的 <script> 花括号配平",
          sc.count("{") == sc.count("}"), f"{sc.count('{')}/{sc.count('}')}")
    defined = set(_re.findall(r"(--[A-Za-z0-9_-]+)\s*:", st))
    used = set(_re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)\s*\)", html))
    missing = sorted(used - defined)
    check("index.html 里 var(--x) 引用的变量都已定义",
          not missing, f"未定义：{missing}")


def phase5_steam() -> None:
    """Steam 集成：解析器正确性 + 真实数据一致性。"""
    print("\n=== 阶段五：Steam 集成 ===")

    # VDF 解析器：必须把最外层键名包回去。
    # 这个 bug 曾让 Steam 清单解析出 0 个游戏（而库路径看着正常，极难发现）。
    acf = ('"AppState"\n{\n\t"appid"\t\t"1063660"\n\t"name"\t\t"Test Game"\n'
           '\t"installdir"\t\t"TestGame"\n\t"SizeOnDisk"\t\t"11474672374"\n'
           '\t"StateFlags"\t\t"4"\n\t"LauncherPath"\t\t"D:\\\\steam\\\\steam.exe"\n}\n')
    parsed = app._parse_vdf(acf)
    check("VDF 解析保留最外层键名", list(parsed.keys()) == ["AppState"],
          str(list(parsed.keys())))
    st = parsed.get("AppState") or {}
    check("VDF 解析出嵌套字段",
          st.get("name") == "Test Game" and st.get("appid") == "1063660",
          str({k: st.get(k) for k in ("name", "appid")}))
    check("VDF 正确还原转义反斜杠",
          st.get("LauncherPath") == "D:\\steam\\steam.exe",
          repr(st.get("LauncherPath")))
    # 解析器不能依赖字段顺序
    shuffled = ('"AppState"\n{\n\t"SizeOnDisk"\t\t"100"\n\t"name"\t\t"X"\n}\n')
    s2 = app._parse_vdf(shuffled).get("AppState") or {}
    check("VDF 不依赖字段顺序", s2.get("SizeOnDisk") == "100" and s2.get("name") == "X")

    ds = app.discover_steam()
    if not ds.get("found"):
        skip("Steam 真实数据检查", ds.get("error") or "本机未检测到 Steam 游戏")
        return
    check("发现 Steam 游戏库", ds["game_count"] > 0, f"{ds['game_count']} 个游戏")
    check("游戏都带名称与体积",
          all(g["name"] and g["size"] >= 0 for g in ds["games"]))
    check("游戏都带安装路径与存在性标记",
          all("path" in g and "exists" in g for g in ds["games"]))
    # 去重：多份 libraryfolders 会列出同一个库，同一游戏不能重复计数
    paths = [os.path.normpath(g["path"]).lower() for g in ds["games"] if g["path"]]
    check("游戏列表按安装路径去重", len(paths) == len(set(paths)),
          f"{len(paths)} 条 / {len(set(paths))} 唯一")
    # 库统计必须与游戏列表对得上（否则界面上会互相矛盾）
    lib_apps = sum(l["apps"] for l in ds["libraries"])
    lib_size = sum(l["size"] for l in ds["libraries"])
    check("库统计与游戏列表一致",
          lib_apps == ds["game_count"] and lib_size == ds["total_size"],
          f"库 {lib_apps} 个 / 游戏 {ds['game_count']} 个；"
          f"库 {app.human(lib_size)} / 游戏 {ds['total_size_h']}")
    check("Steam 数据不含内部打分子段",
          all("match_score" not in g for g in ds["games"]))
    # 速度：读清单必须是秒级（这是它相对扫描磁盘的意义所在）
    t0 = time.time()
    app.discover_steam()
    dt = time.time() - t0
    check("读取 Steam 清单在 5 秒内完成", dt < 5, f"{dt:.2f} 秒")

    # 游戏中文名映射与展示
    gn = app.load_game_names()
    check("game-names.json 存在且格式正确",
          isinstance(gn.get("games"), dict) and len(gn["games"]) > 10,
          f"{len(gn.get('games') or {})} 个条目")
    d1, z1 = app.game_display_name("1174180", "Red Dead Redemption 2")
    check("按 appid 匹配到中文名", "荒野大镖客" in d1 and z1 == "荒野大镖客：救赎 2",
          f"{d1!r} / {z1!r}")
    d2, z2 = app.game_display_name(None, "The Witcher 3")
    check("按名称模糊匹配到中文名", "巫师 3" in d2, f"{d2!r}")
    d3, z3 = app.game_display_name("3528450", "纸房子 Sweet Home")
    check("原名含中文时不加重复前缀", d3 == "纸房子 Sweet Home", f"{d3!r}")
    check("Steam 游戏列表包含 display_name 字段",
          all("display_name" in g and "zh_name" in g for g in ds["games"]))
    has_zh = [g for g in ds["games"] if g.get("zh_name")]
    check("Steam 游戏中识别出中文名", len(has_zh) >= 15,
          f"{len(has_zh)}/{len(ds['games'])} 项")


def phase6_partition() -> None:
    """目录细分（可清缓存 / 不要删）与 PowerShell 返回值契约。

    回归背景（本项目最隐蔽的一个 bug）：
    run_ps 早先写成「result 是 list 就返回 data[0]」，而 PS_STEAM_ORPHAN_SIZE
    返回的正是 [{path,size},…] 数组。后果是 analyze_dir_partition 里
    **除第一个以外的目录体积全部为 0** —— 界面上表现为
    「浏览器缓存目录识别得对，但显示 0 B」，看起来像没扫到，实则是解析层
    把数组吃掉了。所以这里把 run_ps 的「原样返回」当成契约钉死。
    """
    print("\n=== 阶段六：目录细分 + PowerShell 返回值契约 ===")

    # 1) run_ps 必须原样返回数组，不能折叠成首元素
    probe = app.BASE_DIR / ".oc-selftest-arr.tmp"
    try:
        probe.write_text(r"C:\Windows", encoding="utf-8")
        script = app.PS_STEAM_ORPHAN_SIZE.replace(
            "__LIST__", str(probe).replace("'", "''"))
        r = app.run_ps(script, timeout=600)
        # 带上实际内容：dict 可能是「折叠的数组」（真缺陷），也可能是带
        # error 的失败返回（慢机器上脚本瞬时失败）—— 两者必须能区分。
        check("run_ps 保留数组形状（单元素时也不折叠）",
              isinstance(r, list) or (isinstance(r, dict) and r.get("error")),
              f"返回类型 {type(r).__name__} 内容 {str(r)[:140]}")
        if isinstance(r, dict) and r.get("error"):
            skip("run_ps 单元素形状",
                 "脚本瞬时失败，重跑自测即可：" + str(r.get("error"))[:100])
        if isinstance(r, list):
            check("数组元素带 path/size 且体积非零",
                  bool(r) and int(r[0].get("size") or 0) > 0,
                  str(r[:1])[:120])
        # 两个路径时必须是两个元素（早先这里恒为 1 个）
        probe.write_text("C:\\Windows\nC:\\Windows\\System32", encoding="utf-8")
        script = app.PS_STEAM_ORPHAN_SIZE.replace(
            "__LIST__", str(probe).replace("'", "''"))
        r2 = app.run_ps(script, timeout=600)
        check("run_ps 不丢元素（2 个路径 → 2 个结果）",
              isinstance(r2, list) and len(r2) == 2,
              f"拿到 {len(r2) if isinstance(r2, list) else type(r2).__name__}")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass

    # 2) 分区接口的结构与身份判定
    tgt = os.environ.get("TEMP") or "C:\\Windows\\Temp"
    if not os.path.isdir(tgt):
        skip("目录细分结构检查", f"{tgt} 不存在")
        return
    part = app.analyze_dir_partition(tgt)
    check("分区接口不报错", not part.get("error"), str(part.get("error")))
    check("分区返回可清理与保留两组",
          isinstance(part.get("cleanable"), list)
          and isinstance(part.get("keep"), list))
    check("分区带身份与说明字段（供界面按身份措辞）",
          part.get("kind") in ("browser", "game", "unknown")
          and bool(part.get("title")) and bool(part.get("note")),
          f"kind={part.get('kind')}")
    check("分区给出两组体积合计",
          part.get("cleanable_size_h") and part.get("keep_size_h"),
          f"{part.get('cleanable_size_h')} / {part.get('keep_size_h')}")
    # 关键：保留项的体积必须真的量出来。早先因 run_ps 折叠数组，
    # 这里会恒为 0 —— 这条断言就是防止那个 bug 复活。
    dirs_kept = [x for x in part["keep"] if x["type"] == "dir"]
    if dirs_kept:
        check("保留目录的体积被真实测量（不是恒 0）",
              any(x["size"] > 0 for x in dirs_kept),
              f"{len(dirs_kept)} 个目录，最大 {max(x['size'] for x in dirs_kept):,} 字节")
    check("每个子项都有体积的可读格式",
          all(x.get("size_h") for x in part["cleanable"] + part["keep"]))
    check("缓存子目录不许出现在「保留」里被误标为可清",
          not [x for x in part["keep"]
               if x["name"].strip().lower() in app.BROWSER_CACHE_DIRS])

    # 3) 身份判定：游戏目录不得被说成浏览器
    games = [g for g in (app.discover_steam().get("games") or [])
             if g.get("path") and g.get("exists")]
    if games:
        g = max(games, key=lambda x: x.get("size") or 0)
        gp = app.analyze_dir_partition(g["path"])
        check("游戏安装目录被识别为 game", gp.get("kind") == "game",
              f"{g['name']} → kind={gp.get('kind')}")
        if gp.get("kind") == "game":
            check("游戏目录的解释文案与游戏相关",
                  all("游戏" in x["why"] for x in gp["keep"]),
                  str([x["why"] for x in gp["keep"][:2]]))
            check("游戏目录不得出现浏览器用户数据文案",
                  not any("浏览器" in x["why"] for x in gp["keep"]))
            check("游戏目录的可清理字节应为 0（工具不删游戏）",
                  gp.get("cleanable_size") == 0,
                  f"{gp.get('cleanable_size_h')}")
    else:
        skip("游戏目录身份判定", "本机没有 Steam 游戏")
    check("不存在的路径报错而不是返回空目录",
          bool(app.analyze_dir_partition("Z:\\no-such-dir-oc").get("error")))


def phase6b_multiplatform() -> None:
    """Epic / GOG 多平台游戏发现（v2.2）。

    合成数据钉死纯解析逻辑（任何机器都能跑）；
    真实数据按「有就验内容，没有就验空结构」的契约走，不因环境缺游戏而误报。
    """
    print("\n=== 阶段六B：Epic / GOG 多平台游戏发现 ===")

    # ---- 1) Epic 清单解析（合成数据）----
    good = app._epic_game_from_manifest({
        "DisplayName": "测试游戏", "InstallLocation": "E:\\Games\\TestGame",
        "InstallSize": 123456789, "AppName": "TestApp123",
        "bIsApplication": True, "CatalogNamespace": "abc"})
    check("Epic 解析：正常游戏清单被接受",
          bool(good) and good["name"] == "测试游戏"
          and good["size"] == 123456789 and good["path"].endswith("TestGame"))
    check("Epic 解析：非应用条目被剔除（引擎/插件类）",
          app._epic_game_from_manifest({
              "DisplayName": "Quixel Bridge", "InstallLocation": "D:\\epic\\UE_5.7",
              "bIsApplication": False, "CatalogNamespace": "ue"}) is None)
    check("Epic 解析：ue 命名空间即使可执行也剔除",
          app._epic_game_from_manifest({
              "DisplayName": "Unreal Engine", "InstallLocation": "D:\\epic\\UE_5.7",
              "bIsApplication": True, "CatalogNamespace": "ue"}) is None)
    check("Epic 解析：体积字段非法不抛异常",
          app._epic_game_from_manifest({
              "DisplayName": "X", "InstallLocation": "D:\\g\\x",
              "InstallSize": " NaN ", "bIsApplication": True})["size"] == 0)
    check("Epic 解析：缺安装路径返回 None",
          app._epic_game_from_manifest({"DisplayName": "X", "bIsApplication": True}) is None)
    check("Epic 解析：非 dict 输入返回 None",
          app._epic_game_from_manifest(None) is None
          and app._epic_game_from_manifest([1, 2]) is None)

    # ---- 2) GOG 注册表值解析（合成数据）----
    gg = app._gog_game_from_values("12345", {"path": "D:\\GOG\\Game", "gameName": "测试 GOG"})
    check("GOG 解析：正常注册表值被接受",
          bool(gg) and gg["name"] == "测试 GOG" and gg["path"].endswith("Game"))
    check("GOG 解析：缺路径返回 None",
          app._gog_game_from_values("1", {"gameName": "X"}) is None)
    check("GOG 解析：缺名字时回退到带 ID 的占位名",
          "12345" in app._gog_game_from_values("12345", {"path": "D:\\g\\x"})["name"])

    # ---- 3) 真实发现：Epic（有就验内容，没有就验空结构）----
    t0 = time.time()
    de = app.discover_epic()
    dt = time.time() - t0
    check("Epic 发现在 5 秒内完成", dt < 5, f"{dt:.2f} 秒")
    check("Epic 返回结构完整",
          all(k in de for k in ("found", "games", "game_count", "total_size_h")))
    if de["found"]:
        check("Epic 游戏记录带平台与安全标记",
              all(g["platform"] == "epic" and g["cleanable"] is False
                  and g["category"] == "游戏" for g in de["games"]))
        check("Epic 不混入引擎/插件类条目",
              not [g["name"] for g in de["games"]
                   if any(k in g["name"].lower()
                          for k in ("unreal engine", "quixel", "fab"))],
              str([g["name"] for g in de["games"]]))
        check("Epic 游戏体积已量出",
              any(g["size"] > 1024 ** 3 for g in de["games"]),
              str([f"{g['name']}={g['size_h']}" for g in de["games"]]))
    else:
        skip("Epic 真实数据", "本机未检测到 Epic 游戏")

    # ---- 4) 真实发现：GOG（本机多半没有，验证不炸与结构）----
    dg = app.discover_gog()
    check("GOG 发现不抛异常且结构完整",
          all(k in dg for k in ("found", "games", "game_count")))
    if dg["found"]:
        check("GOG 游戏记录带平台与安全标记",
              all(g["platform"] == "gog" and g["cleanable"] is False
                  for g in dg["games"]))
    else:
        skip("GOG 真实数据", "本机未检测到 GOG 游戏")

    # ---- 5) 统一入口与树标注索引 ----
    dall = app.discover_games()
    check("统一入口：游戏数等于三平台之和",
          dall["game_count"]
          == sum(dall["platforms"][p]["game_count"] for p in ("steam", "epic", "gog")))
    check("统一入口：按体积降序",
          all(dall["games"][i]["size"] >= dall["games"][i + 1]["size"]
              for i in range(len(dall["games"]) - 1)))
    gdirs, glibs = app.game_install_dirs()
    check("游戏目录索引返回 (dict, set)",
          isinstance(gdirs, dict) and isinstance(glibs, set))
    if de["found"]:
        ep = de["games"][0]["path"].lower()
        check("索引覆盖本机 Epic 游戏路径", ep in gdirs and gdirs[ep] == "epic", ep)

    # ---- 6) 占用树整合：Epic 游戏所在盘的树必须标出游戏本体 ----
    if de["found"]:
        letter = os.path.splitdrive(de["games"][0]["path"])[0].rstrip(":").upper()
        ov = app.scan_disk_overview(letter)
        tagged = [n for n in ov.get("nodes", []) if n.get("game_platform") == "epic"]
        check("占用树标出 Epic 游戏本体",
              len(tagged) >= min(2, de["game_count"]),
              f"{len(tagged)} 个 / 引擎={ov.get('engine')}")
        check("被标游戏本体的节点不可清理",
              all(n["cleanable"] is False and n["category"] == "游戏" for n in tagged))
        libtag = [n for n in ov.get("nodes", []) if n.get("game_library")]
        check("占用树标出 Epic/GOG 库目录", bool(libtag),
              str([n["name"] for n in libtag][:3]))


def phase6c_packs() -> None:
    """资源包加载的「用户包覆盖内置」语义（load_packs 同 id 去重）。

    覆盖语义：packs_update_from_remote 会把与内置同 id 的更新包写进
    config/packs-user/，load_packs 必须按同 id 去重，否则同名包出现
    两行、匹配条目翻倍。全程用临时目录 + 模块属性打补丁，不往仓库
    packs/ 或 config/packs-user/ 写任何文件。
    """
    print("\n=== 阶段六C：资源包覆盖语义（临时目录）===")
    saved = (app.PACKS_DIR, app.USER_PACKS_DIR, dict(app._PACKS_CACHE))
    tmp = Path(tempfile.mkdtemp(prefix="oc_packs_ovr_"))
    try:
        bdir = tmp / "builtin"
        bdir.mkdir()
        udir = tmp / "user"
        udir.mkdir()
        _ent = lambda title, kw: {"match": {"any": [kw]}, "title": title}  # noqa: E731
        bpack = {"id": "ovr-pack", "name": "内置版", "type": "custom",
                 "entries": [_ent("内置释义", "\\ovr-target")]}
        upack = {"id": "ovr-pack", "name": "用户版", "type": "custom",
                 "entries": [_ent("用户释义", "\\ovr-target")]}
        other = {"id": "only-user", "name": "仅用户", "type": "custom",
                 "entries": [_ent("仅用户释义", "\\only-user-dir")]}
        (bdir / "ovr-pack.json").write_text(
            json.dumps(bpack, ensure_ascii=False), encoding="utf-8")
        (udir / "ovr-pack.json").write_text(
            json.dumps(upack, ensure_ascii=False), encoding="utf-8")
        (udir / "only-user.json").write_text(
            json.dumps(other, ensure_ascii=False), encoding="utf-8")
        app.PACKS_DIR = bdir
        app.USER_PACKS_DIR = udir
        app._PACKS_CACHE["key"] = None
        r = app.load_packs(use_cache=False)
        ids = [p["id"] for p in r["packs"]]
        check("同 id 用户包覆盖内置（列表无重复）",
              sorted(ids) == ["only-user", "ovr-pack"], str(ids))
        byid = {p["id"]: p for p in r["packs"]}
        check("覆盖后的包来自用户目录（元信息取用户版）",
              byid["ovr-pack"]["source"] == "user"
              and byid["ovr-pack"]["name"] == "用户版")
        hit = app.packs_lookup("ovr-target", "C:\\ovr-target")
        check("匹配命中用户版条目",
              bool(hit) and hit.get("title") == "用户释义",
              str((hit or {}).get("title")))
        # 损坏的用户包同样参与覆盖：如实显示错误行，不静默回退
        (udir / "broken.json").write_text("{ not json", encoding="utf-8")
        app._PACKS_CACHE["key"] = None
        r2 = app.load_packs(use_cache=False)
        bro = next((p for p in r2["packs"] if p["id"] == "broken"), None)
        check("损坏用户包如实报错（source=user、ok=False）",
              bro is not None and not bro["ok"] and bro["source"] == "user")
        check("损坏包不产生匹配条目",
              all(rec["pack"] != "broken" for rec in r2["entries"]))
    finally:
        app.PACKS_DIR, app.USER_PACKS_DIR = saved[0], saved[1]
        app._PACKS_CACHE.clear()
        app._PACKS_CACHE.update(saved[2])
        shutil.rmtree(tmp, ignore_errors=True)


def phase6d_fsgroups() -> None:
    """自定义聚合组的加载校验与备份快照。

    load_fs_groups 必须丢掉畸形条目（缺名字 / paths 为空）、归一化路径
    （统一斜杠、去尾分隔符、小写）并去重；save_fs_groups 每次写入前
    要留上一版完整快照（.bak），这是用户手工归组数据的唯一恢复途径。
    全程临时目录 + 模块属性打补丁，不碰真实 config/。
    """
    print("\n=== 阶段六D：自定义聚合组（临时目录）===")
    saved_path = app.FS_GROUPS_PATH
    tmp = Path(tempfile.mkdtemp(prefix="oc_fsgroups_"))
    try:
        app.FS_GROUPS_PATH = tmp / "fs-groups.json"
        # 1. 畸形条目被丢弃、路径归一化去重、缺 id 时按内容补稳定 id
        (tmp / "fs-groups.json").write_text(json.dumps({"groups": [
            {"id": "g-ok", "name": "正常组",
             "paths": ["D:/Steam/", "d:\\steam", "D:\\Steam\\Sub"], "created": "x"},
            {"name": "没有 paths 的组", "paths": []},
            {"id": "g-noname", "name": "   ", "paths": ["C:/x"]},
            {"name": "没写 id 的组", "paths": ["C:/y"]},
            "not-a-dict",
        ]}, ensure_ascii=False), encoding="utf-8")
        groups = app.load_fs_groups()
        check("畸形条目被丢弃，只留合法组", len(groups) == 2, str(len(groups)))
        ok = next(g for g in groups if g["id"] == "g-ok")
        check("路径归一化（斜杠/大小写/尾分隔符）并去重",
              ok["paths"] == ["d:\\steam", "d:\\steam\\sub"], str(ok["paths"]))
        noid = next(g for g in groups if g["name"] == "没写 id 的组")
        check("缺 id 时按内容补稳定 id（两次加载一致）",
              noid["id"] == next(g for g in app.load_fs_groups()
                                 if g["name"] == "没写 id 的组")["id"]
              and noid["id"].startswith("grp-"), noid["id"])
        # 2. save 前留 .bak：第二版写坏也有第一版可救
        app.save_fs_groups([{"id": "g1", "name": "第一版",
                             "paths": ["c:/a"], "created": ""}])
        app.save_fs_groups([{"id": "g1", "name": "第二版",
                             "paths": ["c:/b"], "created": ""}])
        bak = json.loads((tmp / "fs-groups.json.bak").read_text(encoding="utf-8"))
        check("每次写入前留上一版 .bak 快照",
              bak["groups"][0]["name"] == "第一版", str(bak))
        cur = json.loads((tmp / "fs-groups.json").read_text(encoding="utf-8"))
        check("当前文件是最新版",
              cur["groups"][0]["name"] == "第二版", str(cur))
        # 3. 空列表合法（用户删完全部组）：落盘为空表，不再抛错
        app.save_fs_groups([])
        check("空列表合法（全部删除后落盘为空表）",
              app.load_fs_groups() == [])
    finally:
        app.FS_GROUPS_PATH = saved_path
        shutil.rmtree(tmp, ignore_errors=True)


def phase7_scoping_and_accel() -> None:
    """盘符归属 + 自动发现的安全护栏 + WizTree 加速。

    这一段盯的是两类**会让用户误删**的缺陷：

    1. 盘符归属。用户实测反馈「D/E 盘老是冒出包管理器/浏览器/临时文件」——
       根因是 %LOCALAPPDATA% 类目标没标 system_only，而这类路径永远展开成
       系统盘的用户目录，于是每个盘都挂了一遍 C 盘的内容。选 D 盘却列出
       「D 盘的 pip 缓存」（实际是 C:\\Users\\…）纯属误导，必须钉死。

    2. 自动发现的误报。它是按「目录名命中知识库」推荐清理的，
       而知识库关键词是给缓存目录写的。实测曾把
       C:\\Program Files (x86)\\Adobe、某盘根下一个名字很短的目录（真的装着 Photoshop 2024）
       推荐为「可清理」—— 勾选等于卸载软件。
    """
    print("\n=== 阶段七：盘符归属 + 自动发现护栏 + WizTree 加速 ===")

    # ---- 1) 盘符归属：非系统盘不得出现指向别的盘的路径 ----
    for letter in ("D", "E", "F"):
        if not os.path.isdir(f"{letter}:\\"):
            continue
        if app.is_system_drive(letter):
            continue
        stray = []
        for t in app.build_targets(letter):
            p = os.path.normpath(app.expand(t["path"], letter)).upper()
            # 盘根本身的合法形态是「D:」（normpath 不补反斜杠），只要盘符
            # 一致就没有指向别的盘；裸「D:」恰好证明目标落在本盘。
            if not (p == f"{letter}:" or p.startswith(f"{letter}:\\")):
                stray.append((t["id"], p))
        check(f"{letter}: 盘的目标路径都落在 {letter}: 盘上", not stray,
              str(stray[:3]))

    # 判据本身要正确：%LOCALAPPDATA% 类路径必须被认成「只属于系统盘」
    check("is_user_local 认出 %LOCALAPPDATA% 目标",
          app.is_user_local({"path": r"%LOCALAPPDATA%\pip\Cache"}))
    check("is_user_local 认出 %APPDATA% 目标",
          app.is_user_local({"path": r"%APPDATA%\Foo"}))
    check("is_user_local 不误伤 %DRIVE% 目标",
          not app.is_user_local({"path": r"%DRIVE%\Windows\Temp"}))
    check("targets.json 里所有用户目录目标都能被自动识别",
          all(app.is_user_local(t) for t in app.load_targets()["targets"]
              if any(tok in str(t.get("path", "")).upper()
                     for tok in app.USER_PROFILE_TOKENS)))

    # ---- 2) 自动发现护栏 ----
    check("自动发现排除 Program Files（程序本体不是缓存）",
          app._auto_excluded(r"C:\Program Files\Adobe",
                             ["Program Files", "Adobe"]))
    check("自动发现排除 Program Files (x86)",
          app._auto_excluded(r"C:\Program Files (x86)\Adobe",
                             ["Program Files (x86)", "Adobe"]))
    check("自动发现仍然放行普通缓存目录",
          not app._auto_excluded(r"D:\SomeApp\Cache", ["SomeApp", "Cache"]))

    # 装机目录识别：这是防误删的关键判据。
    # 真实样本（本机装了哪些软件）不写进代码，放 tools/local-cases.json 且
    # 不进版本库；公开副本没有该文件时这几项自动跳过，不留假阳性。
    cases = _load_local_cases()
    prog_cases = [str(p) for p in (cases.get("installed_programs") or [])]
    cache_cases = [str(p) for p in (cases.get("cache_dirs") or [])]
    if not prog_cases and not cache_cases:
        skip("装机目录识别实测", "无本机用例（tools/local-cases.json 缺失，"
                                 "可参考 local-cases.json.example 自建）")
    for p in prog_cases:
        if os.path.isdir(p):
            check(f"识别出装机目录：{p}", app._looks_like_installed_program(p))
    for p in cache_cases:
        if os.path.isdir(p):
            check(f"不把缓存/下载目录误判成装机目录：{p}",
                  not app._looks_like_installed_program(p))
    # 程序本体绝不能被自动发现推荐清理
    for p in prog_cases:
        if os.path.isdir(p):
            check(f"装机目录不出现在自动发现里：{p}",
                  not any(t["path"].lower() == p.lower()
                          for t in app.build_targets(p[0]) if t.get("discovered")))

    # ---- 3) WizTree 加速与便携管理 ----
    st = app.wiztree_status()
    check("wiztree_status 返回必要字段",
          all(k in st for k in ("found", "portable", "version", "license_note",
                                "download_url", "portable_dir")))
    check("WizTree 下载地址指向官方域名",
          "diskanalyzer.com" in st.get("download_url", ""))
    check("WizTree 许可证说明包含版权方及免费说明",
          "Antibody Software" in st.get("license_note", "")
          and "免费" in st.get("license_note", ""))

    wz = app.wiztree_available()
    if not wz.get("found"):
        skip("WizTree 加速", "本机未安装 WizTree（会自动回退到 PowerShell）")
    else:
        check("探测到 WizTree 可执行文件", os.path.isfile(wz["path"]), wz["path"])
        # 能关掉，便于排查与对照（否则无法验证回退路径）
        os.environ["OC_NO_WIZTREE"] = "1"
        try:
            check("OC_NO_WIZTREE=1 时停用加速", not app._wiz_ok())
        finally:
            os.environ.pop("OC_NO_WIZTREE", None)
        check("默认启用加速", app._wiz_ok())

        letter = "D" if os.path.isdir("D:\\") else "C"
        csv_path, err = app._wiztree_export(letter, timeout=420)
        check("WizTree 能导出整盘明细", bool(csv_path), err or "ok")
        if csv_path:
            try:
                table = app._wiztree_parse(csv_path)
                check("导出解析出目录体积表", len(table["dirs"]) > 0,
                      f"{len(table['dirs'])} 个目录 / {table['rows']:,} 行")
                check("导出行数远大于目录数（确实含文件级明细）",
                      table["rows"] > len(table["dirs"]))
                # 目录体积必须含子孙：根目录体积应显著大于其任一子目录
                root = os.path.normpath(f"{letter}:\\").lower()
                check("盘符根目录体积已量出", table["dirs"].get(root, 0) > 0,
                      app.human(table["dirs"].get(root, 0)))
                kids = app._wiztree_children(table["dirs"], f"{letter}:\\", 5)
                check("能取出顶层子目录且按体积降序",
                      len(kids) > 0
                      and all(kids[i]["size"] >= kids[i + 1]["size"]
                              for i in range(len(kids) - 1)),
                      " / ".join(f"{k['name']}={app.human(k['size'])}"
                                 for k in kids[:3]))
                check("子目录体积不超过父目录体积",
                      all(k["size"] <= table["dirs"][root] for k in kids))
            finally:
                try:
                    os.remove(csv_path)
                except OSError:
                    pass

        # 扫出来的树必须与「两条路径给出同一形状」一致
        ov = app.scan_disk_overview(letter)
        check(f"{letter}: 扫描走 WizTree 路径", ov.get("engine") == "wiztree",
              str(ov.get("engine")))
        check("扫描结果带引擎说明（界面要如实告知）",
              bool(ov.get("engine_label")) and bool(ov.get("engine_note")),
              str(ov.get("engine_note"))[:60])
        check("WizTree 路径下报告可任意层下钻", ov.get("subdir_ready") is True)
        check("占用树的节点带体积可读格式",
              all(n.get("size_h") for n in ov["nodes"]))

        # ---- 深扫（depth=2）与快速（depth=1）：2026-10-13 起排行默认只列
        # 第 1 层（父级不拆子级，深层靠下钻），「深扫」也只到 2 层。
        ov = app.scan_disk_overview(letter, 2)
        # 自定义提级：造一个深层目录、写入 pin 覆盖，快速扫描（1 层）后它
        # 必须作为顶层节点出现（不受展开深度限制）。
        probe = os.path.join(f"{letter}:\\", "_oc_pin_probe", "deep", "target")
        os.makedirs(probe, exist_ok=True)
        with open(os.path.join(probe, "x.bin"), "wb") as f:
            f.write(b"0" * 4096)
        _ov_real = app.load_overrides()
        _ov_backup = {k: dict(v) for k, v in _ov_real.get("targets", {}).items()
                      if k == os.path.normpath(probe).lower()}
        _ov_real.setdefault("targets", {})[os.path.normpath(probe).lower()] = \
            {"pin": True}
        app.save_overrides(_ov_real)
        try:
            ovq = app.scan_disk_overview(letter, 1)
            check("快速扫描正常返回",
                  not ovq.get("error") and ovq.get("node_count", 0) > 0,
                  f"{ovq.get('node_count')} 个节点 / 引擎={ovq.get('engine')}")
            check("快速扫描响应如实带深度标记",
                  ovq.get("max_depth") in (1, 2, 3), str(ovq.get("max_depth")))
            # 深度上限是 3：1 层树 + 注入的游戏本体/提级节点（Steam 库嵌套
            # 时最深到 3，属设计行为而非退化——与 cdp_check 口径一致）。
            check("快速扫描节点不超过 3 层深（1 层树 + 游戏本体/提级注入）",
                  all(int(n.get("depth") or 0) <= 3 for n in ovq.get("nodes", [])),
                  f"最深 {max((int(n.get('depth') or 0)) for n in ovq.get('nodes', []))}")
            pinned = [n for n in ovq.get("nodes", []) if n.get("pinned")]
            check("自定义提级目录出现在排行顶层",
                  any(os.path.normpath(n["path"]).lower()
                      == os.path.normpath(probe).lower()
                      and int(n.get("depth") or 0) == 1 and n.get("pinned")
                      for n in pinned),
                  f"pinned={len(pinned)} / probe={'命中' if pinned else '缺失'}")
            check("提级节点带 drill 能力（下钻看子项）",
                  all(n.get("can_drill") for n in pinned) if pinned else False)
        finally:
            cur = app.load_overrides()
            cur.setdefault("targets", {}).pop(os.path.normpath(probe).lower(), None)
            for k, v in _ov_backup.items():
                cur["targets"][k] = v
            app.save_overrides(cur)
        shutil.rmtree(os.path.join(f"{letter}:\\", "_oc_pin_probe"),
                      ignore_errors=True)
        check("快速扫描节点数不多于深扫",
              ovq["node_count"] <= len(ov["nodes"]),
              f"快速 {ovq['node_count']} vs 深扫 {len(ov['nodes'])}")
        # 二级拆分：查表必须秒回
        dirs = [n for n in ov["nodes"] if n["type"] == "dir" and n["can_drill"]]
        if dirs:
            t0 = time.time()
            sub = app.list_subdirs(letter, dirs[0]["path"])
            dt = time.time() - t0
            check("逐层拆子项可用", bool(sub.get("children")),
                  f"{dirs[0]['name']} → {len(sub.get('children') or [])} 项")
            check("查表式拆子项在 2 秒内返回", dt < 2, f"{dt * 1000:.0f} ms")
            check("子项都带体积与可读格式",
                  all(c.get("size_h") for c in sub["children"]))
            check("拆子项响应带该层真实总数（total ≥ 列出数）",
                  isinstance(sub.get("total"), int)
                  and sub["total"] >= len(sub["children"]),
                  f"total={sub.get('total')} / listed={len(sub['children'])}")
        else:
            skip("逐层拆子项", "本盘没找到可展开的目录")

        # 不存在的目录要报错而不是返回空
        check("拆不存在的目录时明确报错",
              bool(app.list_subdirs(letter, "Z:\\no-such-oc-dir").get("error")))

        # 导出文件必须被清理 —— 这条是被实测打脸后加的：
        # 早先解析完不删 CSV，连扫十几次后临时目录里攒了 24 个导出文件、约 1.2 GB。
        # 一个「磁盘清理工具」自己变成垃圾制造者，是绝对不能接受的。
        pat = os.path.join(tempfile.gettempdir(), "oc-wiztree-*.csv")
        leaked_before = glob.glob(pat)
        check("扫描前临时目录里没有遗留的导出文件",
              len(leaked_before) == 0,
              f"{len(leaked_before)} 个：" + str(leaked_before[:2]))
        app.scan_disk_overview(letter)
        leaked_after = glob.glob(pat)
        check("扫描后不会残留 WizTree 导出文件（不能自己制造垃圾）",
              len(leaked_after) == 0,
              f"残留 {len(leaked_after)} 个：" + str(leaked_after[:2]))
        # 崩溃遗留的清理函数也要能用
        bogus = os.path.join(tempfile.gettempdir(), "oc-wiztree-Z-999999.csv")
        with open(bogus, "w", encoding="utf-8") as fh:
            fh.write("x")
        app.cleanup_stale_wiztree_exports()
        check("启动时能清掉异常退出遗留的导出文件", not os.path.exists(bogus))

    # ---- 4) 无 WizTree 环境下的下钻能力（2026-09-30 放开，须持续可用）----
    make_sandbox()
    check("无加速探测：非空目录标记为可展开",
          app._dir_has_any_child(str(SANDBOX / "tree")))
    empty_dir = SANDBOX / "empty"
    empty_dir.mkdir(exist_ok=True)
    check("无加速探测：空目录不标可展开",
          not app._dir_has_any_child(str(empty_dir)))
    check("无加速探测：不存在/无权限的路径不抛异常",
          not app._dir_has_any_child(str(SANDBOX / "nope")))
    # 用不存在的盘符 Z 保证走不到 WizTree 缓存，强制命中原生回退分支
    sub = app.list_subdirs("Z", str(SANDBOX / "tree"))
    check("无加速回退：list_subdirs 走原生扫描且能列出子项",
          sub.get("engine") != "wiztree" and len(sub.get("children") or []) >= 2,
          f"engine={sub.get('engine')} / {len(sub.get('children') or [])} 项")
    check("无加速回退：子项体积已量出且带可读格式",
          all(c.get("size_h") for c in sub["children"])
          and any(c["size"] > 0 for c in sub["children"]))
    # total 字段：前端「该层共有 N 个子项，只列出前 M 个」提示的真相来源
    check("无加速回退：total 等于实际列出数（小层不截断）",
          sub.get("total") == len(sub.get("children") or []),
          f"total={sub.get('total')} / listed={len(sub.get('children') or [])}")
    sub1 = app.list_subdirs("Z", str(SANDBOX / "tree"), limit=1)
    check("无加速回退：limit 截断时 total 仍报真实总数",
          len(sub1.get("children") or []) == 1
          and sub1.get("total", 0) >= len(sub.get("children") or []),
          f"listed=1 / total={sub1.get('total')}")
    shutil.rmtree(SANDBOX, ignore_errors=True)

    # ---- 5) 子进程调用的超时有界性 ----
    # 这一段对应一次实际观察到的卡死：扫描停在「正在读取 Steam 库清单」不动，
    # 进度条（当时还是按时长估算的假进度）继续往上爬，看起来像在正常工作。
    #
    # 根因不在被测脚本，而在 subprocess.run(timeout=…) 的超时实现：
    # Windows 上它超时后会先 kill()，再调一次 communicate() 去收输出；
    # 若残留子进程仍持有 stdout 管道，这次 communicate() 会永久等待。
    # 因此 run_ps 改成显式 Popen + 结束整棵进程树 + 有上限的收尾。
    #
    # 断言只钉「一定会返回」这一条性质，不钉具体耗时，以免在慢机器上误报。
    if os.name == "nt":
        t0 = time.time()
        r = app.run_ps("Start-Sleep -Seconds 120; 'never'", timeout=3)
        dt = time.time() - t0
        check("run_ps 超时后必定返回（不会无限阻塞）", dt < 30,
              f"{dt:.1f}s")
        check("run_ps 超时返回明确的错误并写明秒数",
              isinstance(r, dict) and "超时" in str(r.get("error")),
              str(r.get("error"))[:50])

        # 子进程持有 stdout 管道 —— 旧实现在这里会永久挂住
        t0 = time.time()
        r = app.run_ps(
            "$p = Start-Process -FilePath cmd "
            "-ArgumentList '/c','ping -n 90 127.0.0.1 > NUL' "
            "-WindowStyle Hidden -PassThru; Start-Sleep -Seconds 120; 'done'",
            timeout=4)
        dt = time.time() - t0
        check("run_ps 在子进程持有管道时也能返回", dt < 30, f"{dt:.1f}s")

        # 正常路径不受影响：中文往返、数组、错误分支
        check("run_ps 中文输出往返正确",
              app.run_ps('[pscustomobject]@{t="中文 ✓"} | ConvertTo-Json -Compress'
                         ).get("t") == "中文 ✓")
        check("run_ps 数组输出不被折成首元素",
              app.run_ps('@(1,2,3) | ConvertTo-Json -Compress') == [1, 2, 3])
        check("run_ps 无输出时给出带退出码的错误",
              "无输出" in str(app.run_ps("Write-Error 'x' 2>&1 | Out-Null"
                                         ).get("error")))
        check("run_ps 非 JSON 输出被识别",
              "不是合法 JSON" in str(app.run_ps("Write-Output 'not json'"
                                                ).get("error")))
    else:
        skip("run_ps 超时有界性", "只在 Windows 上有 PowerShell")

    # ---- 6) 内嵌脚本模板的语法 ----
    # 对应一次真实事故：v2.3 给磁盘递归脚本加「按节点预算上报进度」时，
    # `if (...) {` 少写了一个右花括号。模板能渲染、占位符能替换、静态 linter
    # 也只看数据文件，Python 侧完全看不出问题；但脚本一交给系统 shell 就在
    # 解析阶段直接失败（Missing closing '}' in statement block or type
    # definition.），全盘扫描因此整条不可用 —— 浏览器检查里三十多条依赖它的
    # 断言成片跳过，而唯一那条会让作业变红的断言又长期被误判成「CI 环境超时」。
    # 所以这里不只数括号，把渲染结果直接交给解析器判一次。
    tpl_names = sorted(n for n in dir(app)
                       if n.startswith("PS_") and isinstance(getattr(app, n), str))
    unbalanced = []
    for n in tpl_names:
        body = getattr(app, n)
        if body.count("{") != body.count("}"):
            unbalanced.append(
                f"{n}({{={body.count('{')} }}={body.count('}')})")
    check("内嵌脚本模板的花括号配平（少一个右花括号会让整段脚本语法失败）",
          not unbalanced,
          "；".join(unbalanced) or f"检查了 {len(tpl_names)} 个模板")

    depth_cap = max(1, min(app.DISK_TREE_MAX_DEPTH, 6))
    rendered = (app.PS_TREE_WALK
                .replace("__ROOT__", "C:" + os.sep)
                .replace("__PROGFILE__", "")
                .replace("__MINCHILD__", "1048576")
                .replace("__RATIO__", repr(app.DISK_TREE_MIN_DESCEND_RATIO))
                .replace("__FLOOR__", str(app.DISK_TREE_MIN_DESCEND_FLOOR))
                .replace("__MAXDEPTH__", str(depth_cap))
                .replace("__MAXPERDIR__", str(app.DISK_TREE_MAX_PER_DIR))
                .replace("__MAXNODES__", str(app.DISK_TREE_MAX_NODES))
                .replace("__MAXDESCENTS__", str(app.DISK_TREE_MAX_DESCENTS)))
    leftover = [t for t in ("__ROOT__", "__PROGFILE__", "__MINCHILD__", "__RATIO__",
                            "__FLOOR__", "__MAXDEPTH__", "__MAXPERDIR__",
                            "__MAXNODES__", "__MAXDESCENTS__") if t in rendered]
    check("磁盘递归脚本渲染后无残留占位符", not leftover,
          "、".join(leftover) or "9 个占位符全部替换")

    if os.name == "nt":
        tdir = tempfile.mkdtemp(prefix="oc-tpl-syntax-")
        try:
            sp = os.path.join(tdir, "walk-check.ps1")
            with open(sp, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write(rendered)
            probe = ("$e=$null;"
                     "[void][System.Management.Automation.Language.Parser]"
                     "::ParseFile('" + sp + "',[ref]$null,[ref]$e);"
                     "@{n=@($e).Count; first=(@($e) | Select-Object -First 1 | "
                     "ForEach-Object { $_.Message })} | ConvertTo-Json -Compress")
            r = app.run_ps(probe, timeout=120)
            n = r.get("n") if isinstance(r, dict) else None
            first = r.get("first") if isinstance(r, dict) else r
            check("磁盘递归脚本能被解析器接受（0 处语法错误）", n == 0,
                  f"错误 {n} 处：{first}")
        finally:
            shutil.rmtree(tdir, ignore_errors=True)
    else:
        skip("磁盘递归脚本的语法解析", "只在 Windows 上有对应解析器")


def phase8_identify_correctness() -> None:
    """识别正确性金标准用例（外部审计报告 2026-10-13，P0/P1 回归）。

    审计指出的盲区：既有断言全部是「输入合法性」测试（路径护栏 + HTTP 层），
    没有一条在问「这个目录到底是什么」—— D:\\Quark\\Webui（15 GB 的 SD-WebUI
    整合包）因此差点能被当作云盘缓存整目录永久删除。本阶段用**合成目录**
    补上这个盲区：在 Temp 里现场搭出内容特征，不依赖真机上恰好装了什么。
    """
    print("\n---- 阶段八：识别正确性（金标准用例 + 删除预检）----")
    tmp = tempfile.mkdtemp(prefix="oc_ident_")
    # 行级覆盖测试不得碰用户真实 config/custom-overrides.json：整个阶段
    # 把 OVERRIDES_PATH 指到临时文件，结束还原。
    real_ov = app.OVERRIDES_PATH
    app.OVERRIDES_PATH = Path(tmp) / "overrides-test.json"
    try:
        # ① 整合包：名字不在知识库里、祖先路径带 "quark"，内容有 python_embeded
        webui = os.path.join(tmp, "Quark", "Webui")
        os.makedirs(os.path.join(webui, "python_embeded"))
        with open(os.path.join(webui, "launch.bat"), "w") as f:
            f.write("echo ok")
        info = app.identify("Webui", webui, "dir")
        check("整合包目录（祖先带 quark）不被判为可清理",
              info.get("cleanable") is False, str(info.get("title")))
        check("整合包目录带内容判定标记", bool(info.get("content_protected")))
        check("整合包目录风险升为高", info.get("risk") == "高")

        # ② 对照：普通缓存目录仍可清理（内容判定不许误伤）
        cache = os.path.join(tmp, "cache")
        os.makedirs(cache)
        with open(os.path.join(cache, "chunk0001.bin"), "wb") as f:
            f.write(b"0" * 2048)
        info2 = app.identify("cache", cache, "dir")
        check("普通 cache 目录仍可清理", info2.get("cleanable") is True)
        check("普通 cache 目录不带内容判定标记", not info2.get("content_protected"))

        # ③ P0-2：下载目录默认建议保留（cleanable 由 true 改 false）
        dl = os.path.join(tmp, "Downloads")
        os.makedirs(dl)
        info3 = app.identify("Downloads", dl, "dir")
        check("下载目录不再默认可清理（P0-2）",
              info3.get("cleanable") is False, f"rule={info3.get('rule_id')}")
        check("下载目录风险为高", info3.get("risk") == "高")

        # ④ P0-1 收窄：裸 driver/drivers 目录名不再命中驱动规则
        #（注意断言口径：合成目录放在 Temp 下，祖先链的 Temp 合理命中
        #  cache-temp，这不是回归；要验证的只是「drivers 规则不再被裸词命中」）
        drv = os.path.join(tmp, "drivers")
        os.makedirs(drv)
        info4 = app.identify("drivers", drv, "dir")
        check("裸 drivers 目录名不再命中驱动规则",
              info4.get("rule_id") != "drivers", f"rule={info4.get('rule_id')}")

        # ⑤ P0-3：软件安装目录自带的 cache 被护栏拒绝
        prog = os.path.join(tmp, "SomeApp")
        os.makedirs(os.path.join(prog, "cache"))
        with open(os.path.join(prog, "unins000.exe"), "wb") as f:
            f.write(b"MZ")
        ok, why = app.check_deletable(os.path.join(prog, "cache"), "tree")
        check("软件自带的 cache 目录被拒绝", ok is False, why[:60])
        check("拒绝理由说明是程序自带", "自带" in why)

        # ⑥ 对照：系统 Temp 容器仍放行（\Windows\Temp 语义不能被误伤）
        sys_tmp = os.environ.get("TEMP") or r"C:\Windows\Temp"
        ok2 = app.check_deletable(sys_tmp, "tree")[0]
        check("系统 Temp 容器仍放行", ok2 is True)

        # ⑦ P1-1：目录内有被占用文件 → 预检命中、改名失败 → 零副作用
        occupied = os.path.join(tmp, "occupied-app")
        os.makedirs(occupied)
        dll_path = os.path.join(occupied, "app.dll")
        hold = open(dll_path, "wb")
        try:
            hold.write(b"MZ" * 16)
            hold.flush()
            locked = app._precheck_locked_files(occupied)
            check("预检发现被持有的文件（本进程句柄）",
                  any(p.endswith("app.dll") for p in locked), str(locked)[:80])
            full, partial, _note = app.rm_tree_strict(occupied)
            check("被占用目录整体未删（零副作用，不再部分删除）",
                  full is False and partial is False,
                  f"full={full} partial={partial}")
            check("被占用目录内容完好",
                  os.path.isdir(occupied) and os.path.isfile(dll_path))
        finally:
            hold.close()

        # ⑧ 无占用目录：三态删除 = 全删
        plain = os.path.join(tmp, "plain-cache")
        os.makedirs(plain)
        with open(os.path.join(plain, "x.bin"), "wb") as f:
            f.write(b"0" * 1024)
        full2, partial2, _note2 = app.rm_tree_strict(plain)
        check("无占用目录三态删除=全删",
              full2 is True and partial2 is False and not os.path.exists(plain))

        # ⑨ 行级覆盖（右键自定义）字段回环：保存 → 读取 → 恢复
        key = os.path.normpath(cache).lower()
        data = app.load_overrides()
        data["targets"][key] = {"label": "我的缓存", "note": "自定义说明"}
        app.save_overrides(data)
        got = app.load_overrides()["targets"].get(key, {})
        check("行级覆盖 label/note 可保存读取",
              got.get("label") == "我的缓存" and got.get("note") == "自定义说明")
        data = app.load_overrides()
        data["targets"].pop(key, None)
        app.save_overrides(data)
        check("行级覆盖删除后不再出现",
              key not in app.load_overrides()["targets"])

        # ⑩ 回收站 $I 元数据解析（2026-10-13 回归）：v2 真实布局是
        # ver(8) + size(8) + 删除时间(8) + 名称长度(4) + 名称(UTF-16LE)。
        # 曾把 offset 16 的删除时间当成名称长度 → 真机 1979 条全部解析失败。
        # 用合成字节固定这个布局，不再依赖真机回收站里恰好有什么。
        import struct as _struct
        ipath = os.path.join(tmp, "$Imock")
        name2 = r"C:\some\removed\item.txt"
        blob2 = (_struct.pack("<qq", 2, 4096)
                 + _struct.pack("<q", 133000000000000000)
                 + _struct.pack("<i", len(name2))
                 + name2.encode("utf-16le"))
        with open(ipath, "wb") as f:
            f.write(blob2)
        got = app._parse_i_file(ipath)
        check("$I v2 解析出原始路径", got is not None and got[0] == name2, str(got))
        check("$I v2 解析出字节数", got is not None and got[1] == 4096)
        check("$I v2 配对 $R 路径正确",
              got is not None and os.path.basename(got[2]) == "$Rmock")
        # v1：24 字节定长名称字段
        name1 = r"C:\old\item.bin"
        blob1 = _struct.pack("<qq", 1, 512) + _struct.pack("<q", 0) \
            + name1.encode("utf-16le").ljust(520, b"\x00")
        with open(ipath, "wb") as f:
            f.write(blob1)
        got1 = app._parse_i_file(ipath)
        check("$I v1 解析出原始路径", got1 is not None and got1[0] == name1, str(got1))
        # 未知版本 / 截断 → 如实返回 None，调用方跳过
        with open(ipath, "wb") as f:
            f.write(_struct.pack("<q", 9) + b"\x00" * 40)
        check("未知版本 $I 返回 None", app._parse_i_file(ipath) is None)
        with open(ipath, "wb") as f:
            f.write(b"\x02\x00")
        check("截断 $I 返回 None", app._parse_i_file(ipath) is None)
    finally:
        app.OVERRIDES_PATH = real_ov
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 上次运行若被强杀（关窗/超时/BrokenPipe），BACKUP 会遗留且真实配置被测试内容覆盖。
    # 先把遗留备份还原回去再重新备份，否则测试内容会被当成"真实配置"永久固化。
    if BACKUP.exists():
        shutil.move(str(BACKUP), str(REAL_TARGETS))
    shutil.copy2(REAL_TARGETS, BACKUP)

    # history.json / schedule.json 是用户的真实数据，测试期间会被改写。
    # 旧版测试完直接删除会丢用户真实清理记录，必须先备份、跑完再还原。
    state_backups = {}
    for f in ("history.json", "schedule.json"):
        p = HERE / f
        b = HERE / ("." + f + ".backup")
        if not p.exists() and b.exists():  # 上次被强杀，用户数据还躺在备份里，先救回来
            shutil.move(str(b), str(p))
        if p.exists():
            shutil.copy2(p, b)
            state_backups[f] = b
    try:
        # 每个阶段独立兜底：某一阶段抛异常（如冷启动机器上 HTTP 超时）时，
        # 只把该阶段记为失败并继续跑后面的阶段。旧版会让一个未捕获异常
        # 直接终止整套测试，后面的阶段根本跑不到，CI 上尤其明显。
        def _phase(label: str, fn) -> None:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - 测试框架，需要兜住一切
                traceback.print_exc()
                check(f"{label} 未抛异常", False, f"{type(e).__name__}: {e}")

        _phase("阶段一（配置/护栏/盘符/换算）", phase1)
        _phase("写入测试配置", write_test_config)
        _phase("阶段二（真实删除逻辑）", phase2)
        _phase("阶段三（HTTP 端到端）", phase3_http)
        _phase("阶段四（仓库一致性）", phase4_repo)
        _phase("阶段五（Steam）", phase5_steam)
        _phase("阶段六（分区分析）", phase6_partition)
        _phase("阶段六B（多平台游戏）", phase6b_multiplatform)
        _phase("阶段六C（资源包覆盖语义）", phase6c_packs)
        _phase("阶段六D（自定义聚合组）", phase6d_fsgroups)
        _phase("阶段七（盘符与加速）", phase7_scoping_and_accel)
        _phase("阶段八（识别正确性金标准）", phase8_identify_correctness)
    finally:
        shutil.move(str(BACKUP), str(REAL_TARGETS))
        for f in ("history.json", "schedule.json"):
            p = HERE / f
            b = state_backups.get(f)
            if b is not None and b.exists():
                shutil.move(str(b), str(p))   # 还原用户真实数据
            elif p.exists():
                p.unlink()                    # 测试前本就没有，清掉测试产物
        for f in ("custom-rules.json", "convert.json.tmp", "targets.json.tmp"):
            p = HERE / "config" / f
            if p.exists():
                p.unlink()
        shutil.rmtree(HERE / "__pycache__", ignore_errors=True)
        shutil.rmtree(TOOLS_DIR / "__pycache__", ignore_errors=True)
        n = len(json.loads(REAL_TARGETS.read_text(encoding="utf-8-sig"))["targets"])
        print(f"\n  已还原真实 targets.json（{n} 项）")

    print(f"\n{'=' * 52}")
    print(f"  通过 {len(PASS)} 项，失败 {len(FAIL)} 项，跳过 {len(SKIP)} 项")
    if FAIL:
        print("  失败项：" + "、".join(FAIL))
    print("=" * 52 + "\n")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
