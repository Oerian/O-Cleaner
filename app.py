#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
O-Cleaner —— 磁盘清理助手（本地 WebUI，Python 标准库实现，零第三方依赖）

用法:
    python app.py                      # 启动并自动打开浏览器
    python app.py --port 8770          # 指定端口
    python app.py --no-browser         # 不自动打开浏览器
    python app.py --scan-only          # 命令行扫描一次并打印
    python app.py --scan-only --drive D

设计要点:
  * 只用标准库，不引入任何 pip 依赖（清理磁盘的工具不该反过来往系统盘装包）
  * 路径全部使用 %LOCALAPPDATA% / %DRIVE% 这类变量，不写死盘符与用户名，
    因此整个文件夹可以整体拷贝到其它 Windows 设备上直接运行
  * 删除目标必须位于某个盘符之下，并受硬编码保护名单拦截
  * 逐个文件删除并统计失败项，被占用的文件跳过而不是整体失败
  * 单个目标出意外不会中断整批清理
"""

from __future__ import annotations

import argparse
import ctypes
import fnmatch
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import string
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request as _UrlRequest, urlopen as _urlopen
from ps_scripts import (PS_BIG_FILES, PS_DIR_KIND, PS_LOCK_HOLDERS,
                        PS_MEASURE_GLOB, PS_MEASURE_TREE, PS_RECYCLE,
                        PS_STEAM, PS_STEAM_ORPHAN_SIZE, PS_TREE_WALK,
                        PS_UTF8_PREAMBLE)

BASE_DIR = Path(__file__).resolve().parent


def _argv_data_dir() -> str:
    """启动参数里找 --data-dir（须在模块常量定值前生效，故在 import 期预扫）。"""
    for i, v in enumerate(sys.argv):
        if v == "--data-dir" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if v.startswith("--data-dir="):
            return v.split("=", 1)[1]
    return ""


# 数据存档目录：config/ 与 history.json、schedule.json、disk-trend.json 等
# 全部运行数据都落在这里。默认跟程序同目录（绿色便携：整个文件夹拷走 =
# 数据一起迁移，U 盘携带即插即用）；程序目录只读或想把数据单独存放时，
# 用 --data-dir <路径> 或环境变量 OC_DATA_DIR 重定向到任意可写位置。
DATA_DIR = Path(os.environ.get("OC_DATA_DIR") or _argv_data_dir() or BASE_DIR)
# 数据目录可写性的进程内缓存（None = 尚未探测），见 data_dir_writable()
_DATA_DIR_WRITABLE: bool | None = None
CONFIG_DIR = DATA_DIR / "config"
WEB_DIR = BASE_DIR / "web"

# 版本号只在这里定义一次。
# 此前它同时散落在三处（这里的 server_version、WizTree 下载的 User-Agent、
# config/drives.json 的 app.version），2026-10-12 升 v2.3 时漏改了后两处，
# 结果界面显示的仍是 2.2。收敛成一个常量后，drives.json 与打包脚本由自测断言
# 强制保持一致（见 tools/selftest.py 的「版本号三处一致」）。
APP_VERSION = "2.7"
APP_UA = f"O-Cleaner/{APP_VERSION}"

TARGETS_PATH = CONFIG_DIR / "targets.json"
DRIVES_PATH = CONFIG_DIR / "drives.json"
CONVERT_PATH = CONFIG_DIR / "convert.json"
FILETYPES_PATH = CONFIG_DIR / "filetypes.json"
CUSTOM_PATH = CONFIG_DIR / "custom-rules.json"
# 用户自定义聚合组：把占用树里任意路径归拢进自己命名的组（如「游戏」「工作」）。
# 纯显示层组织，不影响识别与清理判定；只存本机、不进版本库、不进分发包。
FS_GROUPS_PATH = CONFIG_DIR / "fs-groups.json"
# 用户自定义释义 / 结论覆盖：按清理项改写说明文案或翻转「可清理」结论。
# 用户显式操作的最后一手，优先级高于资源包与内置知识库；
# 只存本机、不进版本库、不进分发包，见 .gitignore。
OVERRIDES_PATH = CONFIG_DIR / "custom-overrides.json"
# 游戏中文名映射：纯显示用，不参与任何清理判断
GAMENAMES_PATH = CONFIG_DIR / "game-names.json"
# 内置知识库的英文文案（i18n L3）：随仓库分发、进版本库。
# 英文界面请求接口时带 lang=en，后端在响应出口按 rule_id 覆盖
# what/advice/tip 与置信度标签；缺键回落中文。只影响显示，不影响判定。
KB_EN_PATH = CONFIG_DIR / "kb-en.json"
# 资源包：随仓库分发的内置包 + 用户自己导入的包（后者不进版本库）
PACKS_DIR = BASE_DIR / "packs"
USER_PACKS_DIR = CONFIG_DIR / "packs-user"
PACKS_STATE_PATH = CONFIG_DIR / "packs-state.json"
# AI 设置（含密钥）。只存本机、不进版本库，见 .gitignore。
AI_CONFIG_PATH = CONFIG_DIR / "ai.json"
HISTORY_PATH = DATA_DIR / "history.json"
# 各盘用量快照（磁盘趋势曲线的数据源）：每天每盘最多记一条，保留 90 天。
# 运行数据，不进版本库、不进分发包。
DISK_TREND_PATH = DATA_DIR / "disk-trend.json"
SCHEDULE_PATH = DATA_DIR / "schedule.json"

# 绝不允许删除的目录名（即使配置文件被改坏也拦得住）
PROTECTED_DIR_NAMES = {
    "windows", "system32", "syswow64", "winsxs", "installer", "fonts",
    "assembly", "servicing", "program files", "program files (x86)",
    "programdata", "recovery", "system volume information", "$recycle.bin",
    "users", "default", "public", "appdata", "boot", "perflogs", "msocache",
    "onedrive", "documents and settings",
}
# 这些名字只有在“盘符根目录的直接子目录”层级才整体保护；
# 更深的同名目录（例如 C:\Windows\Temp）允许清理，由更深层的规则把关。
PROTECTED_AT_DRIVE_ROOT = {"windows", "program files", "program files (x86)",
                           "programdata", "users", "recovery", "perflogs"}
# 允许作为“清理容器”的目录名（即使名字本身在通用保护名单里）
ALLOWED_CONTAINER_NAMES = {"temp", "tmp", "cache", "caches", ".cache",
                           "logs", "log", "crashdumps", "minidump", "prefetch"}

CREATE_NO_WINDOW = 0x08000000
IS_WINDOWS = os.name == "nt"


# ======================================================================
#  基础工具
# ======================================================================

def is_admin() -> bool:
    if not IS_WINDOWS:
        try:
            return os.geteuid() == 0  # type: ignore[attr-defined]
        except Exception:
            return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def data_dir_writable() -> bool:
    """实测数据存档目录的可写性（自检面板据此判断历史/配置能否落盘）。
    只查目录存在不够 —— 目录存在但只读（如被放进 Program Files、
    或整个目录被设了只读属性）时，写入会静默失败。写一次探针文件最可靠。

    进程内只探一次并缓存：这个函数被 /state 每次调用，而 DATA_DIR 默认就是
    仓库根目录 —— 以前每个请求都在仓库里写一个探针文件再删掉。在带删除保护
    钩子的宿主环境里，那个 unlink 会被拦下并抛 SystemExit，直接打断请求
    （实测 /state 反复 RemoteDisconnected，并耗尽了宿主的删除配额）。
    """
    global _DATA_DIR_WRITABLE
    if _DATA_DIR_WRITABLE is not None:
        return _DATA_DIR_WRITABLE
    try:
        probe = DATA_DIR / f".oc_write_probe_{os.getpid()}"
        probe.write_bytes(b"")
        try:
            probe.unlink()
        except OSError:
            pass
        _DATA_DIR_WRITABLE = True
    except OSError:
        _DATA_DIR_WRITABLE = False
    except SystemExit:
        # 宿主的删除保护钩子会抛 SystemExit（不是 Exception 的子类）。
        # 探针删除受阻 ≠ 数据目录不可写（写入本身成功了），按可写处理，
        # 否则一个安全钩子就能让整个 /state 面板失效。
        _DATA_DIR_WRITABLE = True
    return _DATA_DIR_WRITABLE


def human(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"


def expand(path: str, drive: str | None = None) -> str:
    """展开环境变量与盘符占位符。drive 形如 'C:' 或 'C'。"""
    out = str(path)
    if drive:
        letter = drive.rstrip(":").upper()
        out = out.replace("%DRIVE_LETTER%", letter)
        out = out.replace("%DRIVE%", letter + ":")
    return os.path.expandvars(os.path.expanduser(out))


def is_drive_specific(target: dict) -> bool:
    """路径里出现 %DRIVE% / %DRIVE_LETTER% 才算“所选磁盘专属项”。"""
    blob = f"{target.get('path', '')}|{target.get('glob', '')}|{target.get('drive_letter', '')}"
    return "%DRIVE%" in blob


def target_id_for(target: dict, drive: str | None) -> str:
    """盘符专属项的 id 加盘符后缀，避免多盘之间 id 冲突。"""
    tid = target["id"]
    if drive and is_drive_specific(target):
        return f"{tid}@{drive.rstrip(':').upper()}"
    return tid

def _kill_ps_tree(p) -> None:
    """结束 PowerShell 进程及其子孙，并**保证收尾动作有上限**。

    为什么不用 subprocess.run(timeout=...) 自带的超时：
    在 Windows 上它超时后会先 kill()，再调一次 communicate() 去收输出。
    如果被结束的进程有残留子进程仍握着 stdout 管道，这次 communicate()
    会一直等下去（管道没有 EOF），表现为「扫描永远停在某一阶段、进度条不再变化」。
    这里改成显式结束整棵进程树，再给 5 秒收尾 —— 拿不到输出也照样返回，
    调用方一定能拿到一个结论，而不是挂住。
    """
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, timeout=10,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
    try:
        p.kill()
    except Exception:
        pass
    try:
        p.communicate(timeout=5)
    except Exception:
        pass


def run_ps(script: str, timeout: int = 300):
    """执行一段 PowerShell，原样返回其输出的 JSON，不做类型强制。

    输出单个对象是 dict、数组是 list；调用方取字段前先确认类型，
    或用 _as_list() 归一化。超时路径见 _kill_ps_tree()，保证一定返回。
    """
    cmd = ["powershell.exe", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-Command", PS_UTF8_PREAMBLE + script]
    env = dict(os.environ)
    # 固定子进程输出编码，不依赖父控制台的 chcp，否则报错信息会乱码
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs: dict = {"env": env, "stdout": subprocess.PIPE,
                    "stderr": subprocess.PIPE}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    p = subprocess.Popen(cmd, **kwargs)
    try:
        raw_out, raw_err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_ps_tree(p)
        return {"error": f"PowerShell 执行超时（{timeout} 秒）"}
    out = (raw_out or b"").decode("utf-8", "replace").strip()
    if not out:
        err = (raw_err or b"").decode("utf-8", "replace").strip()
        err = err.replace("\n", " ")[:400]
        return {"error": f"PowerShell 无输出（exit={p.returncode}）：{err}"}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"error": f"PowerShell 输出不是合法 JSON：{out[:300]}"}


def list_drives() -> list[dict]:
    """枚举可用盘符及容量。"""
    out = []
    if not IS_WINDOWS:
        return out
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if not os.path.exists(root):
            continue
        try:
            total, used, free = shutil.disk_usage(root)
        except OSError:
            continue
        if total <= 0:
            continue
        out.append({
            "letter": letter, "root": root,
            "total": total, "used": used, "free": free,
            "total_h": human(total), "used_h": human(used), "free_h": human(free),
            "percent": round(used / total * 100, 1),
            # 前端据此决定「清理范围」默认值：系统盘看全部，数据盘只看本盘
            "is_system": is_system_drive(letter),
        })
    return out


def get_disk_info(drive: str | None = None) -> dict:
    letter = (drive or "C").rstrip(":").upper()
    root = f"{letter}:\\"
    if IS_WINDOWS and not os.path.exists(root):
        return {"error": f"盘符 {letter}: 不存在"}
    target = root if IS_WINDOWS else "/"
    try:
        total, used, free = shutil.disk_usage(target)
        info = {"letter": letter, "total": total, "used": used, "free": free,
                "total_h": human(total), "used_h": human(used), "free_h": human(free),
                "percent": round(used / total * 100, 1) if total else 0,
                # 前端据此决定「清理范围」默认值：系统盘看全部，数据盘只看本盘
                "is_system": is_system_drive(letter)}
        record_disk_snapshot(info)
        return info
    except Exception as exc:
        return {"error": str(exc)}


def record_disk_snapshot(info: dict) -> None:
    """把各盘用量记进趋势快照：每天每盘一条，超过 90 天的丢掉。

    挂在 get_disk_info 的成功路径上。同一天同盘再查时，只有「可用空间变了」
    才更新（比如刚做完清理）—— 否则一天首次的值就是冻结值，趋势反映不出
    清理效果；而可用空间天然随时在变，所以平时不会反复写盘。"""
    if info.get("error") or not info.get("total"):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    hist = _load_json(DISK_TREND_PATH, [], use_cache=False)
    if not isinstance(hist, list):
        hist = []
    for day in hist:
        if isinstance(day, dict) and day.get("d") == today:
            drives = day.setdefault("u", {})
            prev = drives.get(info["letter"])
            if prev and prev.get("free") == info["free"]:
                return                      # 今天记过且空间没变化
            drives[info["letter"]] = {"total": info["total"], "free": info["free"]}
            _atomic_write(DISK_TREND_PATH, hist)
            return
    hist.append({"d": today, "u": {info["letter"]: {"total": info["total"],
                                                    "free": info["free"]}}})
    _atomic_write(DISK_TREND_PATH, hist[-90:])


# ======================================================================
#  PowerShell 测量脚本
#  注意：用 .format() 注入路径，脚本里所有字面量花括号必须写成双花括号
# ======================================================================

# 注意：这些脚本用 str.replace() 注入路径，占位符写成 __PATH__ / __GLOB__ / __DRIVE__。
# 故意不用 .format()：那会要求把 PowerShell 里所有字面量花括号都写成 {{ }}，
# 极易漏改且报错信息晦涩（已经踩过两次）。

def _json_items(raw) -> list[dict]:
    if isinstance(raw, dict):
        raw = [raw]
    out = []
    for i in (raw or []):
        try:
            out.append({"name": i.get("name", ""), "size": int(i.get("size") or 0),
                        "path": i.get("path", "")})
        except Exception:
            continue
    return out


def measure_target(t: dict, drive: str | None = None) -> dict:
    """返回 {size, files, [items], [missing], [error]}"""
    try:
        path = expand(t["path"], drive)
        if not os.path.isdir(path):
            return {"size": 0, "files": 0, "missing": True}
        return _measure_existing(t, path, drive)
    except PermissionError:
        return {"size": 0, "files": 0, "error": "拒绝访问（权限不足或受系统保护）"}
    except Exception as exc:  # noqa: BLE001
        return {"size": 0, "files": 0, "error": f"{type(exc).__name__}: {exc}"}


def _measure_existing(t: dict, path: str, drive: str | None) -> dict:
    kind = t.get("kind", "tree")
    if kind == "recyclebin":
        letter = expand(str(t.get("drive_letter") or ""), drive).rstrip(":") \
            or (drive or "").rstrip(":")
        r = run_ps(PS_RECYCLE.replace("__DRIVE__", letter.replace("'", "''")))
        if "error" in r:
            return {"size": 0, "files": 0, "error": r["error"]}
        if r.get("denied"):
            return {"size": 0, "files": 0,
                    "error": "读取被拒绝（回收站需要管理员权限）；以管理员身份运行后可正常统计"}
        return {"size": int(r.get("size") or 0), "files": int(r.get("files") or 0),
                "items": _json_items(r.get("items"))}

    if kind == "files":
        script = (PS_MEASURE_GLOB
                  .replace("__PATH__", path.replace("'", "''"))
                  .replace("__GLOB__", str(t.get("glob", "*")).replace("'", "''")))
    else:
        script = PS_MEASURE_TREE.replace("__PATH__", path.replace("'", "''"))
    r = run_ps(script)
    if r.get("denied"):
        hint = "需要管理员权限" if t.get("needs_admin") else "权限不足，无法读取"
        return {"size": 0, "files": 0,
                "error": f"读取被拒绝（{hint}）；以管理员身份运行后可正常统计"}
    return {"size": int(r.get("size") or 0), "files": int(r.get("files") or 0)}


# ======================================================================
#  全盘占用树（支持逐层下钻）
# ======================================================================

# 只有达到这些门槛的目录才会被继续展开，否则视为叶子。
# 目的：既能挖到 …\steamapps\common\某款大型游戏（上百 GB）
# 这种深层大项，又不会把几十万个小目录全爬一遍。
#
# 展开门槛用「相对父目录的比例」而非固定值：固定阈值会让一个 500 GB 的盘
# 在所有大于阈值的分支上都做一次全量遍历，几分钟跑不完。
# 取 max(绝对下限, 父目录的 8%)，既控制总耗时，又能沿最粗的分支一路挖到底。
DISK_TREE_MIN_DESCEND_RATIO = 0.08
DISK_TREE_MIN_DESCEND_FLOOR = 300 * 1024 * 1024    # 绝对下限 300 MB
DISK_TREE_MIN_CHILD_MB = 50                         # 子项 ≥50 MB 才进入该层清单
DISK_TREE_MAX_DEPTH = 2                             # 默认展开 2 层（2026-10-14 起；
                                                    # 前端可在 1~5 层间自选，这里是默认值与上限参考）
DISK_TREE_MAX_NODES = 420                           # 总节点上限，控制耗时与数据量
DISK_TREE_MAX_PER_DIR = 40                          # 单个目录最多列出的子项数
# 每层最多沿几条最粗的分支继续深入。这是扫描耗时的主要开关：
# 放大会更全面但更慢（每个深分支都要把整棵子树量一遍）。
DISK_TREE_MAX_DESCENTS = 4

# ======================================================================
#  WizTree 加速：借它的 MFT 读取，把全盘扫描从几分钟压到几十秒
# ======================================================================

# 为什么要接 WizTree 而不是自己读 MFT：
#   * 实测 C 盘 Get-ChildItem 递归要 258 秒，E 盘 51 秒，D 盘 33 秒；
#     而 WizTree 用 /export 导出同盘明细：C 盘 29 秒、D 盘几秒（快 9 倍）。
#   * 自己读 NTFS MFT 需要管理员权限、要处理 USN 日志与碎片，
#     解析出错就是错报体积，风险与收益完全不成比例。
#   * WizTree 只在**用户已经装了它**时才被调用，我们不做任何安装行为。
#
# 它对本项目的第二个价值更大：导出的 CSV 含**每一个文件的完整路径**，
# 于是「任意目录逐层拆分子项」变成一次 CSV 解析 + 内存查表，
# 不必再为下钻去扫盘。这也正是用户要的「大清理项点开看二级目录」。
WIZTREE_NAMES = ("WizTree64.exe", "WizTree.exe")
WIZTREE_HINTS = (
    r"C:\Program Files\WizTree",
    r"C:\Program Files (x86)\WizTree",
)

# 官方下载地址（diskanalyzer.com，Antibody Software Limited）。
# 优先用**便携版 zip**：免安装、不需要管理员权限，解压到本目录后整个
# O-Cleaner 文件夹拷到别的电脑也能直接用 —— 这正是「可移动」的关键。
WIZTREE_PORTABLE_URL = "https://diskanalyzer.com/files/wiztree_4_33_portable.zip"
WIZTREE_SETUP_URL = "https://diskanalyzer.com/files/wiztree_4_33_setup.exe"
WIZTREE_HOME = "https://diskanalyzer.com/download"
WIZTREE_DIST_LICENSE = "https://diskanalyzer.com/distribution-license"
# 第三方许可说明。**必须如实告知**：它决定了能不能把 WizTree 随包分发。
WIZTREE_LICENSE_NOTE = (
    "WizTree 是 Antibody Software Limited 的第三方软件，个人使用免费。"
    "本项目不随包分发它 —— 下载与解压都发生在你自己这台机器上。"
    "若要把 WizTree 随你的产品一起分发，须先向官方购买 Distribution License。"
)
# 便携版解压到这里（相对 O-Cleaner 根目录，跟着文件夹走）
WIZTREE_PORTABLE_DIR = BASE_DIR / "portable" / "WizTree"

def find_wiztree() -> str | None:
    """找 WizTree 可执行文件。返回完整路径或 None（没装就静默走原路径）。

    查找顺序（**先看本目录自带的便携版**，这样拷到别的电脑能立刻用）：
      1. O-Cleaner\\portable\\WizTree\\ —— 自动下载或用户手工放的便携版，跟着文件夹走
      2. 常见安装位置
      3. 注册表卸载项记录的 InstallLocation
      4. 控制台 PATH 里能直接调到的 wiztree64

    任何一步失败都继续往下，不抛异常 —— 这是可选加速，
    找不到绝不能让主流程报错。
    """
    if not IS_WINDOWS:
        return None
    # 1) 本目录便携版优先：可移动性的关键
    for n in WIZTREE_NAMES:
        p = str(WIZTREE_PORTABLE_DIR / n)
        if os.path.isfile(p):
            return p
    # 2) 常见安装位置 + 各盘符根下的 \WizTree。逐盘探测而不是把某台
    #    机器的安装盘位写死进源码 —— 有人习惯把它装在数据盘根目录。
    drives = tuple(f"{c}:\\" for c in "CDEFGH")
    for d in (*WIZTREE_HINTS, *drives):
        for n in WIZTREE_NAMES:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    # 注册表：读取卸载项里的 InstallLocation（WizTree 会写这个键）
    try:
        import winreg  # type: ignore
        keys = [
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        ]
        for hive, sub in keys:
            try:
                with winreg.OpenKey(hive, sub) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            name = winreg.EnumKey(root, i)
                            with winreg.OpenKey(root, name) as k:
                                disp = str(winreg.QueryValueEx(k, "DisplayName")[0])
                                if "wiztree" not in disp.lower():
                                    continue
                                loc = str(winreg.QueryValueEx(k, "InstallLocation")[0])
                                for n in WIZTREE_NAMES:
                                    p = os.path.join(loc, n)
                                    if os.path.isfile(p):
                                        return p
                        except OSError:
                            continue
            except OSError:
                continue
    except Exception:
        pass
    # 最后试 PATH
    import shutil as _sh
    for n in WIZTREE_NAMES:
        p = _sh.which(n)
        if p:
            return p
    return None


def wiztree_available() -> dict:
    """给界面用的探测结果（缓存，避免每帧都查注册表）。"""
    global _WIZ_CACHE
    if _WIZ_CACHE is None:
        p = find_wiztree()
        _WIZ_CACHE = {"found": bool(p), "path": p or ""}
    return _WIZ_CACHE


def _wiz_ok() -> bool:
    """是否启用 WizTree 加速。可用环境变量 OC_NO_WIZTREE=1 关掉
    （自检与排查时要能强制走 PowerShell 路径做对照）。"""
    if os.environ.get("OC_NO_WIZTREE"):
        return False
    return bool(wiztree_available().get("found"))


def wiztree_status() -> dict:
    """给「新用户引导」用的完整状态：找到没有、是不是本目录自带、怎么装。"""
    wz = wiztree_available()
    path = str(wz.get("path") or "")
    portable = bool(path) and os.path.normpath(path).lower().startswith(
        os.path.normpath(str(WIZTREE_PORTABLE_DIR)).lower())
    ver = ""
    if path:
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath '{path.replace(chr(39), chr(39)*2)}')"
                 ".VersionInfo.FileVersion"],
                capture_output=True, timeout=20,
                creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
            ver = (out.stdout or b"").decode("utf-8", "replace").strip()
        except Exception:
            ver = ""
    return {
        "found": bool(path),
        "path": path,
        "portable": portable,               # 本目录自带 → 拷走就能用
        "version": ver,
        "enabled": _wiz_ok(),
        "portable_dir": str(WIZTREE_PORTABLE_DIR),
        "portable_rel": "portable\\WizTree",
        "download_url": WIZTREE_PORTABLE_URL,
        "setup_url": WIZTREE_SETUP_URL,
        "homepage": WIZTREE_HOME,
        "dist_license_url": WIZTREE_DIST_LICENSE,
        "license_note": WIZTREE_LICENSE_NOTE,
        "cached_drives": sorted(_WIZ_DIRS.keys()),
    }


def acquire_wiztree(timeout: int = 300) -> dict:
    """下载 WizTree **便携版**并解压到本目录，供这台或别的电脑直接使用。

    为什么用便携版 zip 而不是安装程序：
      * 免安装、不需要管理员权限；
      * 解压后就在 O-Cleaner\\portable\\WizTree\\ 里，整个文件夹拷到
        别的电脑（含 U 盘）即可直接用 —— 这是「移动可用」的关键；
      * 不写注册表、不装进 Program Files，卸载就是删文件夹。

    合法性：这是"用户在自己机器上下载并使用"，不是我们随包分发 WizTree。
    随包分发需要官方的 Distribution License，所以**安装包不会包含它**，
    界面上也会把这一点如实写明（见 WIZTREE_LICENSE_NOTE）。
    """
    import io
    import zipfile

    result = {"ok": False, "error": "", "path": "", "version": "",
              "portable_dir": str(WIZTREE_PORTABLE_DIR), "log": []}

    # 已经有了就直接返回，不重复下载
    if wiztree_available().get("found"):
        st = wiztree_status()
        result.update(ok=True, path=st["path"], version=st["version"],
                      already=True)
        result["log"].append("已经可用，无需下载")
        return result

    try:
        WIZTREE_PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result["error"] = f"无法创建目录 {WIZTREE_PORTABLE_DIR}：{e}"
        return result

    result["log"].append(f"从官方地址下载便携版：{WIZTREE_PORTABLE_URL}")
    try:
        req = _UrlRequest(
            WIZTREE_PORTABLE_URL, headers={"User-Agent": APP_UA})
        with _urlopen(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("Content-Type") or "")
            raw = resp.read()
    except Exception as e:
        result["error"] = (f"下载失败：{type(e).__name__}: {e}。"
                           f"可手动下载 {WIZTREE_PORTABLE_URL} 后把内容解压到 "
                           f"{WIZTREE_PORTABLE_DIR}")
        return result

    result["log"].append(f"下载完成 {human(len(raw))}")
    # 基本校验：必须是 zip（PK 头）且体积合理，避免把错误页面当成压缩包解开
    if not raw.startswith(b"PK\x03\x04"):
        result["error"] = (f"下载到的不是 zip（Content-Type={ctype}，"
                           f"前 4 字节={raw[:4]!r}），已中止且未写入任何文件")
        return result
    if len(raw) < 1_000_000:
        result["error"] = f"下载体积异常偏小（{human(len(raw))}），已中止"
        return result

    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            # 防目录穿越：压缩包里的路径不能含 .. 或绝对路径
            bad = [n for n in names
                   if n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/")]
            if bad:
                result["error"] = f"压缩包内含可疑路径，已中止：{bad[:3]}"
                return result
            zf.extractall(str(WIZTREE_PORTABLE_DIR))
        result["log"].append(f"解压 {len(names)} 个文件到 "
                             f"{WIZTREE_PORTABLE_DIR}")
    except Exception as e:
        result["error"] = f"解压失败：{type(e).__name__}: {e}"
        return result

    # 解压后重新探测（find_wiztree 会优先看这个便携目录）
    global _WIZ_CACHE
    _WIZ_CACHE = None
    st = wiztree_status()
    if not st["found"]:
        result["error"] = (f"解压完成但没找到 WizTree64.exe，"
                           f"请检查 {WIZTREE_PORTABLE_DIR} 的内容")
        return result
    result.update(ok=True, path=st["path"], version=st["version"])
    result["log"].append(f"已就绪：{st['path']}")
    return result


def _wiztree_cleanup(csv_path: str) -> bool:
    """删除 WizTree 导出的 CSV。返回是否删掉。

    导出文件可能上到一百多 MB（C 盘 77 万行），解析完就必须清掉。
    删除失败不是致命问题，但**绝不能因此抛异常**打断扫描 —— 表已经在内存里了。
    """
    if not csv_path:
        return False
    try:
        os.remove(csv_path)
        return True
    except OSError:
        return False


def cleanup_stale_wiztree_exports() -> int:
    """清理上次异常退出（崩溃 / 强杀）遗留的导出文件。

    为什么需要：正常路径已经在 finally 里删了，但如果进程被强杀，
    finally 不会执行，那些一百多 MB 的文件就会永远躺在临时目录里。
    启动时扫一遍把遗留清掉，成本可忽略（一次 glob）。
    """
    n = 0
    try:
        pat = re.compile(r"^oc-wiztree-[A-Z]-\d+\.csv$", re.I)
        for name in os.listdir(tempfile.gettempdir()):
            if not pat.match(name):
                continue
            try:
                os.remove(os.path.join(tempfile.gettempdir(), name))
                n += 1
            except OSError:
                continue
    except OSError:
        pass
    return n


def _wiztree_export(drive: str, timeout: int = 300) -> tuple[str | None, str]:
    """调用 WizTree 导出整盘明细 CSV。返回 (csv 路径, 错误信息)。

    命令行：WizTree64.exe <盘符> /export="<csv>" —— 该写法导出后
    WizTree 自动退出（exit=0），无需杀进程。
    """
    exe = (wiztree_available().get("path") or "")
    if not exe:
        return None, "未安装 WizTree"
    letter = (drive or "C").rstrip(":").upper() + ":"
    csv_path = os.path.join(tempfile.gettempdir(),
                            f"oc-wiztree-{letter.rstrip(':')}-{os.getpid()}.csv")
    try:
        if os.path.exists(csv_path):
            os.remove(csv_path)
    except OSError:
        pass
    cmd = [exe, letter + "\\", f'/export={csv_path}']
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        return None, f"WizTree 导出超时（{timeout} 秒）"
    except OSError as e:
        return None, f"WizTree 启动失败：{e}"
    if not os.path.isfile(csv_path):
        err = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return None, f"WizTree 未生成导出文件（exit={p.returncode}）{err}"
    return csv_path, ""


def _wiztree_direct_prefix(path: str) -> tuple[str, str, int]:
    """「直接子目录」判定的公共参数：规范化键、子目录前缀、目标层数。"""
    key = os.path.normpath(path).lower()
    pre = key if key.endswith("\\") else key + "\\"
    return key, pre, pre.count("\\")


def _wiztree_children(dirs: dict[str, int], path: str, limit: int = 60) -> list[dict]:
    """从目录体积表里取出某个目录的**直接**子目录（按体积降序）。

    判「直接子目录」用路径里的反斜杠个数，比 endswith/startswith 组合可靠：
    只比较层数就不会把 D:\\a\\bc 误当成 D:\\a\\b 的子项。
    """
    key, pre, depth = _wiztree_direct_prefix(path)
    out = []
    for p, sz in dirs.items():
        if p == key or not p.startswith(pre):
            continue
        if p.count("\\") != depth:
            continue
        out.append({"path": p, "name": p.rstrip("\\").split("\\")[-1],
                    "size": sz, "type": "dir"})
    out.sort(key=lambda x: -x["size"])
    return out[:limit]


def _dir_has_any_child(path: str) -> bool:
    """廉价探测目录里是否有任何条目（取到第一个就返回）。

    无 WizTree 内存表时无法零成本预判「能否展开」，全量统计体积又太贵；
    一次性 scandir 只花一次系统调用，整棵树几百个节点实测也就几十毫秒。
    权限不足/路径消失一律按「不可展开」处理，绝不让探测本身抛异常。
    """
    try:
        with os.scandir(path) as it:
            for _ in it:
                return True
    except OSError:
        return False
    return False


def _wiztree_parse(csv_path: str, on_progress=None) -> dict:
    """解析 WizTree CSV，返回 {"dirs": {路径:体积}, "files": {父目录:[(名,大小)]}, "rows": n}。

    为什么要连「直接文件」也存下来：原来那套展开逻辑每层会同时列出**子目录与大文件**
    （`hasChildren` 与文件条目混在同一层），只看目录会让用户觉得「文件不见了」。
    WizTree 的 CSV 正好把两者都给全了，顺手存下来即可完全复刻原行为。

    只保留达到体积门槛的条目，避免把 C 盘 77 万行全部留在内存里。

    on_progress(frac) 是可选回调，按读到的字节数报进度（0~1）。
    这一步在 C 盘上会读上百 MB，是整条链路里最长的纯 CPU 阶段，
    不报进度的话界面只能一直显示「解析中」。
    """
    dirs: dict[str, int] = {}
    files: dict[str, list] = {}
    floor = max(0, int(DISK_TREE_MIN_CHILD_MB)) * 1024 * 1024
    n = 0
    try:
        total_bytes = max(1, os.path.getsize(csv_path))
    except OSError:
        total_bytes = 1
    with open(csv_path, "r", encoding="utf-8-sig", errors="replace") as fh:
        fh.readline()                  # WizTree 版本与捐赠提示
        fh.readline()                  # 列名
        for line in fh:
            n += 1
            if on_progress and n % 20000 == 0:
                try:
                    on_progress(min(1.0, fh.tell() / total_bytes))
                except Exception:
                    pass           # 报进度失败不影响解析
            # 路径一定被双引号包住，所以按「第一个引号闭合」切分比按逗号切分安全
            # （路径里可以含逗号，按逗号切会把路径截断）。
            if not line.startswith('"'):
                continue
            end = line.find('"', 1)
            if end <= 1:
                continue
            path = line[1:end]
            parts = line[end + 1:].lstrip(",").split(",", 1)
            if not parts:
                continue
            try:
                size = int(parts[0])
            except (ValueError, TypeError):
                continue
            low = os.path.normpath(path).lower()
            if path.endswith("\\"):
                # 盘根第一层目录无条件保留：资源管理器里看得到的每一项
                # 都必须出现在顶层视图里。此前 System Volume Information、
                # Config.Msi 这类小体积隐藏项被门槛整个挡掉，
                # 「扫出来的比资源管理器少几项」就是这么来的。
                # 深层子目录仍按体积门槛裁剪，内存不受影响。
                if size >= floor or low.count("\\") <= 1:
                    dirs[low] = size
            elif size >= floor:
                parent = os.path.dirname(low)
                files.setdefault(parent, []).append(
                    (path.rstrip("\\").split("\\")[-1], size, path))
    if on_progress:
        try:
            on_progress(1.0)
        except Exception:
            pass
    return {"dirs": dirs, "files": files, "rows": n}


# 默认隐藏的系统项：资源管理器勾着「隐藏受保护的操作系统文件」时不显示，
# 但 MFT/完整递归都能扫到。前端把这些聚成一个可展开的独立项，
# 避免「扫出来的和资源管理器看到的不一样」让用户困惑。
# 除目录外还收 NTFS 的根级元文件（$MFT / $LogFile / $Extend 等）：
# 它们是文件系统自身的记账数据，只存在于盘根，用户删不了也不该删。
_HIDDEN_SYS_NAMES = {
    "$recycle.bin", "recycler", "system volume information", "config.msi",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "dumpstack.log", "dumpstack.log.tmp", "$winreagent",
    "$mft", "$mftmirr", "$logfile", "$volume", "$attrdef", "$bitmap",
    "$boot", "$badclus", "$secure", "$upcase", "$extend",
}


def _wiztree_raw_nodes(root: str, table: dict,
                       max_depth: int, min_child: int, ratio: float,
                       floor: int, max_per_dir: int, max_nodes: int,
                       max_descents: int) -> tuple[list[dict], list[dict], int]:
    """用 WizTree 的体积表复刻 PS_TREE_WALK 的展开策略，产出同一形状的原始节点。

    刻意**逐行对应** PowerShell 那版的逻辑（同样的 minChild 过滤、同样的
    「父目录体积 × ratio 且不低于 floor」下探门槛、同样的每层取前 maxPerDir、
    同样的每条分支最多下探 maxDescents 个、同样的总节点预算），
    否则两条扫描路径会给出不同的树，界面表现就不一致了。
    区别只在于：这里查的是内存表，不再递归遍历磁盘。
    """
    dirs = table["dirs"]
    files = table["files"]
    out: list[dict] = []
    budget = max_nodes
    root_key = os.path.normpath(root).lower()

    def min_descend(parent_size: int) -> int:
        d = int(parent_size * ratio)
        return max(int(floor), min(d, 4_000_000_000))

    def kids_of(path_key: str) -> list[dict]:
        """某一层的候选子项（目录 + 文件），按体积降序取前 max_per_dir。"""
        pre = path_key if path_key.endswith("\\") else path_key + "\\"
        depth = pre.count("\\")
        items = []
        for p, sz in dirs.items():
            if p == path_key or not p.startswith(pre):
                continue
            if p.count("\\") != depth:
                continue
            if sz < min_child:
                continue
            items.append({"type": "dir", "name": p.rstrip("\\").split("\\")[-1],
                          "path": p, "size": sz, "modified": ""})
        for name, sz, full in files.get(path_key, ()):
            if sz >= min_child:
                items.append({"type": "file", "name": name, "path": full.lower(),
                              "size": sz, "modified": ""})
        items.sort(key=lambda x: -x["size"])
        return items[:max_per_dir]

    def walk(path_key: str, depth: int, parent_key: str, self_size: int) -> None:
        nonlocal budget
        if depth > max_depth or budget <= 0:
            return
        md = min_descend(self_size)
        descended = 0
        for k in kids_of(path_key):
            if budget <= 0:
                break
            budget -= 1
            has_kids = False
            if (k["type"] == "dir" and k["size"] >= md and depth < max_depth
                    and descended < max_descents and budget > 0):
                descended += 1
                has_kids = True
            out.append({"parent": parent_key, "name": k["name"], "path": k["path"],
                        "type": k["type"], "size": k["size"], "depth": depth,
                        "modified": k["modified"], "hasChildren": has_kids})
            if has_kids:
                walk(k["path"], depth + 1, k["path"], k["size"])

    # 顶层：盘符根的直接子目录（原版对第一层不做 minChild 过滤）
    tops = []
    pre = root_key if root_key.endswith("\\") else root_key + "\\"
    depth = pre.count("\\")
    for p, sz in dirs.items():
        if p == root_key or not p.startswith(pre) or p.count("\\") != depth:
            continue
        tops.append({"name": p.rstrip("\\").split("\\")[-1], "path": p, "size": sz})
    tops.sort(key=lambda x: -x["size"])
    total = sum(t["size"] for t in tops)

    for t in tops:
        if budget <= 0:
            break
        budget -= 1
        has_kids = t["size"] >= min_descend(total) and budget > 0
        out.append({"parent": root_key, "name": t["name"], "path": t["path"],
                    "type": "dir", "size": t["size"], "depth": 1,
                    "modified": "", "hasChildren": has_kids})
        if has_kids:
            walk(t["path"], 2, t["path"], t["size"])

    # 盘符根下的大文件（原版同样单独列在根层）；
    # 隐藏系统文件（pagefile/hiberfil/DumpStack 等）不受门槛限制，保证聚合组完整
    rootfiles = []
    for name, sz, full in files.get(root_key, ()):
        if sz >= min_child or name.lower() in _HIDDEN_SYS_NAMES:
            rootfiles.append({"name": name, "path": full.lower(),
                              "size": sz, "modified": ""})
    return out, rootfiles, total


# ======================================================================
#  扫描进度
# ======================================================================
# 界面上的进度条必须对应**真实发生的阶段**：纯按时间推算的百分比
# （如 `1 - exp(-已用秒数/90)`）与扫描实际进度无关，无法作为等待依据。
# 只记录真实阶段：
#   * 有确定分母的（已解析行数 / 已建节点数 / 已用预算）给精确百分比；
#   * 没有分母的（WizTree 读 MFT、PowerShell 递归）标 indeterminate，
#     界面画成流动条纹 + 显示当前阶段文案，不伪造百分比。
_SCAN_PROG: dict = {"active": False}
_PROG_LOCK = threading.Lock()


def prog_start(drive: str) -> None:
    with _PROG_LOCK:
        _SCAN_PROG.clear()
        _SCAN_PROG.update({"active": True, "drive": drive, "phase": "prepare",
                           "text": "正在检查磁盘与配置", "pct": 2.0,
                           "indeterminate": False, "t0": time.time()})


def prog_set(phase: str, text: str, pct: float,
             indeterminate: bool = False) -> None:
    """更新当前阶段。pct 是**整体**进度（0-100），不是阶段内进度。"""
    with _PROG_LOCK:
        if not _SCAN_PROG.get("active"):
            return
        _SCAN_PROG.update({"phase": phase, "text": text,
                           "pct": max(0.0, min(100.0, float(pct))),
                           "indeterminate": bool(indeterminate),
                           "at": time.time()})


def prog_done(ok: bool, text: str = "") -> None:
    with _PROG_LOCK:
        _SCAN_PROG.update({"active": False, "phase": "done" if ok else "error",
                           "text": text or ("扫描完成" if ok else "扫描失败"),
                           "pct": 100.0 if ok else _SCAN_PROG.get("pct", 0.0),
                           "indeterminate": False, "at": time.time()})


def prog_snapshot() -> dict:
    with _PROG_LOCK:
        d = dict(_SCAN_PROG)
    if d.get("active"):
        d["elapsed"] = round(time.time() - float(d.get("t0") or time.time()), 1)
    return d


def _watch_walk_progress(prog_file: str, stop: threading.Event,
                         lo: float, hi: float) -> None:
    """轮询 PowerShell 写出的进度文件，把「已处理节点 / 预算」换算成整体百分比。

    PowerShell 递归那条路只有一次阻塞调用（run_ps），拿不到中途输出；
    于是让脚本自己按固定间隔把计数写进一个临时文件，Python 侧每 0.5 秒读一次。
    读不到就什么都不做 —— 进度是附加信息，绝不能因为它出错而影响扫描本身。
    """
    while not stop.wait(0.5):
        try:
            with open(prog_file, "r", encoding="utf-8", errors="replace") as fh:
                body = fh.read().strip()
            done_s, _, total_s = body.partition("/")
            frac = float(done_s) / max(1.0, float(total_s))
            frac = min(1.0, max(0.0, frac))
            prog_set("walk",
                     f"正在递归统计目录体积（节点预算已用 {int(frac * 100)}%）",
                     lo + (hi - lo) * frac)
        except Exception:
            pass


def _scan_overview_reported(drive: str, max_depth: int) -> dict:
    """带进度上报的扫描主体。真正的实现在 _scan_overview_impl()。

    拆出来是为了让每条 return 路径（包括报错早退）都能落到 prog_done()，
    否则一次失败的扫描会让进度条永远停在半路。
    """
    letter = (drive or "C").rstrip(":").upper()
    prog_start(letter)
    try:
        r = _scan_overview_impl(drive, max_depth)
    except Exception as e:
        prog_done(False, f"扫描异常：{e}")
        raise
    if r.get("error"):
        prog_done(False, r["error"])
    else:
        prog_done(True, f"扫描完成：{r.get('node_count', 0)} 个节点")
    return r


# 同盘全盘扫描的并发闸门。为什么必须串行：浏览器对耗时几十秒的请求可能内部
# 静默重试（原连接异常断开时），用户也可能在扫描中快速换盘再换回来 —— 于是
# 同盘的两个扫描并发跑。两条路径会共用同一个 WizTree 导出 CSV 路径互相干扰，
# 输掉的那条退化到 PowerShell 全量递归（系统盘 4 分钟起步），请求迟迟不返回。
# 处理：同盘同时只跑一份；后来者等它结束直接复用结果，不发第二份扫描。
_OVERVIEW_LOCKS: dict = {}
_OVERVIEW_RESULTS: dict = {}
_OVERVIEW_REG_LOCK = threading.Lock()


def scan_disk_overview(drive: str, max_depth: int = DISK_TREE_MAX_DEPTH) -> dict:
    letter = (drive or "C").rstrip(":").upper()
    with _OVERVIEW_REG_LOCK:
        lock = _OVERVIEW_LOCKS.setdefault(letter, threading.Lock())
    if lock.acquire(blocking=False):
        try:
            r = _scan_overview_reported(drive, max_depth)
            if not r.get("error"):
                _OVERVIEW_RESULTS[letter] = r
            return r
        finally:
            lock.release()
    # 已有同盘扫描在跑：等它结束，复用刚扫出来的结果（等待期间进度条
    # 继续反映那一次扫描，前端轮询的是同一份快照）。
    with lock:
        cached = _OVERVIEW_RESULTS.get(letter)
    if cached is not None:
        return cached
    # 走到这里说明前一次扫描报错早退（没留结果）：自己重扫一遍。
    return _scan_overview_reported(drive, max_depth)


def _scan_overview_impl(drive: str, max_depth: int = DISK_TREE_MAX_DEPTH) -> dict:
    """磁盘概览：一次遍历生成整棵占用树，供界面逐层下钻。

    两条扫描路径，优先用快的那条：

    1. **WizTree**（若本机装了它）：调用 `WizTree64.exe <盘> /export=<csv>`，
       借它读 NTFS MFT 的能力拿全盘明细，「任意目录逐层拆子项」变成内存查表。
    2. **PowerShell 递归**（兜底）：整棵树在单个 PS 进程内递归完成。

    两条路径用同一套展开参数与同一套节点形状，界面表现一致。
    展开策略：沿「体积最粗的几条分支」逐层深入，
    才能挖到 …\\steamapps\\common\\某款大型游戏 这种第 4 层的大项。
    """
    letter = (drive or "C").rstrip(":").upper()
    root = letter + ":\\"
    if not IS_WINDOWS or not os.path.exists(root):
        return {"error": f"盘符 {letter}: 不存在", "nodes": []}

    depth_cap = max(1, min(max_depth, 6))
    engine = "powershell"
    table: dict | None = None

    if _wiz_ok():
        prog_set("export", f"正在用 WizTree 读取 {letter}: 盘的 MFT 明细", 10,
                 indeterminate=True)
        csv_path, werr = _wiztree_export(letter)
        if csv_path:
            try:
                t0 = time.time()
                table = _wiztree_parse(
                    csv_path,
                    on_progress=lambda frac: prog_set(
                        "parse", "正在解析 MFT 明细"
                                f"（{int(frac * 100)}%）", 14 + 56 * frac))
                parsed = time.time() - t0
                prog_set("tree", "正在按体积展开目录树", 72)
                roots, rootfiles, total = _wiztree_raw_nodes(
                    root, table, depth_cap,
                    DISK_TREE_MIN_CHILD_MB * 1024 * 1024,
                    DISK_TREE_MIN_DESCEND_RATIO, DISK_TREE_MIN_DESCEND_FLOOR,
                    DISK_TREE_MAX_PER_DIR, DISK_TREE_MAX_NODES,
                    DISK_TREE_MAX_DESCENTS)
                r = {"root": root.lower(), "nodes": roots, "rootfiles": rootfiles,
                     "total": total}
                engine = "wiztree"
                # 表留在内存里，供「逐层拆子项」与后续下钻直接查
                with _lock:
                    _WIZ_DIRS[letter] = {"table": table, "at": time.time(),
                                         "parse_s": round(parsed, 2)}
            except Exception as e:
                table = None
                werr = f"WizTree 导出解析失败：{e}"
            finally:
                # 导出的 CSV 很大（C 盘约 120 MB，含 77 万行），
                # 表已经进内存了就必须删掉它 —— 否则一个「磁盘清理工具」
                # 每扫一次就往临时目录塞一百多 MB，自己变成垃圾制造者。
                # CSV 高达上百 MB，表进内存后必须立刻删，别让清理工具自己制造垃圾
                _wiztree_cleanup(csv_path)
        if engine != "wiztree":
            # 加速路径失败不影响可用性，静默回退到 PowerShell；失败原因记在 payload 里
            pass
    else:
        werr = "未安装 WizTree"

    if engine != "wiztree":
        # PowerShell 只有一次阻塞调用，中途进度靠脚本自己往临时文件里写计数
        prog_file = os.path.join(
            tempfile.gettempdir(), f"oc-prog-{os.getpid()}-{letter}.txt")
        try:
            if os.path.exists(prog_file):
                os.remove(prog_file)
        except OSError:
            prog_file = ""
        script = (PS_TREE_WALK
                  .replace("__ROOT__", root.replace("'", "''"))
                  .replace("__PROGFILE__", prog_file.replace("'", "''"))
                  .replace("__MINCHILD__", str(DISK_TREE_MIN_CHILD_MB * 1024 * 1024))
                  .replace("__RATIO__", repr(DISK_TREE_MIN_DESCEND_RATIO))
                  .replace("__FLOOR__", str(DISK_TREE_MIN_DESCEND_FLOOR))
                  .replace("__MAXDEPTH__", str(depth_cap))
                  .replace("__MAXPERDIR__", str(DISK_TREE_MAX_PER_DIR))
                  .replace("__MAXNODES__", str(DISK_TREE_MAX_NODES))
                  .replace("__MAXDESCENTS__", str(DISK_TREE_MAX_DESCENTS)))
        prog_set("walk", "正在递归统计目录体积（未装 WizTree，耗时更长）", 10)
        stop = threading.Event()
        watcher = threading.Thread(target=_watch_walk_progress,
                                   args=(prog_file, stop, 10.0, 72.0), daemon=True)
        if prog_file:
            watcher.start()
        try:
            r = run_ps(script, timeout=1800)
        finally:
            stop.set()
            if prog_file:
                try:
                    os.remove(prog_file)
                except OSError:
                    pass
        if "error" in r:
            return {"error": r["error"], "nodes": []}

    prog_set("steam", "正在读取 Steam 库清单", 76, indeterminate=True)
    # Steam 游戏整合进磁盘树：读一次 Steam 清单，得到各库的 steamapps\\common 路径。
    # 这样全盘占用树里能直接标出「这个目录是一个 Steam 游戏」，
    # 用户不必在「磁盘空间」和「Steam 页面」之间来回对照 —— 这正是本次的诉求。
    # discover_steam() 只读几 KB 清单，代价很小。（common/lib 集合由
    # steam_common_dirs() 统一提供，删除游戏的端点也用同一份做二次确认。）
    steam_common, steam_libs = steam_common_dirs()
    # Epic/GOG 游戏目录索引（带 60 秒缓存，清单级读取；冷启动要读多个库目录，
    # 耗时不可预知，所以这一小段同样标为不确定进度）
    prog_set("games", "正在读取 Epic / GOG 游戏清单", 78, indeterminate=True)
    try:
        other_game_dirs, other_game_libs = game_install_dirs()
    except Exception:
        other_game_dirs, other_game_libs = {}, set()
    # 用户自定义的行级显示覆盖（右键改徽章文字/下方说明）：
    # 键为归一化小写路径，整棵树只读一次，逐节点套用。
    try:
        _row_overrides = load_overrides().get("targets", {})
    except Exception:
        _row_overrides = {}

    def mk(n: dict, parent_default: str) -> dict:
        name = str(n.get("name") or "")
        path = str(n.get("path") or "")
        item = {
            "parent": str(n.get("parent") or parent_default).lower(),
            "name": name, "path": path,
            "depth": int(n.get("depth") or 1),
            "type": str(n.get("type") or "dir"),
            "size": int(n.get("size") or 0),
            "modified": str(n.get("modified") or ""),
            "has_children": bool(n.get("hasChildren")),
            # 默认隐藏的系统项标记：前端据此把同层这些项聚成一组
            "hidden_sys": name.lower() in _HIDDEN_SYS_NAMES,
        }
        info = identify(name, path, item["type"])
        info.pop("match_score", None)      # 内部打分不进 payload
        item.update(info)
        item["size_h"] = human(item["size"])
        # Steam 归属标记：位于某个 steamapps\common\ 正下方的就是游戏本体
        low = os.path.normpath(path).lower()
        parent_low = os.path.dirname(low)
        if parent_low in steam_common:
            item["steam_game"] = True
            item["rule_id"] = "steam-game"   # 内联分支伪 id：英文文案按它覆盖
            item["category"] = "游戏"
            # 游戏本体是「可清理项」：一台机器上最大的可回收空间往往就是
            # 几款没在玩的大作。允许删，但风险等级保持「高」—— 删掉的是
            # 整款游戏，且个别游戏的存档/Mod 也可能放在安装目录里。
            item["cleanable"] = True
            item["risk"] = "高"
            # 磁盘树里也用中文名，和 Steam 面板保持一致
            disp, zh = game_display_name(None, name)
            if zh:
                item["display_name"] = disp
                item["zh_name"] = zh
            item["what"] = "Steam 游戏本体：整个目录就是这款游戏的全部文件。"
            item["advice"] = (
                "没在玩的游戏可以整目录删除，一次就能回收几 GB 到上百 GB。"
                "更推荐在 Steam 客户端里右键游戏 → 管理 → 卸载（会同时清掉库记录）；"
                "直接删这个文件夹也能释放空间，之后 Steam 库里它会显示为「未安装」，"
                "想再玩重新下载即可。"
                "注意：少数游戏把存档、配置或 Mod 存在安装目录里"
                "（多数游戏存档其实在 文档\\My Games、AppData 或 Steam 云存档），"
                "删前确认没有想保留的东西。")
        elif low in steam_common or low in steam_libs:
            item["steam_library"] = True
            item["category"] = "游戏"
            if not item.get("what"):
                item["what"] = "Steam 游戏库的位置，下面是各款游戏的安装目录。"
        elif low in other_game_dirs:
            # Epic/GOG 游戏本体：按清单里的精确安装路径命中，宁缺毋滥
            plat = other_game_dirs[low]
            item["game_platform"] = plat
            item["rule_id"] = "epic-game" if plat == "epic" else "gog-game"
            item["category"] = "游戏"
            # 与 Steam 游戏本体同一待遇：可清理，但给出完整中文说明
            item["cleanable"] = True
            item["risk"] = "高"
            disp, zh = game_display_name(None, name)
            if zh:
                item["display_name"] = disp
                item["zh_name"] = zh
            item["what"] = (f"{'Epic Games' if plat == 'epic' else 'GOG'}"
                            "安装的游戏本体：整个目录就是这款游戏的全部文件。")
            item["advice"] = (
                "没在玩的游戏可以整目录删除。"
                + ("更推荐在 Epic 启动器里删除（设置 → 管理），会一并清理记录。"
                   if plat == "epic" else
                   "更推荐用 GOG Galaxy 卸载，会一并清理注册表记录。")
                + "直接删文件夹也能释放空间。注意：GOG/老游戏常把存档放在"
                  "安装目录或 文档\\My Games 里，删前确认没有想保留的存档。")
        elif low in other_game_libs:
            item["game_library"] = True
            item["rule_id"] = "other-game-lib"
            item["category"] = "游戏"
            if not item.get("what"):
                item["what"] = "Epic/GOG 游戏库目录，下面是各款游戏的安装目录。"
        # 用户行级覆盖（右键自定义）：徽章文字与下方说明，用户是最后一手
        ov = _row_overrides.get(low)
        if ov:
            if ov.get("label"):
                item["badge_label"] = ov["label"]
                item["custom"] = True
            if ov.get("note"):
                item["title"] = ov["note"]
                item["note_override"] = True
                item["custom"] = True
            if ov.get("pin"):
                item["pinned"] = True
            # 标签样式/颜色（2026-10-14）：纯显示覆盖，换徽章样式与柱条颜色
            if ov.get("label_kind") in ("clean", "unk", "keep"):
                item["label_kind"] = ov["label_kind"]
                item["custom"] = True
            if isinstance(ov.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", ov["color"]):
                item["custom_color"] = ov["color"]
                item["custom"] = True
        return item

    root_key = str(r.get("root") or root).lower()
    nodes: list[dict] = []
    raw_nodes = _as_list(r.get("nodes"))
    prog_set("identify", "正在逐项识别与标注", 84)
    for i, n in enumerate(raw_nodes):
        # 节点数从几百到几千不等，按批次报进度就够，不必每个都更新
        if i and i % 200 == 0:
            prog_set("identify",
                     f"正在逐项识别与标注（{i}/{len(raw_nodes)}）",
                     84 + 10 * (i / max(1, len(raw_nodes))))
        try:
            nodes.append(mk(n, root_key))
        except Exception:
            continue
    for f in _as_list(r.get("rootfiles")):
        try:
            nodes.append(mk({**f, "type": "file", "depth": 1}, root_key))
        except Exception:
            continue
    for i, n in enumerate(nodes):
        n["id"] = i

    # 快速扫描只展开 2 层，游戏本体（常见在 steamapps\common\ 下第 4 层）会被
    # 挡在树外 —— 而它们恰恰是用户最想整删的大项。把树里缺失的游戏本体补进
    # 来：挂到树内最深的现有祖先下，保证父子链可导航。有 WizTree 时查表
    # O(1)；无加速时限量实测（只补前 60 个），避免快速扫描反而变慢。
    if max_depth < 4:
        prog_set("inject", "补齐游戏本体节点", 88, indeterminate=True)
        have = {os.path.normpath(n["path"]).lower() for n in nodes if n.get("path")}
        wiz_tbl = _wiz_table(letter)
        wiz_dirs_tbl = wiz_tbl.get("dirs") if engine == "wiztree" else None
        cands: list[str] = []
        for common in steam_common:
            c = os.path.normpath(common)
            if os.path.splitdrive(c)[0].rstrip(":").upper() != letter:
                continue
            if wiz_dirs_tbl is not None:
                cands += [ch["path"] for ch in
                          _wiztree_children(wiz_dirs_tbl, common, limit=120)]
            else:
                try:
                    with os.scandir(common) as it:
                        cands += [e.path for e in it
                                  if e.is_dir(follow_symlinks=False)]
                except OSError:
                    pass
        cands += [os.path.normpath(p) for p in other_game_dirs]
        injected = 0
        for cand in cands:
            low = os.path.normpath(cand).lower()
            if low in have or injected >= 60:
                continue
            if os.path.splitdrive(low)[0].rstrip(":").upper() != letter:
                continue
            if wiz_dirs_tbl is not None:
                size = int(wiz_tbl.get("dirs", {}).get(low) or 0)
            else:
                sz, _trunc = _size_of_dir_budgeted(cand)
                size = int(sz or 0)
            if size <= 0:
                continue
            best, best_len = None, 0
            for n in nodes:
                if n["type"] != "dir" or not n.get("path"):
                    continue
                np = os.path.normpath(n["path"]).lower()
                if np != low and low.startswith(np + "\\") and len(np) > best_len:
                    best, best_len = n, len(np)
            node = mk({"name": os.path.basename(cand.rstrip("\\") or cand),
                       "path": cand,
                       "parent": (best["path"] if best else root_key).lower(),
                       "depth": (best["depth"] + 1 if best else 1),
                       "type": "dir", "size": size,
                       "modified": "", "hasChildren": True}, root_key)
            node["can_drill"] = True
            node["injected"] = True
            nodes.append(node)
            have.add(low)
            injected += 1

    # 游戏本体整删不拆分：本体行出现在排行里时，它内部的子项不再单独成行
    #（同一项目整删；「压缩包 / download」这类可拆分目录不受影响，
    # 子项照常显示）。标记给前端过滤，同时供勾选去重参考。
    for g in [n for n in nodes if n.get("steam_game") or n.get("game_platform")]:
        pre = os.path.normpath(g["path"]).lower()
        pre = pre if pre.endswith("\\") else pre + "\\"
        for m in nodes:
            if m is g or m["type"] != "dir" or not m.get("path"):
                continue
            mp = os.path.normpath(m["path"]).lower()
            if mp.startswith(pre):
                m["in_game_body"] = g["path"]

    # 自定义提级（2026-10-13）：用户右键把某个深层子目录「提级」，它就固定
    # 出现在排行顶层，不受展开深度限制。标记存在 custom-overrides.json 的
    # pin 字段里，随迁移数据（bundle 导出/导入）一起走。目录已消失时静默
    # 跳过 —— 提级只是显示偏好，绝不该因为显示偏好让扫描报错。
    try:
        _pins = {k: v for k, v in (_row_overrides or {}).items()
                 if isinstance(v, dict) and v.get("pin")}
    except Exception:
        _pins = {}
    if _pins:
        wiz_tbl = _wiz_table(letter)
        wiz_dirs_tbl2 = wiz_tbl.get("dirs") if engine == "wiztree" else None
        have2 = {os.path.normpath(n["path"]).lower() for n in nodes if n.get("path")}
        pinned_added = 0
        for pkey in _pins:
            if pinned_added >= 40:
                break
            if not isinstance(pkey, str) or len(pkey) < 4:
                continue
            cand = os.path.normpath(pkey)
            if os.path.splitdrive(cand)[0].rstrip(":").upper() != letter:
                continue              # 别的盘的提级项，在那一盘的扫描里出现
            if not os.path.isdir(cand):
                continue              # 目录已被删除/移动：静默跳过
            low = cand.lower()
            if low in have2 or low == root_key or root_key.startswith(low + "\\"):
                continue              # 树里已有（mk 会带 pinned 标记）或无意义祖先
            if wiz_dirs_tbl2 is not None:
                size = int(wiz_tbl.get("dirs", {}).get(low) or 0)
                if size <= 0:
                    # WizTree 内存表只存 ≥50MB 的目录；提级项是用户点名要看的，
                    # 小目录也必须给出真实体积，查不到就现场量。
                    sz, _trunc = _size_of_dir_budgeted(cand)
                    size = int(sz or 0)
            else:
                sz, _trunc = _size_of_dir_budgeted(cand)
                size = int(sz or 0)
            node = mk({"name": os.path.basename(cand) or cand,
                       "path": cand, "parent": root_key, "depth": 1,
                       "type": "dir", "size": size, "modified": "",
                       "hasChildren": True}, root_key)
            node["can_drill"] = True
            node["pinned"] = True
            nodes.append(node)
            have2.add(low)
            pinned_added += 1

    # 把「这条树节点正好是某个清理目标」的对应关系直接标在节点上（target_id）：    # 占用排行里的可清理行靠它优先走「按目标 id 清理」，从而保留回收站等
    # 特殊目标的清理语义；对不上目标的可清理行才走按路径清理（服务端重校验）。
    # 用户级目标（%LOCALAPPDATA% 等）展开后在别的盘，匹配不上当前盘的节点，
    # 属预期 —— 它们仍由预设一键勾选（按 id）。
    try:
        # build_targets 已经把盘符专属项的 id 加好盘符后缀（recycle-bin@C），
        # 这里直接用 t["id"] —— 再过一遍 target_id_for 会变成 recycle-bin@C@C，
        # 前端拿这个 id 去 SCAN/clean 里都查不到，勾了也会被后端静默丢弃。
        _tg_paths = {
            os.path.normpath(expand(t["path"], letter)).lower(): t["id"]
            for t in build_targets(letter)
        }
        for n in nodes:
            if n["type"] == "dir":
                tid = _tg_paths.get(str(n["path"]).lower())
                if tid:
                    n["target_id"] = tid
    except Exception as e:  # 标注失败不影响扫描本身
        print(f"[overview] 目标标注失败：{type(e).__name__}: {e}", file=sys.stderr)
    # 由数据决定「能否展开」，前端不必去猜 ——
    # has_children 表示已经在树里有子节点；此外还有「子项未入树但目录确实可展开」的情况。
    # 有 WizTree 时可以直接查内存表，比只靠树里有没有子节点更准：
    # 被体积门槛挡在树外的目录（<50 MB）照样应该允许点开看。
    wiz_dirs = _wiz_table(letter).get("dirs") \
        if engine == "wiztree" else None
    prog_set("finish", "正在整理可下钻信息", 96)
    parent_keys = {n["parent"] for n in nodes}
    for n in nodes:
        key = n["path"].lower()
        if n["type"] != "dir":
            n["can_drill"] = False
            continue
        if n.get("has_children") or key in parent_keys:
            n["can_drill"] = True
        elif wiz_dirs is not None:
            n["can_drill"] = bool(_wiztree_children(wiz_dirs, n["path"], 1))
        else:
            # 无 WizTree：退化为一次性 scandir 探测，让深层下钻在无加速环境也可用
            n["can_drill"] = _dir_has_any_child(n["path"])

    disk = get_disk_info(letter)
    return {
        "drive": letter,
        "root": root_key,
        "nodes": nodes,
        "explained": len([n for n in nodes if n.get("recognized")]),
        "node_count": len(nodes),
        "scanned_total": int(r.get("total") or 0),
        "scanned_total_h": human(int(r.get("total") or 0)),
        "min_child_h": human(DISK_TREE_MIN_CHILD_MB * 1024 * 1024),
        "max_depth": max_depth,
        "disk": disk,
        "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "is_admin": is_admin(),
        # 用了哪条扫描路径 —— 界面上要如实告诉用户「快是因为借了 WizTree」
        "engine": engine,
        "engine_label": ("WizTree MFT 加速" if engine == "wiztree"
                         else "PowerShell 递归（未安装 WizTree）"),
        "engine_note": (f"WizTree 导出 {table['rows']:,} 行，解析 "
                        f"{_WIZ_DIRS.get(letter, {}).get('parse_s', '?')} 秒"
                        if engine == "wiztree" else (werr or "")),
        # 结构化的行数/秒数：前端英文界面用它拼本地化文案（engine_note 是中文）
        "engine_rows": (table["rows"] if engine == "wiztree" else None),
        "engine_secs": (_WIZ_DIRS.get(letter, {}).get("parse_s")
                        if engine == "wiztree" else None),
        "subdir_ready": bool(engine == "wiztree"),
    }


def list_subdirs(drive: str, path: str, limit: int = 80) -> dict:
    """列出任意目录的直接子目录（含体积），用于「大清理项逐层拆分子项」。

    为什么需要单独一个接口，而不是让前端把整棵树都拿下去：
    C 盘有 77 万行文件记录，全量塞给浏览器既慢又浪费。
    WizTree 的导出表已经常驻内存，所以这里回答任意深度的下钻都是
    O(表大小) 的纯查表操作，实测 0 毫秒级 —— 深度完全不受树深上限约束。

    没有 WizTree（表不存在）时回退到现场测量直接子目录，仍然可用。
    """
    letter = (drive or "C").rstrip(":").upper()
    if not path:
        return {"error": "缺少 path 参数", "children": []}
    p = os.path.normpath(path)
    if not os.path.isdir(p):
        return {"error": "目录不存在", "children": []}

    children: list[dict] = []
    total = 0
    cached = _WIZ_DIRS.get(letter)
    if cached and cached.get("table"):
        table = cached["table"]
        children = _wiztree_children(table["dirs"], p, limit)
        # 该层真实子项总数（目录 + 直接文件）：前端要如实告知「只列出了前 N 个」
        key, pre, depth = _wiztree_direct_prefix(p)
        total = sum(1 for q in table["dirs"]
                    if q != key and q.startswith(pre) and q.count("\\") == depth)
        total += len(table["files"].get(key) or ())
        for c in children:
            cp = os.path.normpath(c["path"])
            try:
                c["modified"] = datetime.fromtimestamp(
                    os.path.getmtime(cp)).strftime("%Y-%m-%d")
            except OSError:
                c["modified"] = ""
        # 补上直接大文件，否则「文件去哪了」会让人困惑
        for name, sz, full in (table["files"].get(os.path.normpath(p).lower())
                               or ()):
            children.append({"path": full.lower(), "name": name, "size": sz,
                             "modified": "", "type": "file"})
        children.sort(key=lambda x: -x["size"])
        children = children[:limit]
        engine = "wiztree"
    else:
        engine = "powershell"
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            children.append({"path": e.path, "name": e.name,
                                             "size": _size_of_dir(e.path),
                                             "modified": datetime.fromtimestamp(
                                                 e.stat().st_mtime
                                             ).strftime("%Y-%m-%d"),
                                             "type": "dir"})
                        elif e.is_file(follow_symlinks=False):
                            st = e.stat()
                            children.append({"path": e.path, "name": e.name,
                                             "size": st.st_size,
                                             "modified": datetime.fromtimestamp(
                                                 st.st_mtime).strftime("%Y-%m-%d"),
                                             "type": "file"})
                    except OSError:
                        continue
        except OSError as e:
            return {"error": f"无法读取目录：{e}", "children": []}
        children.sort(key=lambda x: -x["size"])
        total = len(children)
        children = children[:limit]

    for c in children:
        c["size_h"] = human(c["size"])
        # 与占用树同一套标记：下钻视图里前端才能把同层隐藏项聚成一组
        c["hidden_sys"] = str(c.get("name") or "").lower() in _HIDDEN_SYS_NAMES
        info = identify(c["name"], c["path"], c.get("type") or "dir")
        info.pop("match_score", None)
        c.update(info)
        c["can_drill"] = bool(c.get("type") == "dir"
                              and (os.path.isdir(c["path"])
                                   or (cached and cached.get("table")
                                       and _wiztree_children(
                                           cached["table"]["dirs"], c["path"], 1))))
    # 自定义提级标注：下钻视图里也要能看到「这一项已提级到顶层」
    try:
        _pin_keys = {k for k, v in (load_overrides().get("targets") or {}).items()
                     if isinstance(v, dict) and v.get("pin")}
    except Exception:
        _pin_keys = set()
    if _pin_keys:
        for c in children:
            if os.path.normpath(str(c.get("path") or "")).lower() in _pin_keys:
                c["pinned"] = True
    # Epic/GOG 游戏标注：子项恰好是某游戏安装目录时给出与占用树一致的身份
    try:
        gdirs, glibs = game_install_dirs()
    except Exception:
        gdirs, glibs = {}, set()
    if gdirs or glibs:
        for c in children:
            if c.get("type") != "dir":
                continue
            clow = os.path.normpath(c["path"]).lower()
            if clow in gdirs:
                plat = gdirs[clow]
                c["game_platform"] = plat
                c["category"] = "游戏"
                c["cleanable"] = False
                c["risk"] = "高"
                if not c.get("what"):
                    c["what"] = ("Epic Games 安装的游戏本体。" if plat == "epic"
                                 else "GOG 安装的游戏本体。")
            elif clow in glibs:
                c["game_library"] = True
                c["category"] = "游戏"
    return {
        "drive": letter, "path": p, "children": children,
        "engine": engine,
        "total": total,
        "parent_size": (cached["table"]["dirs"].get(os.path.normpath(p).lower(), 0)
                        if cached and cached.get("table") else 0),
        "listed_total": sum(c["size"] for c in children),
        "listed_total_h": human(sum(c["size"] for c in children)),
    }

def _as_list(v) -> list:
    """把可能是单值/None/列表的 JSON 字段统一成列表。"""
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


# 指向「当前用户自己的目录」的路径标记。这类路径与盘符无关：
# %LOCALAPPDATA% 永远展开成 Windows 所在盘的用户目录，无论你选了哪个盘。
# 所以它们只能在系统盘上出现 —— 否则会出现极荒谬的结果：
# 选 D 盘时列出「D 盘的 pip 缓存」，而路径实际是 C:\Users\…\pip\Cache。
# 这正是用户反馈「D/E 盘老是冒出包管理器/浏览器/临时文件」的根因：
# 这些目标的 system_only 没标，于是每个盘都挂了一遍 C 盘的内容。
USER_PROFILE_TOKENS = ("%LOCALAPPDATA%", "%APPDATA%", "%USERPROFILE%", "%TEMP%")


def is_user_local(t: dict) -> bool:
    """目标是否指向「当前用户目录」（因而只能在系统盘上成立）。"""
    if t.get("local_only"):
        return True
    path = str(t.get("path") or "").upper()
    return any(tok in path for tok in USER_PROFILE_TOKENS)


def is_system_drive(letter: str) -> bool:
    """判断是否为装有 Windows 的系统盘。

    用于过滤掉其它盘上不存在的系统目标（例如不该在 D 盘列出 D:\\Windows\\Temp）。
    """
    if not IS_WINDOWS:
        return letter.upper() == "C"
    root = letter.rstrip(":").upper() + ":\\"
    return (os.path.isdir(os.path.join(root, "Windows", "System32"))
            or (os.path.isdir(os.path.join(root, "Windows"))
                and os.path.isdir(os.path.join(root, "Users"))))

# ======================================================================
#  配置加载
# ======================================================================

_cache: dict = {}
_lock = threading.Lock()
# WizTree 探测结果（None = 还没探测过）
_WIZ_CACHE: dict | None = None
# 最近一次 WizTree 导出的目录体积表：盘符 → {"dirs": {path:size}, "at": 时间戳}
# 有了它，「点开大项看二级目录」就只是内存查表，不必再扫盘。
# 表一直保留到同盘符的下一次扫描覆盖 —— 下钻靠它，过期驱逐反而会让
# 「隔几分钟再点开子项」退化成现场扫盘，不做 TTL。
_WIZ_DIRS: dict[str, dict] = {}


def _wiz_table(letter: str) -> dict:
    """取某盘符已缓存的 WizTree 体积表（可能为空 dict，调用方无需判 None）。"""
    return _WIZ_DIRS.get(letter, {}).get("table", {}) or {}


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


def load_targets() -> dict:
    return _load_json(TARGETS_PATH, {"presets": [], "targets": []})


def load_drives_cfg() -> dict:
    return _load_json(DRIVES_PATH, {"app": {}, "profiles": {}})


def load_convert() -> dict:
    return _load_json(CONVERT_PATH, {"display": {}, "units": []})


def load_filetypes() -> dict:
    return _load_json(FILETYPES_PATH, {"folder_rules": [], "file_rules": []})


def load_game_names() -> dict:
    """游戏中文名映射（appid → 中文名）。缺失时返回空表，不影响任何功能。"""
    return _load_json(GAMENAMES_PATH, {"games": {}})


def load_kb_en() -> dict:
    """内置知识库的英文文案。缺失/损坏时返回空表，全部回落中文。"""
    data = _load_json(KB_EN_PATH, {})
    return data if isinstance(data, dict) else {}


def apply_lang_payload(obj, lang: str):
    """英文界面下深度覆盖负载里的知识库长文本（i18n L3）。

    递归遍历 dict/list：凡是带 rule_id 的节点，用 kb-en.json 里同 id 的
    what/advice/tip/rebuild/impact/keep_if/short（非空才覆盖）、识别名
    label（支持 {drive} 盘符模板，覆盖节点自身的 label 字段）与置信度
    标签替换中文原文；其它字段（cleanable/risk/category/路径/体积等）
    一律不动。lang 不是 en 时原样返回。放在响应出口做而不是改识别器，
    是因为同一段文本可能来自内置知识库，也可能来自资源包 ——
    按 rule_id 覆盖与来源无关。
    """
    if str(lang or "").lower() != "en":
        return obj
    kb = load_kb_en()
    rules = kb.get("rules") if isinstance(kb.get("rules"), dict) else {}
    labels = kb.get("confidence_labels") if isinstance(kb.get("confidence_labels"), dict) else {}
    risk_labels = kb.get("risk_labels") if isinstance(kb.get("risk_labels"), dict) else {}

    def walk(node):
        if isinstance(node, list):
            for x in node:
                walk(x)
            return
        if not isinstance(node, dict):
            return
        rid = str(node.get("rule_id") or "")
        # 游戏中文名（zh_name）是给中文用户准备的：英文界面下显示原始名
        # 更自然 —— 中英夹杂反而难读。先做这一步，资源包条目的英文
        # label 随后覆盖（顺序保证：pack 的官方英文名优先于目录名）。
        # 只动显示字段，不碰判定。
        if str(node.get("zh_name") or "").strip() and node.get("name"):
            node["display_name"] = node["name"]
            node["zh_name"] = node["name"]
            # title 也一并还原：树行 / 游戏行的 title 来自内置规则的中文
            # 标题（「Steam 游戏库」）或包的中文 title，英文界面下同样
            # 应显示原始名。资源包条目的英文 label 随后仍会覆盖它。
            node["title"] = node["name"]
        entry = rules.get(rid) if rid else None
        if isinstance(entry, dict):
            for f in ("what", "advice", "tip", "rebuild", "impact",
                      "keep_if", "short"):
                en = entry.get(f)
                if isinstance(en, str) and en.strip() and node.get(f):
                    node[f] = en
            # 识别名：title / display_name / zh_name 都来自规则的中文 title，
            # 英文界面换成条目的 label（内置 42 条 + 资源包条目均已补齐；
            # 游戏本体行 rule_id=steam-game 等无 label，保持原始目录名）。
            en_name = entry.get("label")
            if isinstance(en_name, str) and en_name.strip():
                for f in ("title", "display_name", "zh_name"):
                    if node.get(f):
                        node[f] = en_name
            # 目标 / 结果行自身的显示名（label 字段）：支持 {drive} 盘符
            # 模板。盘符优先取节点自带的 drive 字段（清理结果行有），
            # 否则从展开后的绝对路径首字符取（state 的 targets 有）。
            # 拿不到盘符就放弃覆盖 —— 宁可显示中文也不显示「 盘回收站」。
            en_label = entry.get("label")
            # 自动发现项（rule_id=target:auto）的 label 是「{名}（自动发现）」，
            # 名字本身来自识别规则、无法在 kb-en 里预写整条，改为只换
            # 尾部的中文后缀。
            suffix = entry.get("label_suffix")
            if isinstance(suffix, str) and suffix and node.get("label"):
                zh_sfx = "（自动发现）"
                if str(node["label"]).endswith(zh_sfx):
                    node["label"] = str(node["label"])[: -len(zh_sfx)] + suffix
            if isinstance(en_label, str) and en_label.strip() and node.get("label"):
                if "{drive}" in en_label:
                    drv = str(node.get("drive") or "").rstrip(":").upper()
                    if not drv:
                        pth = str(node.get("path") or "")
                        drv = pth[0].upper() if len(pth) > 1 and pth[1] == ":" else ""
                    en_label = en_label.replace("{drive}", drv) if drv else ""
                if en_label:
                    node["label"] = en_label
        conf = str(node.get("confidence") or "")
        en_label = labels.get(conf)
        if isinstance(en_label, str) and en_label and node.get("confidence_label"):
            node["confidence_label"] = en_label
        en_risk = risk_labels.get(str(node.get("risk") or ""))
        if en_risk and node.get("risk"):
            node["risk"] = en_risk
        # 兜底：英文界面下 title / display_name 仍含中文（没有 label 的
        # 内置标题、上层目录匹配残留等）时，回退为原始名 —— 宁可显示
        # 目录原名，也不在英文界面里残留中文识别名。
        for f in ("title", "display_name"):
            v = node.get(f)
            if isinstance(v, str) and any("一" <= ch <= "\u9fff" for ch in v) \
                    and node.get("name"):
                node[f] = node["name"]
        for v in node.values():
            walk(v)

    walk(obj)
    return obj


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


def load_custom_rules() -> list:
    """自定义规则 = 用户勾选的一组固定清理项，可命名保存、一键套用或立即清理。

    结构：{"id","name","ids","drive","auto","created"}。
    旧版本存的是 {"path","kind","glob"} 形式的自由路径规则，语义完全不同，
    这里直接跳过，避免两种含义混在一起。
    """
    data = _load_json(CUSTOM_PATH, {"rules": []}, use_cache=False)
    rules = data.get("rules", []) if isinstance(data, dict) else (data or [])
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if "ids" not in r or "path" in r:
            continue
        out.append({
            "id": r.get("id") or f"rule-{abs(hash(str(r.get('name', '')))) % 10**8}",
            "name": (r.get("name") or "未命名规则").strip(),
            "ids": [str(i) for i in (r.get("ids") or [])],
            "drive": (r.get("drive") or "C").rstrip(":").upper(),
            "auto": bool(r.get("auto")),
            "created": r.get("created") or "",
        })
    return out


def save_custom_rules(rules: list) -> None:
    _cache.pop(str(CUSTOM_PATH), None)
    _atomic_write(CUSTOM_PATH, {"rules": rules})


def load_fs_groups() -> list:
    """用户自定义聚合组：把任意路径归拢进自己命名的组。

    结构：{"id","name","paths","created"}。paths 存归一化小写路径
    （与前端 normKey 同口径：统一斜杠方向、去结尾分隔符、转小写），
    匹配时前端按同口径比对，大小写与结尾反斜杠差异不影响归组。
    畸形条目（缺 id/名字、paths 为空）直接丢弃，不参与显示。
    """
    data = _load_json(FS_GROUPS_PATH, {"groups": []}, use_cache=False)
    groups = data.get("groups", []) if isinstance(data, dict) else []
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        paths = []
        for p in (g.get("paths") or []):
            np = (str(p).replace("/", "\\").rstrip("\\").strip().lower())
            if np and np not in paths:
                paths.append(np)
        if not str(g.get("name") or "").strip() or not paths:
            continue
        out.append({
            # id 兜底必须跨进程稳定：内置 hash() 受 PYTHONHASHSEED 随机化，
            # 同一份数据重启后会算出不同 id，前端按 id 管理组就会错乱。
            # 用内容摘要（名字 + 首路径）保证同一数据永远得到同一 id。
            "id": (str(g.get("id") or "").strip()
                   or "grp-" + hashlib.md5(
                       (str(g.get("name") or "") + "|" + str(g.get("paths") or "")[:1])
                       .encode("utf-8")).hexdigest()[:10]),
            "name": str(g["name"]).strip(),
            "paths": paths,
            "created": g.get("created") or "",
        })
    return out


def save_fs_groups(groups: list) -> None:
    # 先留一份 .bak：用户手工整理的归组数据没有别的恢复途径，
    # 覆盖写坏一次就全没了；写成功后 .bak 是上一版完整快照。
    try:
        if FS_GROUPS_PATH.exists():
            shutil.copyfile(FS_GROUPS_PATH, str(FS_GROUPS_PATH) + ".bak")
    except OSError:
        pass                     # 备份失败不影响主写入
    _cache.pop(str(FS_GROUPS_PATH), None)
    _atomic_write(FS_GROUPS_PATH, {"groups": groups})


def load_overrides() -> dict:
    """用户自定义释义 / 结论覆盖：{"targets": {键: {字段...}}}。

    只存用户显式改过的字段；没改的字段不出现，沿用资源包 / 内置的结论。
    键：普通目标用 base_id；自动发现 / 自定义目录项没有稳定的 base_id
    （它们分别共用 "auto" / "custom"），改用归一化小写路径 —— 见 _override_key。
    """
    data = _load_json(OVERRIDES_PATH, {"targets": {}}, use_cache=False)
    raw = data.get("targets") if isinstance(data, dict) else None
    out: dict = {}
    for k, v in (raw or {}).items():
        if not isinstance(v, dict):
            continue
        entry = {}
        for f in ("what", "rebuild", "impact", "keep_if"):
            if isinstance(v.get(f), str) and v[f].strip():
                entry[f] = v[f].strip()
        for f in ("label", "note"):
            if isinstance(v.get(f), str) and v[f].strip():
                entry[f] = v[f].strip()
        if isinstance(v.get("cleanable"), bool):
            entry["cleanable"] = v["cleanable"]
        if isinstance(v.get("pin"), bool):
            entry["pin"] = v["pin"]          # 自定义提级（2026-10-13）
        # 标签样式与颜色（2026-10-14）：label_kind 只换徽章样式与颜色，
        # 不参与清理判定；color 同时用于徽章与占用柱条。
        if v.get("label_kind") in ("clean", "unk", "keep"):
            entry["label_kind"] = v["label_kind"]
        if isinstance(v.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v["color"]):
            entry["color"] = v["color"].lower()
        if entry:
            out[str(k)] = entry
    return {"targets": out}


def save_overrides(data: dict) -> None:
    _cache.pop(str(OVERRIDES_PATH), None)
    _atomic_write(OVERRIDES_PATH, data)


def _override_key(t: dict) -> str:
    """覆盖条目的键：普通目标用 base_id（跨盘符通用）；
    自动发现 / 自定义目录项（base_id 泛化为 auto / custom）用归一化路径 ——
    它们的「身份」本来就是那个具体目录，而不是一个类型。"""
    bid = t.get("base_id") or t.get("id") or ""
    if bid in ("auto", "custom"):
        return os.path.normpath(t.get("path", "")).lower()
    return bid


def _apply_target_override(t: dict, overrides: dict) -> None:
    """把用户覆盖写进目标 dict。在资源包合并**之后**调用：用户是最后一手，
    优先级高于资源包与内置知识库。改过的字段打 custom 标记给界面显示。"""
    ov = overrides.get(_override_key(t))
    if not ov:
        return
    for f in ("what", "rebuild", "impact", "keep_if"):
        if f in ov:
            t[f] = ov[f]
    if "cleanable" in ov:
        t["cleanable"] = ov["cleanable"]
    t["custom"] = True


def _export_bundle() -> dict:
    """把本机可迁移的自定义项汇成一个 JSON（bundle/import 的逆操作）。

    覆盖三类数据：换算单位、释义/结论覆盖、固定项规则。这个导出文件同时
    是导入格式的「活样例」——把它交给 AI 参照着改，产出的文件可直接导回。
    """
    cfg = load_convert()
    units = [{
        "id": u.get("id"), "label": u.get("label"),
        "plural": u.get("plural") or u.get("label"),
        "bytes_per_unit": u.get("bytes_per_unit"),
        "note": u.get("note", ""), "category": u.get("category", "自定义"),
    } for u in cfg.get("units", []) if isinstance(u, dict)]
    return {
        "kind": "ocleaner-custom",
        "version": 1,
        "exported": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "units": units,
        "overrides": load_overrides().get("targets", {}),
        "rules": load_custom_rules(),
    }


_UNIT_MULT = {"b": 1, "kb": 1024, "mb": 1024 ** 2,
              "gb": 1024 ** 3, "tb": 1024 ** 4}


def _bundle_unit_bytes(u: dict) -> int | None:
    """导入单位时算出字节数：接受 bytes_per_unit 或 size:{value,unit}。"""
    bpu = u.get("bytes_per_unit")
    if bpu is not None:
        try:
            bpu = float(bpu)
        except (TypeError, ValueError):
            return None
        # NaN 已被 `bpu > 0` 排除（NaN 与任何数比较均为 False），无需 bpu == bpu
        if bpu > 0 and bpu != float("inf"):
            return int(bpu)
        return None
    size = u.get("size")
    if isinstance(size, dict):
        try:
            val = float(size.get("value"))
            mult = _UNIT_MULT.get(str(size.get("unit") or "").lower())
        except (TypeError, ValueError):
            return None
        if val > 0 and mult:
            return int(val * mult)
    return None


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_history() -> list:
    data = _load_json(HISTORY_PATH, [], use_cache=False)
    return data if isinstance(data, list) else []


def append_history(entry: dict) -> None:
    with _lock:
        hist = load_history()
        hist.insert(0, entry)
        _atomic_write(HISTORY_PATH, hist[:200])
    # 同步落一份独立回执：history.json 只保留最近 200 条且可能被清空，
    # 回执文件是每次清理的永久凭据（路径/体积/文件数/结果），用户可单独拷走。
    try:
        _write_receipt(entry)
    except Exception:
        traceback.print_exc()  # 回执失败不影响清理本身，也不影响历史入账


RECEIPT_DIR = DATA_DIR / "receipts"


def _write_receipt(entry: dict) -> None:
    """把一次清理的逐项结果写成独立 JSON 回执，落在数据目录 receipts/ 下。

    文件名取自清理记录自身的时间戳（与 history.json 里的条目一一对应）；
    解析不出时退回当前时间。
    """
    RECEIPT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = str(entry.get("time") or "")
    stamp = re.sub(r"[^0-9]", "", stamp)[:15]  # YYYYMMDD-HHMMSS
    if len(stamp) < 14:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S").replace("-", "")[:14]
    path = RECEIPT_DIR / f"cleanup-{stamp}.json"
    n = 0
    while path.exists():  # 同一秒多次清理时加序号，绝不覆盖
        n += 1
        path = RECEIPT_DIR / f"cleanup-{stamp}-{n}.json"
    receipt = {"tool": "O-Cleaner", "app_version": APP_VERSION, **entry}
    _atomic_write(path, receipt)


# ======================================================================
#  识别引擎
# ======================================================================

def _match_rule(rule: dict, name: str, ext: str, is_dir: bool) -> int:
    """给单条规则打分。0 表示不匹配；分越高越可信。

    短英文关键词（<=4 字符）必须整词匹配，避免子串误伤
    （如 "tim" 命中 "opTIMization"）。
    """
    keys = rule.get("match") or []
    how = rule.get("kind", "substring")
    low = name.lower()
    if how == "exact":
        return 100 if low in [str(k).lower() for k in keys] else 0
    if how == "extension":
        return 80 if ext and ext in [str(k).lower() for k in keys] else 0

    best = 0
    for k in keys:
        k = str(k).lower()
        if not k:
            continue
        if low == k:
            best = max(best, 95)
            continue
        if k not in low:
            continue
        if all(ord(c) < 128 for c in k) and len(k) <= 4:
            i = low.find(k)
            ok = False
            while i != -1:
                before = low[i - 1] if i > 0 else " "
                after = low[i + len(k)] if i + len(k) < len(low) else " "
                if not before.isalnum() and not after.isalnum():
                    ok = True
                    break
                i = low.find(k, i + 1)
            if not ok:
                continue
        best = max(best, (60 if is_dir else 45) + min(len(k), 12))
    return best


# 未被任何规则识别时，这些扩展名允许用「所在目录」推断身份；
# 其余扩展名已有明确含义（.mp4/.pdf/.exe），路径不应压过它
GENERIC_EXTS = {"", ".bin", ".dat", ".db", ".tmp", ".cache", ".data", ".idx",
                ".blob", ".log", ".json", ".xml", ".txt"}

# 游戏库的标志性路径段。位于这些路径下的 .exe 是游戏主程序而非安装包，
# 绝不能被「安装包 → 可清理」命中 —— 删掉 RDR2.exe 游戏就打不开了。
GAME_INSTALL_MARKERS = ("\\steamapps\\", "\\common\\", "\\steamlibrary\\",
                        "\\epic games\\", "\\gog galaxy\\",
                        "\\ubisoft game launcher\\")


def _in_game_install(path: str) -> bool:
    low = "\\" + (path or "").lower()
    return any(m in low for m in GAME_INSTALL_MARKERS)


# ---- 内容特征判定：删除决策不能只看目录名，还要看目录里装的是什么 ----
# 名称规则（cache/temp/quark/download…）只是猜测，目录内容才是证据。
# 祖先路径里一个 "quark" 就能把装着完整 Python 运行时与模型的程序目录
# 判成「云盘数据目录 · 可清理」，因此凡是名称规则判出「可清理」的目录，
# 都要过一遍内容特征；命中程序本体特征就强制改判不可清理（宁可少删，也不误删）。
_PROGRAM_WEIGHT_EXTS = {".safetensors", ".ckpt", ".gguf", ".pth"}
_WEIGHT_MIN_BYTES = 1024 ** 3          # 1 GB 的权重文件 = AI 模型，不是缓存
_PROGRAM_EXE_DLL_MIN = 15              # 顶层 exe+dll 超过它视为程序本体
_CONTENT_SCAN_ENTRY_CAP = 200          # 单层最多看的条目数（防超大目录拖慢标注）
_CONTENT_SCAN_SUBDIR_CAP = 12          # 最多下探的子目录数（Electron/整合包特征常在下一层）


def _content_says_program_body(path: str) -> str:
    """检查目录内容是否具有「程序本体」特征。命中返回原因描述，否则返回 ""。

    只扫本层 + 下一层且限量，对扫描标注（每个命中规则的节点调一次）开销可忽略。
    误判方向是安全的：真缓存目录几乎不可能长出这些特征；
    万一误判也只是把缓存标成「保留」，绝不反向（把程序标成可删）。
    """
    if not os.path.isdir(path):
        return ""
    # 明确的临时/缓存容器不参与内容判定：它们本来就是「内容可随意重建」
    # 的语义，安装包解压出的 exe 多不代表这是程序本体（如系统 Temp）。
    if os.path.basename(os.path.normpath(path)).lower() in ALLOWED_CONTAINER_NAMES:
        return ""
    try:
        entries = [e for e in os.scandir(path)][: _CONTENT_SCAN_ENTRY_CAP]
    except OSError:
        return ""
    names = {e.name.lower() for e in entries}
    # ① 整合包 Python 环境 / 启动器运行时（SD-WebUI 秋叶整合包等的直接特征）
    for marker in ("python_embeded", "python_embedded", "python_runtime", ".launcher"):
        if marker in names:
            return f"目录内含 {marker}\\（整合包 / 启动器运行时特征）"
    # ② 安装器痕迹：顶层有卸载程序 = 这是被安装到这里的软件
    if any(n.startswith("unins") and n.endswith((".exe", ".dat")) for n in names):
        return "目录内含卸载程序（unins*.exe / unins*.dat），这是安装的程序本体"
    # ③ Node / Electron 程序特征
    if "node_modules" in names and "package.json" in names:
        return "目录内 node_modules 与 package.json 并存（Node 程序特征）"
    sub_names: set[str] = set()
    sub_checked = 0
    exe_dll = 0
    for e in entries:
        try:
            if e.is_dir(follow_symlinks=False):
                if sub_checked < _CONTENT_SCAN_SUBDIR_CAP:
                    sub_checked += 1
                    try:
                        sub_names |= {
                            s.name.lower()
                            for s in os.scandir(e.path)
                        } | {e.name.lower()}
                    except OSError:
                        continue
            else:
                ext = os.path.splitext(e.name)[1].lower()
                if ext in (".exe", ".dll"):
                    exe_dll += 1
                elif ext in _PROGRAM_WEIGHT_EXTS:
                    try:
                        if e.stat(follow_symlinks=False).st_size >= _WEIGHT_MIN_BYTES:
                            return (f"目录内含 ≥1GB 的模型权重文件（{e.name}），"
                                    "这是 AI 程序的数据本体")
                    except OSError:
                        pass
        except OSError:
            continue
    if any(n == "app.asar" for n in sub_names):
        return "目录内 resources\\app.asar（Electron 程序特征）"
    if exe_dll >= _PROGRAM_EXE_DLL_MIN:
        return f"目录顶层含 {exe_dll} 个 .exe/.dll（程序本体特征，缓存目录不会这样）"
    return ""


def _looks_like_browser_profile(path: str) -> bool:
    """判断目录是否真的是浏览器资料目录。

    为什么需要：名字里带 chrome/edge 的目录未必是浏览器数据。
    实测有台机器上一个与浏览器同名的目录里装的是论文和 ComfyUI 压缩包 —— 是个误命名目录。
    若不加判断，会告诉用户「这里面有书签和密码」，纯属误导。
    真实资料目录一定有 User Data / Default / Profile 这类结构。

    注意 `cache` / `blob_storage` / `code cache` 这类**通用名不能单独作为证据**：
    几乎任何开发项目都有 node_modules/.cache，业务程序也可能用 blob_storage
    存用户上传的数据。把它们当判定依据，普通项目目录会被认成浏览器资料目录，
    进而把里面的业务数据划成「可清理、删掉会自动重建」—— 实测已发生。
    """
    if not os.path.isdir(path):
        return False
    # 强特征：只有浏览器资料目录才有的结构
    strong = {"user data", "local storage", "indexeddb", "session storage",
              "service worker", "extensions", "web data", "bookmarks",
              "preferences", "login data", "history"}
    # 弱特征：通用缓存目录名，必须与强特征同时出现才算数
    weak = {"cache", "code cache", "gpucache", "shadercache", "blob_storage",
            "default", "profile", "profiles"}
    try:
        names = {e.name.lower() for e in os.scandir(path)}
    except OSError:
        return False
    if names & strong:
        return True
    # 「Default」目录本身也是一层强特征（Chrome/Edge 的多用户结构），
    # 但要求它旁边还有 Profile/User Data 之类，避免同样是通用名的误判
    if names & weak and (names & {"default", "profile", "profiles"}) and \
            ("user data" in path.lower().replace("/", "\\")):
        return True
    # 也有可能是「上层」目录（如 ...\Google\Chrome），往下看一层
    for sub in list(names)[:12]:
        try:
            sp = os.path.join(path, sub)
            if not os.path.isdir(sp):
                continue
            sub_names = {e.name.lower() for e in os.scandir(sp)}
            if sub_names & strong:
                return True
        except OSError:
            continue
    return False


def _identify_builtin(name: str, path: str, kind: str) -> dict:
    """按知识库识别一个文件/目录，返回分类、风险、说明与清理建议。

    两路证据独立算分：路径不得压过扩展名，否则安装包会被
    所在目录的「下载目录」规则吞掉分类。
    """
    kb = load_filetypes()
    is_dir = (kind == "dir")
    ext = os.path.splitext(name)[1].lower()
    folder_rules = list(kb.get("folder_rules", []))
    file_rules = list(kb.get("file_rules", []))
    # 文件也要考虑目录规则 —— 「文件所在的目录」往往是唯一线索
    # （例如 .ollama\\models\\blobs\\sha256-xxx 只有靠目录规则才能解释）
    rules = folder_rules if is_dir else (folder_rules + file_rules)

    ancestors = []
    _d = os.path.dirname(path.rstrip("\\/")) if path else ""
    for _ in range(4):
        if not _d or len(_d) <= 3:
            break
        _b = os.path.basename(_d)
        if _b:
            ancestors.append(_b)
        _d = os.path.dirname(_d)
    dir_part = "\\".join(ancestors).lower()

    in_game = _in_game_install(path)
    # 浏览器规则的上下文校验（只算一次，避免反复扫目录）
    browser_ok: bool | None = None
    is_browser_rule = {"browser-data", "browser-profile"}
    name_best, name_score = None, 0
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if in_game and rule.get("id") == "installer-pkg":
            continue
        if rule.get("id") in is_browser_rule and is_dir:
            if browser_ok is None:
                browser_ok = _looks_like_browser_profile(path)
            if not browser_ok:
                continue          # 名字带 chrome 但不是浏览器资料，不套用该规则
        s = _match_rule(rule, name, ext, is_dir)
        if s > name_score:
            name_best, name_score = rule, s

    path_best, path_score = None, 0
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("kind", "substring") != "substring":
            continue
        for k in (rule.get("match") or []):
            k = str(k).lower()
            if len(k) >= 4 and k in dir_part:
                s = 55 + min(len(k), 14)
                if s > path_score:
                    path_best, path_score = rule, s

    if is_dir:
        best, best_score = name_best, name_score
        if best_score == 0 and path_best is not None:
            best, best_score = path_best, path_score
    elif ext in GENERIC_EXTS and path_best is not None and path_score > name_score:
        best, best_score = path_best, path_score
    else:
        best, best_score = name_best, name_score

    if best and best_score > 0:
        cleanable = bool(best.get("cleanable"))
        if is_dir and cleanable:
            # 内容证据优先于名称猜测（P0-1 修复）：名称像缓存/数据目录，
            # 但内容是程序本体 → 强制改判不可清理。
            reason = _content_says_program_body(path)
            if reason:
                return {
                    "recognized": True, "rule_id": best.get("id"),
                    "title": best.get("title") or name,
                    "category": best.get("category", "其它"),
                    "risk": "高", "cleanable": False,
                    "confidence": "high",
                    "confidence_label": "高（按目录内容判定）",
                    "what": f"名称特征像「{best.get('title') or name}」，"
                            f"但内容不是：{reason}。"
                            "已按目录内容改判为不可清理 —— 名称规则只是猜测，"
                            "删错程序本体的代价远大于少删一个缓存。",
                    "advice": "建议保留。如果确认整个目录确实没用了，"
                              "先在文件资源管理器里打开核实，"
                              "或改用程序自带的卸载/清理功能。",
                    "tip": "想让它恢复自动判定：删掉目录里的程序文件后，"
                           "下次扫描会按名称重新判定。",
                    "content_protected": True,
                    "match_score": best_score,
                }
        return {
            "recognized": True, "rule_id": best.get("id"),
            "title": best.get("title") or name,
            "category": best.get("category", "其它"),
            "risk": best.get("risk", "中"),
            "cleanable": cleanable,
            "confidence": best.get("confidence", "low"),
            "confidence_label": (kb.get("confidence_labels", {}) or {})
                                .get(best.get("confidence", "low"), ""),
            "what": best.get("what", ""), "advice": best.get("advice", ""),
            "tip": best.get("tip", ""), "match_score": best_score,
        }

    if is_dir:
        return {
            "recognized": False, "rule_id": None, "title": name,
            "category": "未识别", "risk": "中", "cleanable": False,
            "confidence": "low",
            "confidence_label": "低（知识库里没有这个名称）",
            "what": f"知识库里没有匹配到「{name}」这个名字，无法确定它的用途。"
                    f"它占用了可观空间，但本工具不会猜测内容。",
            "advice": "建议你手动打开这个目录看一眼内容再决定。本工具不会把它列入清理建议。",
            "tip": "如果你知道它是什么，可以在 config\\filetypes.json 里加一条规则，"
                   "以后就能自动识别了。",
            "match_score": 0,
        }
    return {
        "recognized": False, "rule_id": None, "title": name,
        "category": "未识别", "risk": "中", "cleanable": False,
        "confidence": "low",
        "confidence_label": "低（仅按扩展名无法判断）",
        "what": f"「{ext or '无扩展名'}」类型的文件，知识库里没有对应说明。",
        "advice": "请自行确认它的用途。本工具不会建议删除无法识别的文件。",
        "tip": "只有当你确定它没用时再删。",
        "match_score": 0,
    }


def identify(name: str, path: str, kind: str) -> dict:
    """识别一个目录/文件：内置知识库 + 资源包 的合并结果。

    合并规则（两条，够用且好解释）：

    * 资源包命中的是**这个节点本身**（关键字正好覆盖到路径末尾）→ 包的说明优先：
      title 与四段说明都用包的，因为包比内置规则更具体
      （内置对游戏目录只会说「Steam 游戏库」，包能说出是哪款游戏）。
    * 资源包命中的只是**上层目录**（关键字止于路径中段）→ 只补一个中文显示名，
      结论仍以内置为准，免得把库里随便哪个子目录都改写成库的名字。

    无论哪种情况，**内置已识别时 cleanable / risk / category 一律沿用内置值**：
    前两项决定「删不删」，category 决定归类（扩展名优先于路径线索的保证就在这），
    都不能让可替换的外部数据说了算；包只在内置**不认识**时才提供完整结论。
    """
    info = _identify_builtin(name, path, kind)
    hit = packs_lookup(name, path, kind)
    if not hit:
        return info
    out = dict(info)
    out["pack"] = hit["_pack"]
    title = str(hit.get("title") or "").strip()
    if title:
        # 中文显示名始终带上：前端用它做标题，比内置的归类名更好认
        out["display_name"] = title
        out["zh_name"] = title
    recognized_before = bool(out.get("recognized"))
    if not (hit.get("_node_level") or not recognized_before):
        return out                       # 只匹配到上层目录：到此为止
    if title:
        out["title"] = title
    if not recognized_before:
        if hit.get("category"):
            out["category"] = str(hit["category"])
        out["risk"] = str(hit.get("risk") or "中")
        out["cleanable"] = bool(hit.get("cleanable"))
        out["rule_id"] = f"{hit['_pack']}:{hit.get('id') or name}"
        out["confidence"] = "medium"
        out["confidence_label"] = f"中（来自资源包 {hit['_pack']}）"
        out["match_score"] = int(hit.get("_score") or 0)
        out["recognized"] = True
    pwhat = str(hit.get("what") or "").strip()
    peff = str(hit.get("effect") or "").strip()
    pkeep = str(hit.get("keep") or "").strip()
    preb = str(hit.get("rebuild") or "").strip()
    if pwhat:
        out["what"] = pwhat
    if peff or pkeep:
        out["advice"] = peff + ("　" + pkeep if pkeep else "")
    if preb:
        out["tip"] = preb
    out["pack_note"] = (f"识别与说明来自资源包 {hit['_pack']}" if not recognized_before
                        else f"说明来自资源包 {hit['_pack']}")
    return out


# ======================================================================
#  浏览器数据目录分区
# ======================================================================

# 浏览器数据目录同时装着两类东西：
#   可安全清理的缓存（Cache / Code Cache / GPUCache / ShaderCache …）
#   绝不能删的：书签、密码、Cookie、登录状态、扩展、历史记录
# 因此**整目录绝不可标为可清理**，只能把缓存子目录单独列出来
# （把整目录标成可清理会连带删掉书签、密码与登录状态）。
BROWSER_CACHE_DIRS = {
    "cache", "code cache", "gpucache", "shadercache", "grshadercache",
    "dawngcache", "dawngraphitecache", "component crx cache",
    "media cache", "application cache", "service worker",
    "cachestorage", "scriptcache", "jscache", "blob_storage",
}
# 判定不依赖目录名单：与浏览器同名的目录可能装着完全无关的数据，
# 统一走 _looks_like_browser_profile()，必须在目录内部真看到
# Cache / Code Cache 这类缓存子目录才认。

def _looks_like_game_install(path: str) -> bool:
    """判断目录是否是一个游戏本体（而非缓存）。

    全盘扫描中最大的实体就是游戏（单款 3A 大作常达上百 GB），且它们不是垃圾；
    单独认出来才能给出有用的指引：「想回收请用启动器卸载」。

    判定顺序（先便宜后昂贵）：
      1. 路径里出现 steamapps/common/steamlibrary 等标志段 —— 最可靠也最快；
      2. 名字命中知识库的「游戏」类规则；
      3. 直接子目录里有 .exe —— 兜底，但要真扫目录，所以放最后。
    """
    if not path:
        return False
    if _in_game_install(path):
        return True
    try:
        info = identify(os.path.basename(path.rstrip("\\/")) or "", path, "dir")
        if info.get("recognized") and (info.get("category") or "") in ("游戏", "模拟器"):
            return True
    except Exception:
        pass
    # 兜底：游戏目录根下一定有可执行文件（RDR2.exe / witcher3.exe …）
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".exe"):
                    return True
    except OSError:
        pass
    return False


def analyze_dir_partition(path: str) -> dict:
    """把目录分成「缓存子目录（可清理）/ 其它内容（不要删）」两部分。

    为什么不能只给一句笼统的「建议保留」：一个与浏览器同名的目录可能有上百 GB，
    整体标成可清理会删掉书签密码，但只标「保留」用户也不知道这上百 GB 里
    哪一块动了没事。所以这里按子目录逐个分类并给出理由。

    解释文案必须跟着目录**真实身份**走。踩过的坑：早先把所有保留下来的
    文件都写成「浏览器配置文件，不要删」，结果某款游戏的 levels_3.rpf 也顶着
    这句话 —— 纯属误导。现在按 浏览器资料 / 游戏安装 / 未识别 三种语境分别措辞。
    """
    if not os.path.isdir(path):
        return {"error": "路径不存在", "cleanable": [], "keep": [],
                "kind": "unknown", "title": "路径不存在", "note": ""}
    r = run_ps(PS_DIR_KIND.replace("__PATH__", path.replace("'", "''")), timeout=600)
    if "error" in r:
        return {"error": r["error"], "cleanable": [], "keep": [],
                "kind": "unknown", "title": "", "note": ""}

    # 先定语境，再决定每一行怎么写理由
    is_browser = _looks_like_browser_profile(path)
    is_game = False if is_browser else _looks_like_game_install(path)
    if is_browser:
        kind, title = "browser", "浏览器资料目录"
        note = ("这里同时装着可清缓存和绝不能删的用户数据（书签、密码、"
                "Cookie、登录状态、扩展、历史记录），所以整个目录不能一键清理。")
    elif is_game:
        kind, title = "game", "游戏安装目录"
        note = ("这是游戏本体，体积大但**不是垃圾**。想回收只有一条正路："
                "用 Steam / 启动器卸载这款游戏。工具不会替你删这里的任何文件。")
    else:
        kind, title = "unknown", "未能确定身份的目录"
        note = ("知识库里没有这个目录的规则，工具不对它做任何清理判断。"
                "下面只是把它拆开让你看清体积分布，任何一项都不要直接删。")

    cleanable: list[dict] = []
    keep: list[dict] = []
    for d in _as_list(r.get("dirs")):
        try:
            name = str(d.get("name") or "")
            dp = str(d.get("path") or "")
            entry = {"name": name, "path": dp, "type": "dir",
                     "modified": str(d.get("modified") or ""), "size": 0}
            if name.strip().lower() in BROWSER_CACHE_DIRS:
                entry["role"] = "cache"
                if is_browser:
                    entry["why"] = "浏览器/程序缓存目录，删掉会自动重建"
                    cleanable.append(entry)
                else:
                    # 非浏览器目录下只有名字像缓存 —— 业务程序常把**用户数据**
                    # 放在 blob_storage / cache 里。按「疑似缓存」归类但不给
                    # 「可安全删除」的结论，也不计入可释放体积，交给用户判断。
                    entry["role"] = "maybe-cache"
                    entry["why"] = ("名字像缓存，但这个目录不是浏览器资料目录，"
                                    "无法确认内容可重建 —— 请自行确认后再决定")
                    keep.append(entry)
            else:
                entry["role"] = "keep"
                if is_browser:
                    entry["why"] = "用户数据（书签/密码/登录状态等），不要删"
                elif is_game:
                    entry["why"] = "游戏资源，请用启动器卸载而不是手删"
                else:
                    entry["why"] = "请自行确认内容后再决定，工具不判断"
                keep.append(entry)
        except Exception:
            continue
    for f in _as_list(r.get("files")):
        try:
            keep.append({"name": str(f.get("name") or ""),
                         "path": str(f.get("path") or ""), "type": "file",
                         "size": int(f.get("size") or 0),
                         "modified": str(f.get("modified") or ""),
                         "role": "keep",
                         "why": ("游戏主程序/资源文件，删除会导致游戏无法运行"
                                 if is_game else
                                 "浏览器用户数据文件，不要删" if is_browser else
                                 "请自行确认用途，工具不判断")})
        except Exception:
            continue

    # 体积必须把「目录」也量进去 —— 只算直接文件会严重低报
    # （实测这种目录有上百 GB，但直接文件只有几 GB，其余都在子目录里）
    need_size = [c["path"] for c in cleanable] + [k["path"] for k in keep
                                                  if k["type"] == "dir"]
    if need_size:
        # 临时清单必须放系统临时目录、且文件名唯一：
        # 1) 以前写在 BASE_DIR 下（仓库目录），与 HANDOFF 踩坑史里
        #    oc-steam-orphans.tmp 是同一模式 —— 在带安全删除钩子的宿主环境里
        #    会耗尽删除配额，钩子抛 SystemExit 直接打断请求（实测已复现：
        #    safe-delete 钩子拦下 E:\O-Cleaner\.oc-partition.tmp，请求被断开）。
        # 2) 固定文件名在并发请求下会互相覆盖，A 读到 B 的路径列表。
        lst = Path(tempfile.gettempdir()) / f"oc-partition-{os.getpid()}-{uuid.uuid4().hex[:8]}.tmp"
        try:
            lst.write_text("\n".join(need_size[:80]), encoding="utf-8")
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
            pass
        finally:
            try:
                lst.unlink()
            except OSError:
                pass
    cleanable.sort(key=lambda x: -x["size"])
    keep.sort(key=lambda x: -x["size"])
    for x in cleanable + keep:
        x["size_h"] = human(x["size"])
    return {
        "path": path,
        "kind": kind, "title": title, "note": note,
        "denied": bool(r.get("denied")),
        "cleanable": cleanable, "keep": keep,
        "cleanable_size": sum(c["size"] for c in cleanable),
        "cleanable_size_h": human(sum(c["size"] for c in cleanable)),
        "keep_size": sum(c["size"] for c in keep),
        "keep_size_h": human(sum(c["size"] for c in keep)),
    }


# ======================================================================
#  Steam 游戏库（读 Steam 自己的清单，秒级且精确）
# ======================================================================

# 为什么直接读 Steam 的清单而不是扫磁盘：
#   * 快几个数量级 —— 只读几个几十 KB 的文本，不遍历目录
#   * 准 —— 游戏名、安装目录、体积都是 Steam 自己记录的，不用猜
#   * 能回答「Steam 认为装了什么」与「磁盘上实际有什么」的差异：
#     残留的游戏文件夹（已卸载但目录还在）是常见的空间大户

# StateFlags 位含义（Steam 官方文档）
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


def _ts(v) -> str:
    try:
        n = int(v or 0)
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d") if n > 0 else ""
    except (ValueError, OSError, OverflowError):
        return ""


def discover_steam() -> dict:
    """发现 Steam 库与已安装游戏。数据全部来自 Steam 自己的清单文件。"""
    empty = {"found": False, "roots": [], "libraries": [], "games": [],
             "orphans": [], "game_count": 0, "total_size": 0,
             "total_size_h": "0 B", "orphan_count": 0}
    if not IS_WINDOWS:
        return empty
    r = run_ps(PS_STEAM, timeout=180)
    if "error" in r:
        return {**empty, "error": r["error"]}

    seen_roots: set[str] = set()
    roots: list[str] = []
    for x in _as_list(r.get("roots")):
        p = os.path.normpath(str(x))
        if p.lower() not in seen_roots:
            seen_roots.add(p.lower())
            roots.append(p)
    manifests = [str(x) for x in _as_list(r.get("manifests"))]
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
        info = identify(installdir or state.get("name", ""), game_path, "dir")
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
            "size": size, "size_h": human(size),
            "library": lib,
            "installed": bool(flags & 4),
            "state": STEAM_STATE_FLAGS.get(flags, f"状态码 {flags}"),
            "last_updated": _ts(state.get("LastUpdated")),
            "last_played": _ts(state.get("LastPlayed")),
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
                               "size": v["size"], "size_h": human(v["size"])})
    for letter, d in by_drive.items():
        info = disk_cache.setdefault(letter, get_disk_info(letter))
        d["size_h"] = human(d["size"])
        d["disk_total"] = info.get("total", 0)
        d["disk_total_h"] = info.get("total_h", "")
        d["disk_free_h"] = info.get("free_h", "")
        d["disk_percent"] = info.get("percent", 0)
        d["share"] = (round(d["size"] / info["total"] * 100, 1)
                      if info.get("total") else 0)
        d["libraries"].sort(key=lambda x: -x["size"])

    lib_list = sorted(
        ({**v, "size_h": human(v["size"]), "exists": os.path.isdir(v["path"])}
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
        lst = Path(tempfile.gettempdir()) / "oc-steam-orphans.tmp"
        try:
            lst.write_text("\n".join(o["path"] for o in orphans[:40]),
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
            pass

    total = sum(g["size"] for g in games)
    return {
        "found": True, "roots": roots,
        "libraries": lib_list, "games": games,
        "by_drive": sorted(by_drive.values(), key=lambda x: -x["size"]),
        "game_count": len(games),
        "total_size": total, "total_size_h": human(total),
        "orphans": orphans[:20], "orphan_count": len(orphans),
        "orphan_size": sum(o["size"] for o in orphans),
        "orphan_size_h": human(sum(o["size"] for o in orphans)),
    }


# ======================================================================
#  Epic / GOG 游戏发现（v2.2 新增）
# ======================================================================

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
    if not IS_WINDOWS:
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
            size = _size_of_dir(g["path"])
        info = identify(g["name"], g["path"], "dir")
        info.pop("match_score", None)
        games.append({
            "appid": g["appid"], "platform": "epic",
            "name": g["name"], "display_name": g["name"], "zh_name": "",
            "installdir": os.path.basename(g["path"]),
            "path": g["path"], "exists": exists,
            "size": size, "size_h": human(size),
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
            "total_size": total, "total_size_h": human(total),
            "orphans": orphans[:20], "orphan_count": len(orphans),
            "orphan_size_h": human(sum(o["size"] for o in orphans))}


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
    if not IS_WINDOWS:
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
                size = _size_of_dir(g["path"]) if exists else 0
                info = identify(g["name"], g["path"], "dir")
                info.pop("match_score", None)
                games.append({
                    "appid": g["appid"], "platform": "gog",
                    "name": g["name"], "display_name": g["name"], "zh_name": "",
                    "installdir": os.path.basename(g["path"]),
                    "path": g["path"], "exists": exists,
                    "size": size, "size_h": human(size),
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
            "total_size": total, "total_size_h": human(total),
            "orphans": orphans[:20], "orphan_count": len(orphans),
            "orphan_size_h": human(sum(o["size"] for o in orphans))}


# ---- Epic / GOG 的残留目录（孤儿）----
# 与 Steam 孤儿同一信任级别：只看「安装根」的**直接子目录**，且此刻的平台
# 记录里没有它。安装根 = 平台自己记录的安装目录的父目录 ∪ 默认安装根。
# 启动器自己的目录（如 Epic Games Launcher）会被显式排除。
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
    if IS_WINDOWS:
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
        sz, _trunc = _size_of_dir_budgeted(o["path"], max_entries=50000)
        o["size"] = sz
        o["size_h"] = human(sz)
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
        "total_size": total, "total_size_h": human(total),
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


# ======================================================================
#  安全校验与删除
# ======================================================================

def check_deletable(path: str, kind: str = "tree") -> tuple[bool, str]:
    """判断某个清理目标是否允许操作。返回 (是否允许, 拒绝原因)。

    注意：kind="files" 时 path 是**要扫描的目录**（不是要删的文件），
    真正的删除对象是它内部匹配 glob 的文件，所以同样按目录校验。
    """
    # 空路径必须显式拒绝：os.path.abspath("") 会返回**当前工作目录**，
    # 于是空串会伪装成一个合法目录并通过后续检查（对抗性审查发现的漏点）。
    raw = str(path).strip()
    if not raw:
        return False, "拒绝：路径为空"
    # 同样的问题也发生在「盘符相对路径」上：abspath("E:") 得到的是
    # **E 盘上的当前工作目录**（在本机即仓库自身 E:\O-Cleaner），
    # 而不是盘符根；"..\Windows"、"/etc" 之类也会被 abspath 归一化到 CWD 之下。
    # 这类输入在归一化前必须卡死，否则它会伪装成一个合法目录通过后续全部检查。
    if not os.path.isabs(raw):
        return False, "拒绝：路径必须是绝对路径"
    norm = os.path.normpath(os.path.abspath(raw))
    low = norm.lower()
    if len(low) < 3 or low[1] != ":" or low[2] != "\\":
        return False, "拒绝：目标不是有效的盘符路径"
    drive_root = low[:3]

    if low == drive_root:
        return False, "拒绝：不允许清理盘符根目录本身"

    if os.path.isdir(norm):
        parts = [p for p in norm[len(drive_root):].split("\\") if p]
    elif os.path.isfile(norm):
        if norm.count("\\") <= drive_root.count("\\"):
            return False, "拒绝：不允许在盘符根目录直接删除文件"
        parts = [p for p in norm[len(drive_root):].split("\\") if p]
    else:
        return False, "拒绝：目标不存在或不可访问"

    if not parts:
        return False, "拒绝：不允许清理盘符根目录"

    # 回收站目标（kind="recyclebin"）只允许精确指向「盘根的 $Recycle.Bin」——
    # 这是应用自己的「清空回收站」清理项，勾选后走 Clear-RecycleBin。
    # 它本来会被下面的 $Recycle.Bin 保护规则拦死，导致这条链路不可达
    # （2026-10-15 端到端实测发现）。其余任何路径照常走保护链，不受影响。
    if kind == "recyclebin" and len(parts) == 1 and parts[0].lower() == "$recycle.bin":
        return True, ""

    if len(parts) == 1 and parts[0].lower() in PROTECTED_AT_DRIVE_ROOT:
        return False, f"拒绝：{parts[0]} 属于受保护的系统目录"

    fatal = {"system32", "syswow64", "winsxs", "installer", "servicing",
             "assembly", "system volume information", "boot"}
    for p in parts:
        if p.lower() in fatal:
            return False, f"拒绝：路径中包含受保护的 {p}"

    leaf = parts[-1].lower()
    if leaf in PROTECTED_DIR_NAMES and leaf not in ALLOWED_CONTAINER_NAMES:
        return False, f"拒绝：{parts[-1]} 属于受保护的目录"

    # P0-3（外部审计 2026-10-13）：末级目录名是 cache/temp/logs 这类通用容器
    # **不代表**它是系统缓存 —— 许多程序把 cache/temp 放在安装目录里
    # （Electron 应用尤其常见）。删除决策不能只看末级名字：
    # 逐级检查祖先链，任何一级长得像「已安装程序」（顶层有卸载程序或
    # 多个版本号目录）就拒绝 —— 这种容器会随程序卸载一起消失，单独删它
    # 只会把软件删残。系统容器（\Windows\Temp、AppData\Local\Temp）的
    # 祖先都在下面的豁免名单里，不受影响。
    if leaf in ALLOWED_CONTAINER_NAMES:
        _safe_ancestors = {
            "windows", "program files", "program files (x86)", "users",
            "appdata", "local", "locallow", "roaming", "local settings",
            "programdata", "temp", "tmp", "inetcache", "$recycle.bin",
        }
        for i in range(len(parts) - 1):
            if parts[i].lower() in _safe_ancestors:
                continue
            anc_path = os.path.join(drive_root, *parts[:i + 1])
            if os.path.isdir(anc_path) and _looks_like_installed_program(anc_path):
                return False, (
                    f"拒绝：{parts[-1]} 是已安装程序（{parts[i]}）自带的目录，"
                    "会随程序卸载一起清理，不能单独删除")

    home = os.path.expanduser("~").lower()
    if low in (home, os.path.join(home, "appdata").lower()):
        return False, "拒绝：不允许清理用户主目录"

    # 仓库自身与仓库内数据（history.json / config/ 等）此前只靠「没被推荐」
    # 兜底，没有硬护栏 —— 在这里显式拒绝。清理工具不该有能力删掉自己。
    try:
        base_low = str(BASE_DIR).lower()
        if low == base_low or low.startswith(base_low.rstrip("\\") + "\\"):
            return False, "拒绝：目标是 O-Cleaner 自身或其数据目录"
    except Exception:
        pass

    # junction / 符号链接防护（2026-10-20 审计）：以上全部规则都基于路径
    # **字符串分段名**，而一个指向受保护目录的 junction（如 D:\cache-link
    # → C:\Windows）段名是自选的，不会命中任何名单；Windows 上 Python 的
    # islink() 对 junction 返回 False，shutil.rmtree 会穿过去删真实目标。
    # 因此对真实存在的目标解析真实路径，并对解析结果重新执行同一套检查 ——
    # 只会让护栏**更严**（多拒绝），符合「宁可少推荐，也不误删」的取向。
    try:
        real = os.path.realpath(norm)
    except OSError:
        real = norm
    if os.path.normpath(real).lower() != low:
        return check_deletable(real, kind)

    # 不可读时不在这里判定拒绝，交由上层报「需要管理员权限」，
    # 这样 C:\\Windows\\Temp 能给出可读提示而不是硬性拒绝
    return True, ""


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


# ---- P1-1 修复（外部审计 2026-10-13）：删除前的占用预检与「部分删除」治理 ----
# 旧实现 rmtree(ignore_errors=True) 会跳过删不掉的文件继续删其余的：
# 对「软件正在运行」的场景，被锁的 .exe/.dll 跳过、其余全删 —— 结果是一个
# 表面报「清理失败」、实际已经残缺的安装目录。现在改成三段式：
#   ① 预检：目录内关键文件被占用 → 整体中止，零副作用；
#   ② 改名暂存：os.rename 是原子操作，失败 = 未删除任何内容；
#   ③ 删暂存目录：删不干净时残留目录如实上报，绝不假装删干净了。
_LOCK_PRECHECK_BUDGET = 300          # 最多探测的文件数（CreateFileW 每次亚毫秒级）
_LOCK_PRECHECK_REPORT_MAX = 5        # 结果里最多列出的被占用文件数


# 已知缓存持有者的提示：这类文件被系统服务寄在 svchost.exe 里占着，
# 「一键查找占用进程」的启发式（路径/文件名信号）注定打不中 ——
# 与其让用户在进程列表里白搜，不如直接告诉他们停哪个服务。
_LOCK_OWNER_HINTS = {
    "deliveryoptimization":
        "这类缓存通常由 Windows「传递优化」服务（DoSvc，寄在 svchost.exe 里）持有，"
        "在进程列表里搜不到文件名是正常的。管理员 PowerShell 执行 "
        "Stop-Service DoSvc 后回来重新清理，清完 Start-Service DoSvc 恢复"
        "（重启电脑也会自动恢复）。",
}


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
            if probed >= _LOCK_PRECHECK_BUDGET or len(locked) >= _LOCK_PRECHECK_REPORT_MAX:
                return locked
            probed += 1
            fp = os.path.join(root, f)
            pr = probe_locked(fp)
            if pr.get("locked") is True:
                locked.append(fp)
    return locked


def rm_tree_strict(path: str) -> tuple[bool, bool, str]:
    """三态删除目录：返回 (是否全删, 是否部分删除, 说明)。

    全删 = 目录已不存在；部分删除 = 已从原位置移除、内部有删不掉的文件
    残留在 .oc-deleting-* 暂存目录；两者都失败 = 原目录原封不动。
    """
    staging = path.rstrip("\\/") + f".oc-deleting-{time.time_ns() % 10**8:08d}"
    try:
        os.rename(path, staging)
    except OSError as exc:
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
        return True, False, ""
    tail = staging if len(staging) <= 80 else "…" + staging[-77:]
    return False, True, (f"已从原位置移除，但内部有删不掉的文件，"
                         f"残留在 {tail}（确认相关程序已退出后可手动删除）")


# ---- 删进回收站（可恢复）----
# 用 shell32.SHFileOperationW 带 FOF_ALLOWUNDO。两个刻意的决定：
#   1. **必须先做容量守卫**：目标超过回收站容量上限时，带 FOF_NOCONFIRMATION 的
#      删除会**静默转为永久删除**——那恰好是要避免的事。所以先按注册表里的
#      MaxCapacity（缺省按盘容量 5% 估）算出上限，超限就拒绝并说清楚原因。
#   2. **移入回收站不等于释放空间**（同卷只是改名），所以 trash 模式下 freed
#      一律报 0，由结果弹窗说明「清空回收站后才真正释放」——不虚报。
_FOF_ALLOWUNDO = 0x40
_FOF_NOCONFIRMATION = 0x10
_FOF_SILENT = 0x4
_FOF_NOERRORUI = 0x400


def _recycle_capacity_bytes(drive_root: str) -> int:
    """该盘回收站的容量上限（字节）。注册表 MaxCapacity 缺失时按盘容量 5% 估。"""
    import winreg
    import ctypes
    try:
        guid_buf = ctypes.create_unicode_buffer(100)
        # 路径必须是「X:\」形式，反斜杠结尾
        root = drive_root if drive_root.endswith("\\") else drive_root + "\\"
        if ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW(
                ctypes.c_wchar_p(root), guid_buf, 100):
            key = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                   r"\BitBucket\Volume") + "\\" + guid_buf.value.rstrip("\\")
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                mb, _ = winreg.QueryValueEx(k, "MaxCapacity")
                if isinstance(mb, int) and mb > 0:
                    return mb * 1024 * 1024
    except OSError:
        pass
    try:
        import shutil
        return int(shutil.disk_usage(drive_root).total * 0.05)
    except OSError:
        return 2 * 1024 * 1024 * 1024   # 兜底 2 GB


def rm_trash(path: str) -> tuple[bool, str]:
    """把文件/目录移入回收站。返回 (是否成功, 失败原因)。"""
    import ctypes
    from ctypes import wintypes
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return True, ""
    root = os.path.splitdrive(path)[0] + "\\"
    cap = _recycle_capacity_bytes(root)
    size = _size_of(path)
    if size > cap:
        return False, (f"体积 {human(size)} 超过该盘回收站容量上限（约 {human(cap)}），"
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
    op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
                 | _FOF_NOERRORUI)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code == 0 and not op.fAnyOperationsAborted:
        return True, ""
    return False, f"系统回收站操作失败（错误码 {code}）"


# ---- 按原始路径精确清除回收站条目（2026-10-13）----
# 需求：清理完成后「只清空所选项的回收站」，而不是清空整盘回收站——
# 用户自己删的其它文件必须原样保留。Windows 的回收站条目是一对文件：
#   $I<随机>.ext  元数据（内含**原始完整路径**、字节数、删除时间）
#   $R<随机>.ext  实际数据
# SHFileOperation 不回报目标名，所以要找回「我们刚删的那几项」，只能
# 反着来：解析盘上现存 $I 的原始路径，与本次清理的目标路径精确匹配。
# $I 格式（版本 2，Win10+）：8B 版本号 + 8B 字节数 + 8B 删除时间(FILETIME)
#   + 4B 名称长度(字符) + UTF-16LE 名称；版本 1 为 24B 定长名称字段。

def _parse_i_file(ipath: str):
    """解析一个 $I 元数据文件 → (原始路径, 字节数, 配对的 $R 路径)。
    解析不了（损坏/未知版本）返回 None，调用方跳过。"""
    import struct
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
        # v2 布局：ver(8) + size(8) + 删除时间 FILETIME(8) + 名称长度(4) + 名称
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


def purge_recycle_paths(paths: list) -> dict:
    """把给定原始路径从回收站里精确移除（只动配对的 $I/$R，不动其它条目）。
    返回逐项结果与合计释放量。真实的删除永远只发生在 $Recycle.Bin 内部——
    请求里的路径只用于匹配，绝不会被直接删除。"""
    wanted = []
    for p in paths or []:
        if not isinstance(p, str) or not os.path.isabs(p):
            continue
        wanted.append(os.path.normpath(p).lower())
    wanted = list(dict.fromkeys(wanted))[:200]
    results, freed_total, matched_total = [], 0, 0
    for w in wanted:
        letter = w[:1].upper()
        entries = _recycle_entries(letter)
        hits = [e for e in entries if e["orig"] == w]
        freed = 0
        for e in hits:
            sz = e["size"] or _size_of(e["r"])
            ok_i = rm(e["i"])
            ok_r = True
            if os.path.exists(e["r"]):
                ok_r = rm(e["r"])
            if ok_i and ok_r:
                freed += sz
                matched_total += 1
            # 失败项不计量、如实跳过：$I/$R 任一删除失败都不会计入释放量
        if not hits:
            results.append({"path": w, "matched": 0,
                            "note": "回收站里没有这个路径的条目（可能已被清空）"})
        else:
            freed_total += freed
            results.append({"path": w, "matched": len(hits), "freed": freed})
    return {"results": results, "freed": freed_total,
            "matched": matched_total,
            "freed_h": human(freed_total)}


def _size_of(path: str) -> int:
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            return os.path.getsize(path)
        if os.path.isdir(path):
            return _size_of_dir(path)
    except OSError:
        pass
    return 0


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


def _size_of_dir_budgeted(path: str, max_entries: int = 20000) -> tuple[int, bool]:
    """带条目预算的目录体积。返回 (字节数, 是否因超预算被截断)。
    截断时字节数是**下限**，前端统一按「约」处理。"""
    total, seen = 0, 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for f in files:
            seen += 1
            if seen > max_entries:
                return total, True
            try:
                fp = os.path.join(root, f)
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total, False


# ---- 删除游戏前的存档查询 ----
# 只读操作：在几个「游戏最爱把存档放这」的位置里，找名字与这款游戏相关的目录。
# 匹配是启发式的（名字包含安装目录名或 AppID），所以结果叫「疑似存档」，
# 前端也如实写「建议先备份」——宁可提示了不需要的，也不漏掉要命的。
_GAME_SAVE_ROOTS = (
    ("文档", "~/Documents"),
    ("文档\\My Games", "~/Documents/My Games"),
    ("AppData\\Roaming", "~/AppData/Roaming"),
    ("AppData\\Local", "~/AppData/Local"),
)


def find_game_save_candidates(install_path: str,
                              appid: str = "") -> list[dict]:
    install_path = os.path.abspath(install_path)
    base = os.path.basename(install_path.rstrip("\\/"))
    tokens = set()
    if base:
        tokens.add(base.lower().replace(" ", ""))
        for w in re.split(r"[\s_\-]+", base):
            if len(w) >= 4 and not w.isdigit():
                tokens.add(w.lower())
    appid = str(appid or "").strip()
    if appid.isdigit():
        tokens.add(appid)
    tokens.discard("")
    if not tokens:
        return []
    found = []
    for label, root in _GAME_SAVE_ROOTS:
        root = os.path.expanduser(root)
        try:
            with os.scandir(root) as it:
                for e in it:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                    name = e.name.lower()
                    if not any(t in name for t in tokens):
                        continue
                    # 排除把安装目录本身误当存档（不太可能出现在这些根下，但求稳）
                    if os.path.abspath(e.path).lower() == install_path.lower():
                        continue
                    sz, truncated = _size_of_dir_budgeted(e.path)
                    if sz == 0:
                        continue          # 0 字节的空目录只会制造噪音
                    found.append({"path": e.path, "where": label,
                                  "size": sz, "size_h": human(sz),
                                  "truncated": truncated})
        except OSError:
            continue
    found.sort(key=lambda x: -x["size"])
    return found[:5]


def _is_game_body_path(path: str) -> bool:
    """这个路径是不是「游戏本体」目录（steamapps\\common 下一层 / Epic / GOG 安装路径）。

    游戏本体是同一项目的整体：勾选去重时优先整删，不继续拆分 ——
    拆出子项单独删既不省空间（整款游戏还在）也不安全（漏删/误删一半）。
    """
    low = os.path.normpath(str(path or "")).lower()
    if not low or ":" not in low:
        return False
    try:
        common, _libs = steam_common_dirs()
        if os.path.dirname(low) in common:
            return True
        other_dirs, _libs2 = game_install_dirs()
        return low in other_dirs
    except Exception:
        return False


def dedup_nested_targets(selected: list[dict]) -> tuple[list[dict], list[dict]]:
    """勾选项包含关系去重：同一项目整删不拆，可拆分项取最小。

    外层路径包含内层勾选项时（占用排行里「大项和子项体积近似」的重复内容）：
    * 外层是**游戏本体** → 同一项目，保留外层整删、跳过它里面的勾选项
      （游戏文件夹拆出子项单独删，剩下的游戏壳毫无意义）；
    * 外层是**回收站** → 同样保留外层：清空回收站会带走里面的一切，
      内层勾选项（占用排行里 $Recycle.Bin 下钻出的行）被它覆盖；
    * 其他项目（压缩包、download 这类**可继续拆分**的目录）→ 保留最小项、
      跳过外层，不重复删除、不重复计体积。
    返回 (保留列表, 被跳过列表)。
    """

    def norm(p) -> str:
        return os.path.normpath(str(p)).rstrip("\\").lower() if p else ""

    paths = [norm(t.get("path")) for t in selected]
    is_game = [_is_game_body_path(t.get("path")) for t in selected]
    is_bin = [t.get("kind") == "recyclebin" for t in selected]
    dropped: dict[int, dict] = {}
    for i, t in enumerate(selected):
        p = paths[i]
        if not p or i in dropped:
            continue
        desc = [j for j, q in enumerate(paths)
                if j != i and q and q != p and q.startswith(p + "\\")
                and j not in dropped]
        if not desc:
            continue
        if is_game[i] or is_bin[i]:
            # 同一项目整删不拆：外层（游戏本体/回收站）会一并覆盖内层
            for j in desc:
                dropped[j] = {"label": selected[j].get("label")
                              or selected[j].get("id", ""),
                              "path": selected[j].get("path", ""),
                              "kept": t.get("path", ""),
                              "reason": ("已在同一游戏本体内，整删不拆分"
                                         if is_game[i] else
                                         "清空回收站会一并覆盖该勾选项")}
        else:
            # 可拆分项目：保留最小项，跳过外层
            inner = selected[desc[0]]
            dropped[i] = {"label": t.get("label") or t.get("id", ""),
                          "path": t.get("path", ""),
                          "kept": inner.get("path", ""),
                          "reason": "已包含更小的勾选项，只清理最小项"}
    kept = [t for i, t in enumerate(selected) if i not in dropped]
    merged = [dropped[i] for i in sorted(dropped)]
    return kept, merged


def _purge_recycle_bin(letter: str) -> dict:
    """直接删 {letter}:\\$Recycle.Bin 下各 SID 目录里的 $I/$R 文件。

    为什么不用 Clear-RecycleBin：它对非系统盘有「静默无效」的已知缺陷 ——
    本机实测（2026-10-15，D 盘）：命令成功返回、进度条照走，12 个文件
    原样全在；C 盘（系统盘）却正常。用户看到的「E 盘回收站清不掉」就是它。
    直接删文件与 recycle-purge 两段式同一把尺：普通权限能删当前用户 SID
    的内容，其他 SID 会 PermissionError（管理员运行时可一并清掉）。
    desktop.ini 等结构文件不动。
    返回 deleted/failed/freed/fail_notes。"""
    root = os.path.join(letter + ":", "$Recycle.Bin")
    out = {"deleted": 0, "failed": 0, "freed": 0, "fail_notes": []}
    if not os.path.isdir(root):
        return out

    def _rm_any(p: str) -> None:
        """删一个 $I/$R 条目（可能是文件也可能是目录），失败抛 OSError。"""
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
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
                # $R 条目常带隐藏/系统属性或只读位，先清属性再试一次
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


def clean_target(t: dict, only_paths: list | None = None,
                 drive: str | None = None, trash: bool = False) -> dict:
    """删除一个目标的文件。only_paths 非空时只删这些子项。

    trash=True 时把**风险为「高」**的项改删进回收站（可恢复）。三个要点：
      * 回收站删除不释放空间（同卷只是改名），所以这类项 freed 如实报 0，
        计入 result["trashed"]，由前端说明「清空回收站后才真正释放」；
      * 每个确切被移入的路径记进 result["trash_paths"]（上限 400 条），
        供「清空所选项的回收站」按原始路径**精确**匹配，绝不动回收站
        里其它内容；
      * 中/低风险项不受影响——缓存类东西进回收站只会白白占地方。
    """
    t0 = time.time()
    path = expand(t["path"], drive)
    kind = t.get("kind", "tree")
    letter = (drive or "").rstrip(":").upper()
    use_trash = bool(trash) and t.get("risk") == "高" and kind != "recyclebin"
    # 会再生吗：rebuild 文案里带「自动重建」= 缓存类（删了会回来）；
    # 以「不重建」开头 = 一次性文件（删了就没有）；都没有 = 不确定
    # （自动发现 / 自定义目录），前端按「未分类」如实展示，不硬归类。
    rb_txt = str(t.get("rebuild") or "").strip()
    result = {
        "id": target_id_for(t, drive if is_drive_specific(t) else None),
        "target_id": t["id"],
        # rule_id：英文界面按它在 kb-en.json 里找识别名 / 长文本覆盖
        "rule_id": "target:" + str(t.get("base_id", t["id"])),
        "label": expand(t.get("label", t["id"]), drive),
        "category": t.get("category", "其它"),
        "path": path, "drive": letter,
        "before": 0, "after": 0, "freed": 0,
        "deleted": 0, "failed": 0, "skipped": [], "error": None,
        "seconds": 0.0, "trashed": 0, "trash_errors": [], "trash_paths": [],
        "partial": 0, "partial_notes": [],
        "renews": (True if "自动重建" in rb_txt
                   else False if rb_txt.startswith("不重建") else None),
    }

    ok, why = check_deletable(path, kind)
    if not ok:
        result["error"] = why
        return result

    if kind == "recyclebin":
        # 统计在普通权限下可能被拒（读其他 SID 的回收站内容需要管理员），
        # 但删当前用户 SID 的文件不需要 —— 清空照常执行，统计失败要如实
        # 告诉用户「释放量未知」，不能静默当 0。
        m = measure_target(t, drive)
        before = m.get("size", 0)
        if m.get("error"):
            result["partial_notes"] = [m["error"]]
        if only_paths:
            # only_paths 通道的安全护栏（2026-10-20 审计）：这里的 p 来自请求体，
            # 但 rm() 没有其它防线 —— 必须校验每条路径都位于**本目标对应盘**的
            # $Recycle.Bin 之内，绝不信任前端只传回收站里的路径。
            # 这与 /clean 其它通道「服务端重新校验，绝不信任前端」的边界一致。
            dl = expand(str(t.get("drive_letter") or ""), drive).rstrip(":") or letter
            bin_root = os.path.normpath(dl + ":\\$Recycle.Bin").lower() + "\\"
            for p in only_paths:
                plow = os.path.normpath(os.path.abspath(str(p))).lower()
                if not plow.startswith(bin_root) or plow == bin_root.rstrip("\\"):
                    result["failed"] += 1
                    result["skipped"].append(
                        {"path": str(p)[:200], "reason": "拒绝：路径不在本盘回收站内"})
                    continue
                sz = _size_of(p)
                if rm(p):
                    result["deleted"] += 1
                    result["freed"] += sz
                    # 成对清理 $I 元数据，避免留下孤儿记录
                    meta = os.path.join(os.path.dirname(p),
                                        "$I" + os.path.basename(p)[2:])
                    if os.path.exists(meta):
                        rm(meta)
                else:
                    result["failed"] += 1
                    result["skipped"].append(os.path.basename(p))
            result["after"] = max(0, before - result["freed"])
        else:
            d = expand(str(t.get("drive_letter") or ""), drive).rstrip(":") or letter
            purged = _purge_recycle_bin(d)
            result["deleted"] = purged["deleted"]
            result["freed"] = purged["freed"]
            if purged["failed"]:
                # 兜底：Clear-RecycleBin 对系统盘有效（直删被拒的条目可能被
                # 它清掉）；非系统盘它无效但无害。只在没有直删成果时才计入
                # 它的成败，避免统计口径混乱。
                run_ps("Clear-RecycleBin -DriveLetter "
                       + d.replace("'", "''") + " -Force -ErrorAction SilentlyContinue")
                m2 = measure_target(t, drive)
                if m2.get("size", 0) < m.get("size", 0):
                    result["freed"] += m.get("size", 0) - m2.get("size", 0)
                    purged["failed"] = 0
            if purged["failed"]:
                result["failed"] = purged["failed"]
                result["partial_notes"].append(
                    f"{purged['failed']} 个文件因权限不足未删除（其他账户或系统保护），"
                    "以管理员身份运行可彻底清空")
            for note in purged["fail_notes"]:
                result["partial_notes"].append("跳过: " + note)
            if purged["deleted"] == 0 and purged["failed"] == 0:
                # 回收站本来就是空的：不是错误，如实告诉前端
                result["partial_notes"].append("该盘回收站没有可清空的内容")
        after = measure_target(t, drive).get("size", 0)
        result["after"] = after
        # 逐文件算过 freed（recycle-purge / 整盘直删）就以它为准；
        # 没算过（统计被拒前后都为 0 等）用前后统计差兜底
        result["freed"] = result["freed"] or max(0, before - after)
        if result["freed"] > 0 and not result["deleted"]:
            result["deleted"] = 1  # 整盘清空的成功形态
        result.update(before=before, seconds=round(time.time() - t0, 2))
        return result

    if not os.path.isdir(path):
        result["error"] = "路径不存在"
        return result

    # scandir 本身就可能抛 PermissionError（例如普通权限下的 C:\\Windows\\Temp）
    try:
        with os.scandir(path) as it:
            if kind == "files":
                glob = t.get("glob", "*")
                items = [e.path for e in it
                         if e.is_file(follow_symlinks=False)
                         and fnmatch.fnmatch(e.name, glob)]
            else:
                items = [e.path for e in it]
    except PermissionError:
        result["error"] = "拒绝访问：需要管理员权限（或该目录受系统保护）"
        result["seconds"] = round(time.time() - t0, 2)
        return result
    except OSError as exc:
        result["error"] = f"无法读取目录：{exc.strerror or exc}"
        result["seconds"] = round(time.time() - t0, 2)
        return result

    if only_paths:
        keep = {os.path.abspath(p).lower() for p in only_paths}
        items = [p for p in items if os.path.abspath(p).lower() in keep]

    result["before"] = sum(_size_of(p) for p in items)

    # P1-1 预检：tree 类整目录删除前，先确认目录内没有确定被占用的文件。
    # 命中即整体中止 —— 宁可一次都不删，也不留下删了一半的残缺安装目录。
    # （只_paths 的精细删除与回收站通道不走这条：它们本来就按单项失败处理。）
    if kind == "tree" and not only_paths and not use_trash:
        locked = _precheck_locked_files(path)
        if locked:
            shown = "；".join(os.path.basename(p) for p in locked[:_LOCK_PRECHECK_REPORT_MAX])
            result["error"] = (
                f"目标内有 {len(locked)} 个文件正被其他程序占用，"
                f"已整体中止、未删除任何内容（请先关闭相关程序）：{shown}"
                f"{'…' if len(locked) > _LOCK_PRECHECK_REPORT_MAX else ''}")
            tid = f"{t.get('base_id') or ''} {t.get('id') or ''}"
            result["lock_hint"] = next(
                (h for key, h in _LOCK_OWNER_HINTS.items() if key in tid), None)
            result["locked_files"] = locked
            result["seconds"] = round(time.time() - t0, 2)
            return result

    atomic = kind in ("tree", "files")
    for item in items:
        if use_trash:
            sz = _size_of(item)
            ok, why = rm_trash(item)
            if ok:
                result["deleted"] += 1
                result["trashed"] += 1     # 空间没释放，不计入 freed
                if len(result["trash_paths"]) < 400:
                    result["trash_paths"].append(
                        os.path.normpath(os.path.abspath(str(item))))
            else:
                result["failed"] += 1
                result["trash_errors"].append(f"{os.path.basename(item)}：{why}")
                if len(result["skipped"]) < 20:
                    result["skipped"].append(os.path.basename(item))
        elif atomic:
            sz = _size_of(item)
            if os.path.isdir(item) and not os.path.islink(item):
                # P1-1：目录走三态删除 —— 被占用时先改名失败（零副作用），
                # 删不干净时如实报「部分删除」，不再静默假装删干净。
                full, partial, note = rm_tree_strict(item)
                if full:
                    result["deleted"] += 1
                    result["freed"] += sz
                elif partial:
                    result["partial"] += 1
                    result["partial_notes"].append(
                        f"{os.path.basename(item)}：{note}")
                    if len(result["skipped"]) < 20:
                        result["skipped"].append(os.path.basename(item))
                else:
                    result["failed"] += 1
                    if len(result["skipped"]) < 20:
                        result["skipped"].append(
                            f"{os.path.basename(item)}（{note}）")
            elif rm(item):
                result["deleted"] += 1
                result["freed"] += sz
            else:
                result["failed"] += 1
                result["freed"] += max(0, sz - _size_of(item))
                if len(result["skipped"]) < 20:
                    result["skipped"].append(os.path.basename(item))
        else:
            if rm(item):
                result["deleted"] += 1
            else:
                result["failed"] += 1
                if len(result["skipped"]) < 20:
                    result["skipped"].append(os.path.basename(item))

    if use_trash:
        result["after"] = max(0, result["before"] - result["freed"])
        if result["trash_errors"]:
            result["error"] = "；".join(result["trash_errors"][:3])
    elif atomic:
        result["after"] = max(0, result["before"] - result["freed"])
    else:
        result["after"] = _size_of_dir(path)
        result["freed"] = max(0, result["before"] - result["after"])
    result["seconds"] = round(time.time() - t0, 2)
    return result


# ==================== 被占用文件：占用探测与进程候选 ====================
# 这里刻意把两件事分开，因为它们的可靠度天差地别：
#   * 「这个文件被占着吗」—— **可以可靠回答**：以「不共享」方式请求句柄，
#     拿到共享冲突就说明别的进程正占着它。
#   * 「是哪个进程占的」—— **只能猜**。Windows 没有公开 API 能把「句柄」反查成
#     进程（要 Sysinternals handle.exe，或管理员权限下的 openfiles，二者都不是
#     本工具能依赖的东西）。所以下面返回的是**候选**、按可能性排序，
#     交给使用者自己判断。工具**绝不代关进程** —— 关错了可能是别人没保存的文档。
LOCK_PROBE_LIMIT = 80       # 一次最多探这么多条路径（前端传来的通常远少于它）
LOCK_CANDIDATE_LIMIT = 15   # 去重后展示的候选上限：太长反而让人无从下手
# 脚本按「(目标 × 进程)」成行返回，同一进程命中多个目标就会重复。
# 所以脚本侧先卡一个宽松上限，去重后再按 LOCK_CANDIDATE_LIMIT 裁。
LOCK_RAW_ROW_LIMIT = 120

# CreateFileW 的参数常量
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
    """探测一个路径当前是否被别的进程占用。

    文件：请求「读写 + 不共享」句柄，共享冲突 = 被占用（可靠）。
    目录：只能请求「列目录 + 不共享」，被占用时同样返回共享冲突，
          但资源管理器正打开着也会如此，所以只作参考。
    权限不足时如实返回「无法判定」，**不要猜成「没被占用」** ——
    那会让使用者以为文件已经可以删了。
    """
    out = {"path": path, "exists": os.path.exists(path), "kind": "file",
           "locked": None, "why": ""}
    if not out["exists"]:
        out["why"] = "路径不存在（可能已经删掉了）"
        return out
    if not IS_WINDOWS:
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
    """探测这些路径的占用状态，并给出「可能是谁占用」的候选清单。

    返回值里的 candidates 是**启发式推断**，不是确证：先按「程序本体在该目录下」
    或「命令行里出现了完整路径」这类强信号排序，其余同名匹配排在后面并标记为非强信号。
    """
    clean: list[str] = []
    for p in (paths or []):
        if isinstance(p, str) and p.strip():
            clean.append(p.strip())
    truncated = len(clean) > LOCK_PROBE_LIMIT
    clean = clean[:LOCK_PROBE_LIMIT]

    results = [probe_locked(p) for p in clean]
    candidates: list[dict] = []
    ps_error = None
    if clean and IS_WINDOWS:
        literal = ",".join("'" + p.replace("'", "''") + "'" for p in clean)
        script = (PS_LOCK_HOLDERS
                  .replace("__TARGETS__", literal)
                  .replace("__LIMIT__", str(LOCK_RAW_ROW_LIMIT)))
        r = run_ps(script, timeout=60)
        if isinstance(r, dict) and "error" in r:
            ps_error = str(r["error"])
        else:
            raw = [c for c in _as_list(r) if isinstance(c, dict)]
            # 同一个进程可能命中多个目标 —— 按 PID 合并成一条，并记下命中数。
            # 不合并的话，15 行的上限会被同一个进程的重复行刷满。
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

# 自动发现的深度上限 = 2（盘符根的直接子目录，及其子目录）。
# 这里刻意用下面两段展开的循环实现，而不是递归 + 深度常量：
# 无界递归会在 C:\ 这种盘上把每个目录都扫一遍，实测拖慢整轮扫描，
# 而且第三层往下的归属判断已经不可靠（分不清是哪个软件的内部缓存）。
AUTO_DISCOVER_DEPTH_PENALTY = 12
# 自动发现的候选上限。清单太长反而让人无从下手，且会拖慢扫描。
AUTO_DISCOVER_LIMIT = 12


def _auto_excluded(path: str, segments: list[str]) -> bool:
    """判断候选是否应被自动发现排除。"""
    low_segs = [s.lower() for s in segments]
    # 回收站：整棵树都不进（$Recycle.Bin 及其内部 SID 子目录）
    if any(s.startswith("$recycle.bin") or s.startswith("$recycle") for s in low_segs):
        return True
    for seg in low_segs:
        if seg in ("programdata", "appdata", "windows", "system volume information"):
            return True
        if seg.startswith("$windows."):
            return True
        # Program Files 下装的是**程序本体**，不是缓存。
        # 这里曾被放过，导致自动发现把 C:\Program Files (x86)\Adobe、
        # C:\Program Files\Microsoft OneDrive 这类程序目录标成「可清理」——
        # 名字命中知识库的「创作软件/云盘」规则，但删掉就是卸载软件。
        # 清理目标必须由预定义清单（精确到缓存子目录）给出，绝不由名字猜。
        if seg in ("program files", "program files (x86)"):
            return True
    name = os.path.basename(path.rstrip("\\/")).lower()
    # 回收站的元数据与 GUID/SID 形态的目录名，没有解释价值
    if name.startswith("$i") or name.startswith("$r"):
        return True
    if len(name) > 30 and name.count("-") >= 3:
        return True
    return False


def _looks_like_installed_program(path: str) -> bool:
    """判断目录里装的是**程序本体**（而不是缓存）。

    知识库关键词是给「缓存目录」写的，撞上安装目录会误判
    （短名目录可命中 photoshop 规则，Program Files 下的目录同样会），
    所以自动发现判出「可清理」前必须先排除程序本体。

    判定只看通用特征，不看名字（名字正是误判来源）：
      * 顶层存在 Uninstall*.exe / unins*.exe —— 安装器留下的卸载程序；
      * 两个以上「版本号形态」的子目录 —— 同时覆盖年份命名
        （Adobe Photoshop 2024 / …2023）与四段式版号
        （WPS Office\\12.1.0.x 与 12.1.0.y）。

    刻意**不用**的两条判据：
      * 「顶层 .exe 数量」——下载目录里堆着安装包，按数量算会误判成程序安装；
      * 「子目录里有卸载程序」——装过又留下的软件目录都带卸载器，同样误判。
    卸载程序只在**顶层**出现才算数。
    """
    if not os.path.isdir(path):
        return False
    try:
        entries = list(os.scandir(path))
    except OSError:
        return False
    ver_dirs = 0
    for e in entries:
        try:
            n = e.name.lower()
            if e.is_file():
                if n.startswith(("uninstall", "unins")):
                    return True
                continue
            if not e.is_dir():
                continue
            # 版本号形态：年份（2024）或四段式版号（12.1.0.x）
            if (re.search(r"(19|20)\d{2}", n)
                    or re.search(r"^\d+(\.\d+){2,}$", n)):
                ver_dirs += 1
        except OSError:
            continue
    return ver_dirs >= 2


def auto_discovered_targets(drive: str, limit: int = AUTO_DISCOVER_LIMIT) -> list[dict]:
    """在盘符顶层自动寻找「知识库识别为可清理」的目录。

    为什么需要它：预定义目标列表只覆盖常见系统位置（Windows\\Temp、回收站等），
    但磁盘占用的大头往往是**长尾的个人软件**（Telegram、夸克、Photoshop……）。
    没有自动发现时，D 盘只能列出 3 个可清理项，而实际有几十 GB 可回收。

    安全与体验约束：
      * 只接受知识库判定为 cleanable 且置信度非 low 的候选
      * 未识别的目录一律跳过（宁可少提示，也不猜）
      * 排除回收站内部、ProgramData、AppData、Windows 等无价值或已有专门处理的位置
      * 同名的父/子目录只保留最外层的那个，避免「steam」和「steam\\appcache」同时出现
      * 限制候选总数，清单太长反而让人无从下手
    """
    letter = (drive or "C").rstrip(":").upper()
    root = letter + ":\\"
    if not IS_WINDOWS or not os.path.isdir(root):
        return []
    if not load_filetypes().get("folder_rules"):
        return []

    out: list[dict] = []
    seen_paths: set[str] = set()
    seen_keys: set[str] = set()          # 用「识别归属 + 名称」去重

    def consider(path: str, depth: int, parent_hint: str) -> None:
        if len(out) >= limit * 3:        # 先多收，排序后再截断
            return
        low = os.path.normpath(path).lower()
        if low in seen_paths:
            return
        rel = os.path.relpath(path, root)
        segs = [s for s in rel.split("\\") if s]
        if _auto_excluded(path, segs):
            return
        name = os.path.basename(path.rstrip("\\/"))
        if not name:
            return
        info = identify(name, path, "dir")
        if not (info.get("recognized") and info.get("cleanable")) and parent_hint:
            # 名称无意义时退一步看父目录（例如 …\Telegram\tdata 里的 tdata）
            info = identify(parent_hint, path, "dir")
        if not (info.get("recognized") and info.get("cleanable")):
            return
        if info.get("confidence") == "low":
            return
        # 装的是程序本体就绝不推荐清理（见 _looks_like_installed_program 的注释）
        if _looks_like_installed_program(path):
            return
        # 去重键：同一类内容只提示一次（避免 Quark / quark-cloud-drive / QuarkCloudDrive 并列）
        key = f"{info.get('rule_id')}|{name.lower()}"
        if key in seen_keys:
            return
        ok, _why = check_deletable(path, "tree")
        if not ok:
            return
        seen_paths.add(low)
        seen_keys.add(key)
        out.append({
            "id": "auto-" + rel.replace("\\", "-").replace(" ", "_").lower(),
            "base_id": "auto",
            "label": f"{name}（自动发现）",
            "path": path,
            "category": info.get("category") or "缓存",
            "risk": info.get("risk") or "中",
            "needs_admin": False,
            "kind": "tree",
            "drive_letter": letter,
            "discovered": True,
            "auto_score": 100 - depth * AUTO_DISCOVER_DEPTH_PENALTY,
            "short": info.get("what") or "",
            "what": info.get("what") or "",
            "rebuild": info.get("advice") or "",
            "impact": info.get("advice") or "",
            "keep_if": info.get("tip") or "",
        })

    try:
        level1 = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []

    # 第一层：盘符根目录的直接子目录
    for e in level1:
        consider(e.path, 0, "")

    # 第二层：只对「自身没被识别为可清理」的一级目录再下一层。
    #
    # 这里必须判「父目录是否本身就是可清理候选」，不能只看它有没有被收进 out。
    # 反例（实测）：某下载目录本身命中下载目录规则，但它的 8 个子目录
    # （视频剪辑草稿等）被逐个又列了一遍 ——
    # 既冗余，又把**用户自己的创作草稿**推成了「可清理」。
    # 父目录已经代表这块空间了，就不该再拆开推荐它的内容。
    for e in level1:
        low = os.path.normpath(e.path).lower()
        if low in seen_paths:
            continue                      # 父目录已整体可清理，无需再看子目录
        rel = os.path.relpath(e.path, root)
        segs = [s for s in rel.split("\\") if s]
        if _auto_excluded(e.path, segs):
            continue
        pinfo = identify(e.name, e.path, "dir")
        # 顺序很重要：先判「装的是程序」，再判「本身是候选」。
        # 反过来写的话，一个同时命中两条的一级目录会掉进 else 分支被继续下钻 ——
        # 安装器仓库就是如此：它是安装器仓库（_looks_like_installed_program 为真），
        # 于是被继续下钻，把 8 个子目录挨个推成「可清理」。
        if _looks_like_installed_program(e.path):
            continue                      # 装的是程序本体，不下钻也不推荐其子项
        if (pinfo.get("recognized") and pinfo.get("cleanable")
                and pinfo.get("confidence") != "low"):
            continue                      # 父目录本身就是候选，不再拆它的子项
        try:
            sub = [s for s in os.scandir(e.path) if s.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        for s in sub[:60]:
            consider(s.path, 1, e.name)

    # 浅层优先、同名合并后按深度与名称排序
    out.sort(key=lambda x: (-x["auto_score"], x["path"].lower()))
    return out[:limit]


def build_targets(drive: str | None, custom_path: str | None = None) -> list[dict]:
    """按所选盘符组装目标列表。"""
    cfg = load_targets()
    letter = (drive or "").rstrip(":").upper()
    sys_drive = is_system_drive(letter) if letter else True
    out: list[dict] = []
    seen: set[str] = set()

    def push(item: dict) -> None:
        key = "|".join([
            os.path.normpath(expand(item.get("path", ""), drive or None)).lower(),
            str(item.get("glob", "")), str(item.get("kind", "tree")),
        ])
        if key in seen:
            return
        seen.add(key)
        out.append(item)

    for t in cfg.get("targets", []):
        if not isinstance(t, dict) or "id" not in t or "path" not in t:
            continue
        if is_drive_specific(t) and not letter:
            continue
        # 标了 system_only 的目标只在装有 Windows 的盘上出现，
        # 避免数据盘（如 D:）列出 D:\Windows\Temp 这类根本不存在的项
        if t.get("system_only") and not sys_drive:
            continue
        # 指向用户目录的目标（%LOCALAPPDATA% 等）同理：它们与盘符无关，
        # 永远落在系统盘上。不拦掉就会在 D/E 盘列出「D 盘的 pip 缓存」，
        # 而路径其实是 C:\Users\…（用户实测反馈过这个现象）。
        if is_user_local(t) and not sys_drive:
            continue
        item = dict(t)
        if letter and is_drive_specific(item):
            item["id"] = f"{t['id']}@{letter}"
        item["base_id"] = t["id"]
        push(item)

    # 用户保存的自定义规则不再作为清理目标出现 ——
    # 它们是「一组被勾选的固定项」，只在侧栏里以便捷方式套用，见 custom-rules 接口。

    if custom_path:
        cp = os.path.abspath(expand(custom_path, letter or None))
        for t in discover_junk_folders(cp):
            push(t)

    # 自动发现：把本盘上「被知识库识别为可清理」的应用目录也纳入清理清单。
    # 这是让数据盘真正有用的关键 —— 否则 D/E 盘只能列出 \Temp 和回收站，
    # 而 Telegram / 夸克 / Photoshop 这些几十 GB 的大户都不在清单里。
    if letter:
        for t in auto_discovered_targets(letter):
            push(t)

    # 资源包补足：把包里对同名清理项的说明合并进来（只补空缺字段）。
    # 放在最后统一做，这样内置目标与自动发现目标都能被同一套包覆盖。
    # 用户自定义覆盖再往后一步：三条优先级里用户排最前（最后应用、最高优先）。
    user_ov = load_overrides().get("targets", {})
    for t in out:
        try:
            packs_enrich_target(t, drive)
        except Exception:
            pass
        _apply_target_override(t, user_ov)

    return out


# 自定义目录下的已知垃圾位置：固定子路径 + 顶层目录名两类。
# 这份清单生成的是**可清理**目标 —— 宁可漏掉也不能把用户数据当垃圾，
# 所以只收「删错也无大碍、内容可重建」的名字；Downloads / Documents
# 之类的常见目录一律不收。
JUNK_SUBPATHS = ["$Recycle.Bin", "Temp", "tmp", "Config.Msi", "$WinREAgent"]
JUNK_FOLDER_NAMES = {"temp", "tmp", "cache", "caches", "log", "logs",
                     "crashdumps", "crash dumps", "dumps", "minidumps"}
# 自动补齐的目标公共字段：识别来源标记 + 默认低风险可清理
_CUSTOM_TEMPLATE = {
    "category": "缓存",
    "needs_admin": False,
    "cleanable": True,
    "discovered": True,
    "custom": True,
}


def discover_junk_folders(root: str) -> list[dict]:
    """在自定义目录下发现已知的垃圾文件夹，生成临时目标。"""
    out: list[dict] = []
    if not os.path.isdir(root):
        return out
    root = os.path.abspath(root)

    for sub in JUNK_SUBPATHS:
        p = os.path.join(root, sub)
        if not os.path.isdir(p):
            continue
        is_bin = sub == "$Recycle.Bin"
        out.append({
            **_CUSTOM_TEMPLATE,
            "id": "custom-" + sub.replace("\\", "-").replace("$", "").replace(".", "").lower(),
            "base_id": "custom",
            "label": f"{sub} —— 位于 {root}",
            "path": p, "risk": "低",
            "kind": "recyclebin" if is_bin else "tree",
            "drive_letter": os.path.splitdrive(p)[0].rstrip(":"),
            "short": f"自定义目录中发现的 {sub}。",
            "what": f"在 {root} 下发现了 {sub}，它属于常见的可清理位置。",
            "rebuild": "通常会自动重建。",
            "impact": "清理后对应程序会重新生成所需内容。",
            "keep_if": "不确定用途时先保留。",
        })

    try:
        entries = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        entries = []
    for e in entries:
        if e.name.lower() not in JUNK_FOLDER_NAMES:
            continue
        out.append({
            **_CUSTOM_TEMPLATE,
            "id": "custom-dir-" + e.name.lower().strip(".").replace(" ", "-"),
            "base_id": "custom",
            "label": f"{e.name} —— 位于 {root}",
            "path": e.path, "risk": "中",
            "kind": "tree",
            "drive_letter": os.path.splitdrive(e.path)[0].rstrip(":"),
            "short": f"自定义目录中名为 {e.name} 的文件夹，按名称判断可能是临时或缓存数据。",
            "what": f"按名称判断，{e.name} 通常是临时文件或缓存目录。O-Cleaner 无法确认它的具体用途，"
                    f"所以标为中风险并默认不勾选。",
            "rebuild": "不确定 —— 取决于哪个程序创建了它。",
            "impact": "如果有程序把工作数据放在这里，清理可能导致它出错。",
            "keep_if": "不确定这个目录是谁建的，就先保留。",
        })
    return out


# ======================================================================
#  单位换算
# ======================================================================

def resolve_units(units: list[dict]) -> list[dict]:
    """解析 bytes_per_unit，支持 '1/3 * movie4k' 这类引用其它单位的表达式。"""
    by_id = {u.get("id"): u for u in units if u.get("id")}
    resolved: dict[str, float] = {}

    def resolve(uid: str, chain: tuple = ()) -> float:
        if uid in resolved:
            return resolved[uid]
        u = by_id.get(uid)
        if not u or uid in chain:
            return 0.0
        val = u.get("bytes_per_unit", 0)
        out = 0.0
        if isinstance(val, (int, float)):
            out = float(val)
        elif isinstance(val, str):
            expr = val.strip()
            for other_id in by_id:
                if other_id != uid and other_id in expr:
                    expr = expr.replace(other_id, repr(resolve(other_id, chain + (uid,))))
            try:
                out = float(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 本地配置表达式
            except Exception:
                out = 0.0
        resolved[uid] = out
        return out

    for uid in by_id:
        resolve(uid)
    return [{**u, "bytes": resolved.get(u.get("id"), 0.0)} for u in units]


def fmt_num(v: float) -> str:
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:,.1f}".rstrip("0").rstrip(".")
    if v >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3f}".rstrip("0").rstrip(".")


def convert_bytes(nbytes: float, units: list[dict], max_units: int = 6,
                  only: list[str] | None = None) -> list[dict]:
    out = []
    for u in units:
        if u.get("unlimited"):
            continue
        if only and u.get("id") not in only:
            continue
        b = u.get("bytes") or 0
        if b <= 0:
            continue
        cnt = nbytes / b
        if cnt < 0.01:
            continue
        out.append({
            "id": u.get("id"), "label": u.get("label") or u.get("id"),
            "plural": u.get("plural") or u.get("label") or u.get("id"),
            "count": round(cnt, 4), "count_h": fmt_num(cnt),
            "category": u.get("category", "其它"), "note": u.get("note", ""),
            "bytes_per_unit": b, "bytes_per_unit_h": human(b),
        })
    out.sort(key=lambda x: x["count"])
    return out[:max_units]


# ======================================================================
#  历史统计与换算
# ======================================================================

def history_stats(unit_ids: list[str] | None = None) -> dict:
    hist = load_history()
    total_freed = 0
    total_failed = 0
    run_count = 0
    by_category: dict[str, int] = {}
    by_drive: dict[str, int] = {}
    by_target: dict[str, dict] = {}
    timeline = []
    first_ts = last_ts = None

    for h in hist:
        runs = h.get("results") or []
        if not runs:
            continue
        run_count += 1
        total_freed += int(h.get("freed") or 0)
        for r in runs:
            f = int(r.get("freed") or 0)
            total_failed += int(r.get("failed") or 0)
            cat = r.get("category") or "其它"
            by_category[cat] = by_category.get(cat, 0) + f
            dv = (r.get("drive") or "").strip()
            if dv:
                by_drive[dv] = by_drive.get(dv, 0) + f
            tid = r.get("target_id") or r.get("id")
            if tid:
                slot = by_target.setdefault(tid, {"label": r.get("label", tid), "freed": 0})
                slot["freed"] += f
        timeline.append({"time": h.get("time", ""), "freed": int(h.get("freed") or 0),
                         "trigger": h.get("trigger", ""),
                         "targets": len(runs)})
        ts = h.get("time")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

    cfg = load_convert()
    units = resolve_units(cfg.get("units", []))
    disp = cfg.get("display", {}) or {}
    conversions = convert_bytes(total_freed, units,
                                int(disp.get("max_units", 6) or 6), unit_ids)

    top_targets = sorted(
        ({"id": k, "label": v["label"], "freed": v["freed"], "freed_h": human(v["freed"])}
         for k, v in by_target.items()),
        key=lambda x: -x["freed"])[:12]

    return {
        "run_count": run_count,
        "total_freed": total_freed, "total_freed_h": human(total_freed),
        "total_failed": total_failed,
        "first_time": first_ts, "last_time": last_ts,
        "by_category": [{"name": k, "freed": v, "freed_h": human(v)}
                        for k, v in sorted(by_category.items(), key=lambda x: -x[1])],
        "by_drive": [{"drive": k, "freed": v, "freed_h": human(v)}
                     for k, v in sorted(by_drive.items(), key=lambda x: -x[1])],
        "top_targets": top_targets,
        "timeline": timeline[:30],
        "conversions": conversions,
        "units": [{"id": u.get("id"), "label": u.get("label"),
                   "plural": u.get("plural") or u.get("label"),
                   "category": u.get("category", "其它"), "note": u.get("note", ""),
                   "bytes": u.get("bytes", 0), "bytes_h": human(u.get("bytes") or 0),
                   "unlimited": bool(u.get("unlimited")),
                   "raw": u.get("bytes_per_unit")}
                  for u in units],
    }


# ======================================================================
#  定时清理
# ======================================================================

DEFAULT_SCHEDULE = {"enabled": False, "interval_hours": 24, "preset": "safe",
                    "drive": "C", "last_run": 0, "next_run": 0}


def schedule_state() -> dict:
    s = dict(DEFAULT_SCHEDULE)
    s.update(_load_json(SCHEDULE_PATH, {}, use_cache=False) or {})
    return s


def _resolve_preset(targets: list[dict], preset: dict) -> list[str]:
    """把预设解析成目标 id 集合。盘符专属项用带盘符后缀的完整 id 匹配。"""
    sel = preset.get("select", {}) or {}
    base_to_full: dict[str, list[str]] = {}
    for t in targets:
        base_to_full.setdefault(t.get("base_id", t["id"]), []).append(t["id"])

    if "ids" in sel:
        # 显式按 id 指定：完全尊重配置，不做回收站过滤
        # （否则「只清回收站」这类预设会被自己的过滤器清空）
        out: list[str] = []
        for b in sel["ids"]:
            out.extend(base_to_full.get(b, []))
        return out

    risks = set(sel.get("risk", []))
    allow_admin = bool(sel.get("allow_admin", False))
    return [t["id"] for t in targets
            if t.get("risk") in risks and (allow_admin or not t.get("needs_admin"))]


def _schedule_run_interval(s: dict) -> float:
    """把 interval_hours 归一成合法小时数。

    配置可能是手改的、被旧版本写坏的、或前端 bug 传进来的字符串。
    这里一律钳到 [0.25, 24*30]：非法值回落到默认 24 小时，绝不抛异常 ——
    抛异常会让 next_run 不推进，守护线程 30 秒后又来跑一次（见 _run_scheduled）。
    """
    try:
        hrs = float(s.get("interval_hours"))
    except (TypeError, ValueError):
        return float(DEFAULT_SCHEDULE["interval_hours"])
    if not (hrs > 0) or hrs != hrs or hrs == float("inf"):   # 含 NaN / inf
        return float(DEFAULT_SCHEDULE["interval_hours"])
    return max(0.25, min(hrs, 24 * 30))


def _apply_preset_to_targets(targets: list[dict], preset_id: str) -> list[dict]:
    cfg = load_targets()
    preset = next((p for p in cfg.get("presets", []) if p["id"] == preset_id), None)
    if not preset:
        # 未知预设名（被资源包移除 / 手改配置）**不能**回落成「所有不需要管理员的
        # 目标」—— 那会比用户预期宽得多（实测 safe=9 项，回落=22 项），
        # 而定时任务会照这个范围真删。返回空集，由调用方如实报告。
        return []
    ids = set(_resolve_preset(targets, preset))
    return [t for t in targets if t["id"] in ids]


def _scheduler_loop() -> None:
    while True:
        time.sleep(30)
        try:
            s = schedule_state()
            if s.get("enabled"):
                nxt = float(s.get("next_run") or 0)
                if nxt and time.time() >= nxt:
                    # _run_scheduled 内部会锁内重读配置并推进 next_run，
                    # 所以这里传进去的 s 只供它读取 drive/preset 初值。
                    _run_scheduled(s)
        except Exception:
            # 兜底日志：即便 _run_scheduled 自身抛异常（它已有 finally 推进
            # next_run），也不能让守护线程静默空转。
            traceback.print_exc()


def _run_scheduled(s: dict) -> None:
    drive = (s.get("drive") or "C").rstrip(":").upper()
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             "drive": drive,
             "trigger": f"定时自动清理（{drive}:）",
             "results": [], "freed": 0, "failed": 0, "targets": 0}
    try:
        targets = build_targets(drive)
        selected = _apply_preset_to_targets(targets, s.get("preset") or "safe")
        if not selected:
            # 预设找不到 / 展开为空：不清理，只记一条可读的原因。
            # 以前这里会静默跑空并推进 next_run，用户永远不知道定时任务没生效。
            entry["results"].append({
                "id": "-", "label": (f"预设「{s.get('preset')}」不存在或没有匹配项"),
                "path": "", "drive": drive, "freed": 0, "deleted": 0,
                "failed": 0, "skipped": [], "before": 0, "after": 0,
                "seconds": 0.0, "renews": None,
                "error": "本次未执行任何清理（预设无效），请检查定时清理设置",
            })
        entry["targets"] = len(selected)
        for t in selected:
            try:
                r = clean_target(t, drive=drive)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                r = _error_result(t, drive, exc)
            entry["results"].append(r)
            entry["freed"] += r["freed"]
            entry["failed"] += r["failed"]
    finally:
        # 无论本轮成功、失败还是抛异常，next_run 都必须推进 ——
        # 否则守护线程 30 秒后会认为「又到期了」并再删一次。历史上
        # interval_hours 被写成非法值时，这里抛 ValueError 导致 next_run
        # 停在过去，实测在 2 分钟内连续触发了 6 次真实清理。
        entry["freed_h"] = human(entry["freed"])
        append_history(entry)
        with _lock:
            cur = schedule_state()          # 锁内重读，别用过期的快照
            now = time.time()
            hrs = _schedule_run_interval(cur)
            cur["interval_hours"] = hrs     # 顺手把非法值就地修正
            cur["last_run"] = now
            cur["next_run"] = now + hrs * 3600
            _atomic_write(SCHEDULE_PATH, cur)


def _error_result(t: dict, drive: str | None, exc: Exception) -> dict:
    return {"id": target_id_for(t, drive), "target_id": t.get("base_id", t.get("id")),
            "rule_id": "target:" + str(t.get("base_id", t.get("id"))),
            "label": t.get("label", t.get("id")), "category": t.get("category", "其它"),
            "path": expand(t.get("path", ""), drive),
            "drive": (drive or "").rstrip(":").upper(),
            "before": 0, "after": 0, "freed": 0, "deleted": 0, "failed": 0,
            "skipped": [], "seconds": 0.0,
            "renews": None,
            "error": f"内部错误：{type(exc).__name__}: {exc}"}


# ======================================================================
#  HTTP 服务
# ======================================================================

# ======================================================================
#  资源包：可替换的「识别与释义」数据
# ======================================================================
# 内置知识库（filetypes.json / targets.json）覆盖的是通用情况；资源包用来补足
# 剩下的长尾：游戏中文名、Windows 系统项、常见软件缓存的逐条解释。
#
# 两类来源，同名 id 时用户包优先：
#   packs/*.json               随仓库分发，可在 GitHub 上直接下载
#   config/packs-user/*.json   用户导入的包（.gitignore，不随仓库走）
#
# 匹配语义（见 pack_score）：
#   match.any  列表里**任一**项命中即可；不含 "re:" 前缀时按子串匹配，
#              同时比对「文件/目录名」与「完整路径」，两边任一命中都算
#   match.all  列表里**全部**项都必须命中，用来加范围约束
#              （例如「必须在 steamapps\common 之下」）
#   前缀 "re:"  后面的部分按正则处理
#
# 安全边界：资源包**改不动结论**。cleanable 与 risk 永远由内置配置决定 ——
# 包是可以被随便替换的外部数据，不该有影响「删不删」的能力。
# 说明文字（what / effect / keep / rebuild）允许被包覆盖：那只影响可读性，
# 不影响删除范围，而且「只补空缺」会让补全类资源包（如 windows-system）几乎失效。
_PACKS_CACHE: dict = {"key": None, "data": None}
PACK_TYPE_LABELS = {"games": "游戏", "system": "系统", "apps": "软件",
                    "custom": "自定义"}
# packs 分发仓库（公开）：只放资源包数据，不放代码。
# 资源包更新指向独立公开仓库：只放资源包数据，不放代码，与主仓库解耦分发。
# 本仓库 packs/ 与它保持同内容，lint 的 index.json ↔ 磁盘一致性检查两边都适用。
DEFAULT_PACKS_REMOTE = ("https://raw.githubusercontent.com/"
                        "Oerian/O-Cleaner-packs/main")


def load_packs_state() -> dict:
    d = _load_json(PACKS_STATE_PATH, {})
    if not isinstance(d, dict):
        d = {}
    dis = d.get("disabled")
    d["disabled"] = [str(x) for x in dis] if isinstance(dis, list) else []
    if not d.get("remote"):
        d["remote"] = DEFAULT_PACKS_REMOTE
    return d


def save_packs_state(d: dict) -> None:
    _atomic_write(PACKS_STATE_PATH, d)
    _PACKS_CACHE["key"] = None          # 状态变了，缓存作废


def pack_validate(data) -> tuple[bool, str]:
    """校验资源包结构。返回 (是否合法, 说明)。"""
    if not isinstance(data, dict):
        return False, "资源包必须是一个 JSON 对象"
    pid = str(data.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,64}", pid):
        return False, "id 只能由字母、数字、点、下划线、连字符组成（2~64 字符）"
    if not str(data.get("name") or "").strip():
        return False, "缺少 name"
    ents = data.get("entries")
    if not isinstance(ents, list) or not ents:
        return False, "entries 必须是非空数组"
    if len(ents) > 5000:
        return False, f"entries 过多（{len(ents)} 条，上限 5000）"
    for i, e in enumerate(ents):
        if not isinstance(e, dict):
            return False, f"entries[{i}] 不是对象"
        m = e.get("match")
        if not isinstance(m, dict) or not (m.get("any") or m.get("all")):
            return False, f"entries[{i}] 缺少 match.any 或 match.all"
        if not str(e.get("title") or "").strip():
            return False, f"entries[{i}] 缺少 title"
    return True, f"{len(ents)} 条"


def _pack_tokens(entry: dict) -> tuple[list[str], list[str]]:
    m = entry.get("match") or {}
    norm = lambda xs: [str(x).strip().lower() for x in _as_list(xs) if str(x).strip()]
    return norm(m.get("any")), norm(m.get("all"))


def pack_score(rec: dict, name: str, path: str):
    """一条包条目对「这个节点」的匹配得分。不命中返回 None。

    排序三元组 (匹配在路径里的结束位置, 匹配串长度, 路径末尾位置)：
    位置优先，叶子规则（steamapps\\shadercache）天然排在容器规则
    （\\steamlibrary\\steamapps）前面；同在末尾时更长的匹配串胜过
    泛化的短匹配（\\nvidia\\dxcache > \\dxcache）。
    """
    any_t, all_t = rec.get("any") or [], rec.get("all") or []

    def one(tok: str):
        ends = []
        path_end = 0
        for subj, is_path in ((path, True), (name, False)):
            if not subj:
                continue
            if tok.startswith("re:"):
                try:
                    m = re.search(tok[3:], subj)
                except re.error:
                    return None
                if m:
                    ends.append(m.end())
                    if is_path:
                        path_end = max(path_end, m.end())
            else:
                idx = subj.rfind(tok)
                if idx >= 0:
                    e = idx + len(tok)
                    ends.append(e)
                    if is_path:
                        path_end = max(path_end, e)
        if not ends:
            return None
        # 第三项是「在完整路径里的结束位置」，用来判断命中是不是落在节点本身
        return (max(ends), len(tok), path_end)

    best = None
    if all_t:
        parts = [one(t) for t in all_t]
        if any(x is None for x in parts):
            return None
        best = max(parts)
    if any_t:
        hits = [h for h in (one(t) for t in any_t) if h]
        if not hits:
            return None
        h = max(hits)
        if best is None or h > best:
            best = h
    return best


def load_packs(use_cache: bool = True) -> dict:
    """加载全部资源包。返回 {"packs": [包元信息...], "entries": [匹配记录...]}。

    同 id 的包只保留一份：config/packs-user/ 里的用户包**覆盖**内置版本
    （packs_update_from_remote / import_pack 都把更新写进用户目录，这里的
    去重就是「覆盖」语义的落地——否则界面会列出两行同名包、匹配条目翻倍）。
    用户包损坏时同样参与覆盖：界面如实显示错误行，删掉用户目录里那份即可
    回退内置版本。
    """
    state = load_packs_state()
    key = json.dumps({k: state.get(k) for k in ("disabled",)},
                     sort_keys=True, ensure_ascii=False)
    if use_cache and _PACKS_CACHE["key"] == key and _PACKS_CACHE["data"]:
        return _PACKS_CACHE["data"]
    disabled = {str(x) for x in (state.get("disabled") or [])}
    packs: list[dict] = []
    entries: list[dict] = []

    def _collect(folder, src, out, skip_ids=frozenset()) -> None:
        if not folder.is_dir():
            return
        for f in sorted(folder.glob("*.json")):
            if f.name.lower() == "index.json":       # 清单文件不是包
                continue
            data, err = None, ""
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                err = f"读取失败：{e}"
            if data is not None and not err:
                ok, why = pack_validate(data)
                if not ok:
                    err = why
            pid = str((data or {}).get("id") or f.stem)
            if pid in skip_ids:                      # 用户包覆盖内置版本
                continue
            meta = {
                "id": pid,
                "name": str((data or {}).get("name") or f.stem),
                "version": str((data or {}).get("version") or ""),
                "type": str((data or {}).get("type") or "custom"),
                "type_label": PACK_TYPE_LABELS.get(
                    str((data or {}).get("type") or "custom"), "自定义"),
                "description": str((data or {}).get("description") or ""),
                "updated": str((data or {}).get("updated") or ""),
                "count": len((data or {}).get("entries") or []),
                "enabled": pid not in disabled,
                "source": src, "file": f.name,
                "ok": not err, "error": err,
            }
            out.append(meta)
            if err or pid in disabled:
                continue
            for e in (data.get("entries") or []):
                any_t, all_t = _pack_tokens(e)
                if not (any_t or all_t):
                    continue
                entries.append({"entry": e, "pack": pid,
                                "any": any_t, "all": all_t})

    # 先收集用户包 id（含损坏包，按文件名兜底），供内置侧跳过同 id 包
    user_ids: set[str] = set()
    if USER_PACKS_DIR.is_dir():
        for f in sorted(USER_PACKS_DIR.glob("*.json")):
            if f.name.lower() == "index.json":
                continue
            try:
                ud = json.loads(f.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                ud = None
            user_ids.add(str((ud or {}).get("id") or f.stem))
    _collect(PACKS_DIR, "builtin", packs, skip_ids=user_ids)
    _collect(USER_PACKS_DIR, "user", packs)
    out = {"packs": packs, "entries": entries}
    _PACKS_CACHE["key"], _PACKS_CACHE["data"] = key, out
    return out


def packs_lookup(name: str, path: str, kind: str = "dir") -> dict | None:
    """找一条最匹配的包条目。命中的匹配串越长越具体，优先采用。

    匹配前统一把分隔符换成反斜杠：包里的关键字都是按 Windows 路径写的，
    而调用方可能传 "/" 分隔的路径（例如来自自定义目录输入）。
    """
    low_name = str(name or "").lower().replace("/", "\\")
    low_path = str(path or "").lower().replace("/", "\\")
    if not low_name and not low_path:
        return None
    best, best_score = None, None
    for rec in load_packs()["entries"]:
        e = rec["entry"]
        only = str(e.get("only") or "")
        if only == "dir" and kind == "file":
            continue
        if only == "file" and kind == "dir":
            continue
        sc = pack_score(rec, low_name, low_path)
        if sc is None:
            continue
        if best_score is None or sc > best_score:
            # _node_level：关键字正好覆盖到完整路径的末尾 → 描述的就是这个节点，
            # 而不是它的某个上层目录。判断依据是第三项（路径内的结束位置）。
            best = {**e, "_pack": rec["pack"], "_score": sc[1],
                    "_node_level": bool(sc[2]) and sc[2] >= len(low_path)}
            best_score = sc
    return best


def packs_enrich_target(t: dict, drive: str | None = None) -> None:
    """把资源包里对某个清理目标的说明合并进目标项（就地修改）。

    只补**空缺**字段；cleanable / risk 一律不动 —— 那两项决定「删不删」，
    必须由通过门禁的内置配置说了算，资源包不许改写。
    """
    raw = str(t.get("path") or "")
    path = ""
    try:
        path = expand(raw, drive) if raw else ""
    except Exception:
        path = raw
    label = str(t.get("label") or "")
    base = os.path.basename(path.rstrip("\\/")) if path else ""
    hit = (packs_lookup(label, path, "dir")
           or (packs_lookup(base, path, "dir") if base else None))
    if not hit:
        return
    t["pack"] = hit["_pack"]
    # 伪 rule_id：英文界面下 apply_lang_payload 按它覆盖包内长文本
    if not str(t.get("rule_id") or "").strip():
        t["rule_id"] = f"{hit['_pack']}:{hit.get('id') or label or base}"
    for src, dst in (("what", "what"), ("rebuild", "rebuild"),
                     ("effect", "impact"), ("keep", "keep_if")):
        if hit.get(src) and not str(t.get(dst) or "").strip():
            t[dst] = str(hit[src])
    if hit.get("title") and not str(t.get("short") or "").strip():
        t["short"] = str(hit["title"])


def import_pack(data) -> tuple[bool, str]:
    """导入一个资源包（内容或本地文件路径）。合法就写进 config/packs-user/。"""
    if isinstance(data, str):
        p = os.path.abspath(data)
        if not os.path.isfile(p):
            return False, f"找不到文件：{p}"
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception as e:
            return False, f"不是合法的 JSON：{e}"
    ok, why = pack_validate(data)
    if not ok:
        return False, why
    pid = str(data["id"])
    try:
        USER_PACKS_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(USER_PACKS_DIR / f"{pid}.json", data)
    except OSError as e:
        return False, f"写入失败：{e}"
    _PACKS_CACHE["key"] = None
    return True, f"{data.get('name')}（{why}）"


def fetch_json(url: str, timeout: int = 20):
    """取一个 JSON。只允许 http/https，其余协议直接拒绝。"""
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        return None, "只支持 http/https 地址"
    try:
        req = _UrlRequest(u, headers={"User-Agent": APP_UA})
        with _urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace")), ""
    except Exception as e:
        return None, f"下载失败：{e}"


def packs_update_from_remote() -> dict:
    """从配置的远端清单拉取资源包并导入。

    远端目录里放一个 index.json：
        {"packs": ["games-steam.json", "windows-system.json", ...]}
    每个包再按 <远端>/<文件名> 取回。包内的 id 决定覆盖谁：
    与内置包同 id 时会写进 config/packs-user/，从而覆盖内置版本。
    """
    state = load_packs_state()
    remote = str(state.get("remote") or DEFAULT_PACKS_REMOTE).rstrip("/")
    man, err = fetch_json(remote + "/index.json")
    if err:
        return {"ok": False, "error": err, "remote": remote}
    names = _as_list((man or {}).get("packs"))
    if not names:
        return {"ok": False, "error": "远端清单里没有 packs 列表", "remote": remote}
    done, failed = [], []
    for n in names:
        fname = os.path.basename(str(n))
        if not fname.lower().endswith(".json"):
            continue
        data, e2 = fetch_json(f"{remote}/{fname}")
        if e2:
            failed.append(f"{fname}：{e2}")
            continue
        ok, why = import_pack(data)
        (done if ok else failed).append(f"{fname}：{why}" if ok else f"{fname}：{why}")
    return {"ok": bool(done), "remote": remote, "updated": done,
            "failed": failed, "packs": load_packs(use_cache=False)["packs"]}


# ======================================================================
#  接入自有 AI
# ======================================================================
# 设计约束（都是硬要求）：
#   1. 密钥只落在本机的 config/ai.json 里（.gitignore），不写日志、不回传给前端明文；
#   2. 只接受 OpenAI Chat Completions 兼容的接口，不做任何厂商绑定；
#   3. **AI 没有任何删除能力**。它最多从「内置判定为可清理」的项里挑出建议清单，
#      前端拿到的还是一组 id，勾选与否仍由用户决定，删除仍走同一条清理链路。
# 第 3 条是刻意的：让外部模型影响删除动作，风险不可控。
_AI_DEFAULTS = {
    "enabled": False, "base_url": "", "api_key": "", "model": "",
    "temperature": 0.2, "timeout": 60, "extra_prompt": "",
}


def load_ai_config() -> dict:
    d = _load_json(AI_CONFIG_PATH, {})
    cfg = dict(_AI_DEFAULTS)
    if isinstance(d, dict):
        for k in _AI_DEFAULTS:
            if d.get(k) is not None:
                cfg[k] = d[k]
    return cfg


def save_ai_config(patch: dict) -> dict:
    cfg = load_ai_config()
    for k in _AI_DEFAULTS:
        if k in patch:
            cfg[k] = patch[k]
    cfg["enabled"] = bool(cfg.get("enabled"))
    try:
        cfg["temperature"] = max(0.0, min(2.0, float(cfg.get("temperature") or 0.2)))
    except (TypeError, ValueError):
        cfg["temperature"] = 0.2
    try:
        cfg["timeout"] = max(5, min(300, int(cfg.get("timeout") or 60)))
    except (TypeError, ValueError):
        cfg["timeout"] = 60
    _atomic_write(AI_CONFIG_PATH, cfg)
    return cfg


def ai_public_config() -> dict:
    """给前端看的配置：密钥只回传「有没有」和尾 4 位，绝不回传全文。"""
    cfg = load_ai_config()
    key = str(cfg.get("api_key") or "")
    return {
        "enabled": bool(cfg.get("enabled")),
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "temperature": cfg.get("temperature"),
        "timeout": cfg.get("timeout"),
        "has_key": bool(key),
        "key_tail": key[-4:] if len(key) >= 4 else "",
        "ready": bool(cfg.get("enabled") and cfg.get("base_url") and cfg.get("model")),
    }


def ai_list_models() -> dict:
    """拉取接口的可用模型列表（OpenAI 兼容 GET /models）。

    DeepSeek（https://api.deepseek.com/v1）等主流兼容服务都提供该端点；
    Ollama 的 /v1/models 也兼容。识别不到标准结构时如实报错，
    绝不编造模型名 —— 用户仍可在输入框手填。
    """
    cfg = load_ai_config()
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        return {"ok": False,
                "error": "请先填写接口地址（http:// 或 https:// 开头）再获取模型列表"}
    headers = {"User-Agent": APP_UA}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        req = _UrlRequest(base + "/models", headers=headers)
        with _urlopen(req, timeout=min(30, int(cfg.get("timeout") or 60))) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": f"获取失败：{e}"}
    ids: list[str] = []
    if isinstance(data, dict):
        for m in (data.get("data") or []):
            if isinstance(m, dict) and m.get("id"):
                ids.append(str(m["id"]))
        if not ids and isinstance(data.get("models"), list):
            # 兼容把模型列表放在 models 字段下的服务
            ids = [str(m.get("name") or m.get("model") or m.get("id") or "")
                   for m in data["models"] if isinstance(m, dict)]
    elif isinstance(data, list):
        ids = [str(m) for m in data if isinstance(m, str)]
    ids = sorted(set(i for i in ids if i))
    if not ids:
        return {"ok": False,
                "error": "接口没有返回可识别的模型列表（不是 OpenAI 兼容的 /models？），仍可手动填写模型名"}
    return {"ok": True, "models": ids}


_APP_REPO = "Oerian/O-Cleaner"


def _version_tuple(v: str) -> tuple[int, ...] | None:
    """把 "2.6" / "v2.7.1" 之类的版本串解析成可比较的整数元组；解析不出返回 None。"""
    parts = re.findall(r"\d+", str(v))
    return tuple(int(p) for p in parts) if parts else None


def app_update_check() -> dict:
    """从 GitHub Releases 读取最新版本并与 APP_VERSION 比较。

    只读不改：是否下载、是否升级永远由用户自己点；网络不可达时如实报错，
    绝不静默当作「已是最新」。版本比较按数字逐段比，无法解析时如实说明。
    """
    url = f"https://api.github.com/repos/{_APP_REPO}/releases/latest"
    try:
        req = _UrlRequest(url, headers={"User-Agent": APP_UA})
        with _urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "current": APP_VERSION,
                "error": f"无法连接 GitHub（{e}）；也可以直接到仓库 Releases 页查看"}
    tag = str(data.get("tag_name") or "").lstrip("vV")
    latest = _version_tuple(tag)
    cur = _version_tuple(APP_VERSION)
    assets = [a for a in (data.get("assets") or []) if isinstance(a, dict)]
    download = next((a.get("browser_download_url") for a in assets
                     if str(a.get("name") or "").lower().endswith(".zip")), "")
    return {"ok": True, "current": APP_VERSION, "latest": tag or None,
            "newer": bool(latest and cur and latest > cur),
            "parse_note": None if (latest and cur) else "版本号无法解析，请自行到发布页核对",
            "name": data.get("name") or "",
            "url": data.get("html_url") or "",
            "download": download,
            "published": (data.get("published_at") or "")[:10]}


# ---- 大文件排行 -------------------------------------------------------
# 目录树沿「最粗的分支」逐层展开，天然偏向目录聚合；视频、镜像、安装包
# 这类真正的大头往往就是单个文件。这里用一次专门的全盘遍历把它们直接列出。
BIG_FILES_FLOOR_PS = 10 * 1024 * 1024     # PowerShell 兜底路径的体积地板
BIG_FILES_FLOOR_WIZ = 512 * 1024          # WizTree 明细全量，地板可以更低
BIG_FILES_LIMIT = 30
_BIGFILES_LOCK = threading.Lock()


def _wiztree_big_files(csv_path: str, floor: int) -> list:
    """从 WizTree 导出的 CSV 里抽取文件行（目录行以 \\ 结尾，直接跳过）。

    只保留达到地板的行——C 盘 77 万行里超过 512 KB 的通常只有几千行，
    全量留内存没有压力。切分方式与 _wiztree_parse 相同：路径必被双引号
    包住，按「第一个引号闭合」切，避免路径内逗号把行切坏。
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
            parts = line[end + 1:].lstrip(",").split(",", 1)
            try:
                size = int(parts[0].strip().strip('"'))
            except (ValueError, IndexError):
                continue
            if size >= floor:
                rows.append((path, size))
    return rows


def big_files(drive: str, limit: int = BIG_FILES_LIMIT) -> dict:
    """全盘最大的单个文件排行。只读，不删任何东西。

    两条路径：装了 WizTree 走 MFT 明细（快），否则 PowerShell 全盘遍历
    （与目录树的兜底路径同量级耗时）。同一个盘同时只跑一份，重复请求
    如实报「进行中」而不是静默叠扫。
    """
    letter = (drive or "C").rstrip(":").upper()
    root = letter + ":\\"
    if not IS_WINDOWS or not os.path.exists(root):
        return {"error": f"盘符 {letter}: 不存在", "files": []}
    if not _BIGFILES_LOCK.acquire(blocking=False):
        return {"error": "已有大文件扫描在进行，请稍候", "files": []}
    try:
        rows: list = []
        engine = "powershell"
        if _wiz_ok():
            prog_set("wiz", f"正在用 WizTree 读取 {letter}: 盘的 MFT 明细", 10,
                     indeterminate=True)
            csv_path, _werr = _wiztree_export(letter)
            if csv_path:
                engine = "wiztree"
                try:
                    rows = _wiztree_big_files(csv_path, BIG_FILES_FLOOR_WIZ)
                finally:
                    _wiztree_cleanup(csv_path)
        if engine == "powershell":
            prog_file = os.path.join(
                tempfile.gettempdir(), f"oc-bigfiles-{os.getpid()}-{letter}.txt")
            if os.path.exists(prog_file):
                try:
                    os.remove(prog_file)
                except OSError:
                    pass
            script = (PS_BIG_FILES
                      .replace("__ROOT__", root.replace("'", "''"))
                      .replace("__FLOOR__", str(BIG_FILES_FLOOR_PS))
                      .replace("__PROGFILE__", prog_file.replace("'", "''")))
            prog_set("walk", "正在全盘遍历大文件（未装 WizTree，耗时更长）",
                     10, indeterminate=True)
            stop = threading.Event()
            watcher = threading.Thread(target=_watch_bigfiles,
                                       args=(prog_file, stop), daemon=True)
            if prog_file:
                watcher.start()
            try:
                r = run_ps(script, timeout=1800)
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
            rows = [(str(x.get("p") or ""), int(x.get("s") or 0))
                    for x in items if isinstance(x, dict)]
        rows.sort(key=lambda x: -x[1])
        files = []
        for path, size in rows[:max(1, min(int(limit or BIG_FILES_LIMIT), 100))]:
            name = os.path.basename(path)
            info = identify(name, path, "file")
            info.pop("match_score", None)
            files.append({"path": path, "name": name,
                          "dir": os.path.dirname(path), "size": size,
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
            prog_set("walk", f"已发现 {n} 个超过门槛的大文件…", 40,
                     indeterminate=True)
        except Exception:
            pass


def ai_chat(messages: list[dict], timeout: int | None = None) -> tuple[bool, str]:
    """调用用户自己的 Chat Completions 接口。返回 (成功, 文本或错误)。"""
    cfg = load_ai_config()
    if not cfg.get("enabled"):
        return False, "AI 未启用：在右侧「AI 助手」面板填好接口并勾选「启用」"
    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", base, re.I):
        return False, "接口地址必须以 http:// 或 https:// 开头"
    if not str(cfg.get("model") or "").strip():
        return False, "未填写模型名"
    body = json.dumps({
        "model": cfg["model"], "messages": messages,
        "temperature": float(cfg.get("temperature") or 0.2), "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "User-Agent": APP_UA}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"
    try:
        req = _UrlRequest(base + "/chat/completions", data=body,
                          method="POST", headers=headers)
        with _urlopen(req, timeout=timeout or int(cfg.get("timeout") or 60)) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, f"调用失败：{e}"
    try:
        return True, str(data["choices"][0]["message"]["content"])
    except Exception:
        return False, "接口返回的结构不是 Chat Completions：" + str(data)[:200]


def ai_test() -> dict:
    ok, txt = ai_chat([{"role": "user",
                        "content": "只回复两个字：可用"}], timeout=30)
    return {"ok": ok, "reply": txt if ok else "", "error": "" if ok else txt,
            "config": ai_public_config()}


AI_EXPLAIN_SYS = (
    "你是磁盘清理工具里的说明撰写者。用户会给你一个磁盘上的目录或文件，"
    "以及本工具已有的识别结果。请用简体中文给出简洁、准确、不夸大的说明，"
    "分成四段，每段一行，以「是什么：」「能否重建：」「删除影响：」「建议保留的情况：」开头。"
    "不要用感叹号，不要用「彻底」「永久」「务必」这类词，不要编造你不知道的事实；"
    "不确定就写「不确定」。全文不超过 200 字。"
)


def ai_explain(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    path = str(payload.get("path") or "").strip()
    if not name and not path:
        return {"ok": False, "error": "缺少 name 或 path"}
    kind = str(payload.get("kind") or "dir")
    info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
    local = "\n".join([
        f"名称：{name}", f"完整路径：{path}",
        f"类型：{'目录' if kind == 'dir' else '文件'}",
        f"占用：{payload.get('size_h') or '未知'}",
        f"本工具的内置识别：{info.get('title') or '未识别'}"
        f"（分类 {info.get('category') or '未知'}）",
        f"内置说明：{info.get('what') or '无'}",
    ])
    msgs = [{"role": "system", "content": AI_EXPLAIN_SYS},
            {"role": "user", "content": local}]
    ok, txt = ai_chat(msgs)
    return {"ok": ok, "text": txt if ok else "", "error": "" if ok else txt}


AI_PLAN_SYS = (
    "你是磁盘清理工具的方案助手。用户会给你一个清理目标和一份候选清单，"
    "清单里的每一项都已经由本工具的规则判定为「可清理」。"
    "你只能从清单里挑选，不得添加清单以外的任何路径或文件。"
    "请只输出一个 JSON 对象，不要输出任何其它文字，格式为："
    '{"summary":"一到两句话的说明","picks":[{"id":"清单里的 id","reason":"为什么选它"}]}'
    "。如果清单为空，picks 就返回空数组，summary 说明原因。"
)


def ai_plan(payload: dict) -> dict:
    request = str(payload.get("request") or "").strip()
    if not request:
        return {"ok": False, "error": "请先描述你的清理目标"}
    raw = payload.get("candidates")
    cands = [c for c in _as_list(raw) if isinstance(c, dict) and c.get("id")]
    # 只把「内置判定可清理」的项交给模型，从源头上杜绝模型建议删不该删的东西
    cands = [c for c in cands if c.get("cleanable") is True][:120]
    lines = [f"- id={c['id']} | {c.get('label') or ''} | {c.get('size_h') or ''}"
             f" | {c.get('category') or ''} | 路径 {c.get('path') or ''}"
             for c in cands]
    msg = (f"清理目标：{request}\n\n候选清单（{len(cands)} 项）：\n"
           + ("\n".join(lines) if lines else "（空）"))
    ok, txt = ai_chat([{"role": "system", "content": AI_PLAN_SYS},
                       {"role": "user", "content": msg}])
    if not ok:
        return {"ok": False, "error": txt}
    parsed = _extract_json(txt)
    if not isinstance(parsed, dict):
        return {"ok": False, "error": "模型没有返回可解析的 JSON",
                "raw": txt[:500]}
    known = {c["id"]: c for c in cands}
    picks, dropped = [], []
    for p in _as_list(parsed.get("picks")):
        pid = str((p or {}).get("id") or "")
        if pid in known:
            picks.append({"id": pid, "reason": str((p or {}).get("reason") or ""),
                          "label": known[pid].get("label") or pid,
                          "size_h": known[pid].get("size_h") or ""})
        elif pid:
            dropped.append(pid)      # 模型编了清单外的 id：丢掉并如实回报
    return {"ok": True, "summary": str(parsed.get("summary") or ""),
            "picks": picks, "dropped": dropped, "candidate_count": len(cands),
            "raw": txt[:2000] if not picks else ""}


def _extract_json(text: str):
    """从模型回复里抠出第一个 JSON 对象。模型常把 JSON 包在 ```json 里。"""
    s = str(text or "").strip()
    if "```" in s:
        parts = s.split("```")
        for seg in parts:
            seg = seg.strip()
            if seg.lower().startswith("json"):
                seg = seg[4:].strip()
            if seg.startswith("{") or seg.startswith("["):
                s = seg
                break
    for op, cl in (("{", "}"), ("[", "]")):
        i, j = s.find(op), s.rfind(cl)
        if i >= 0 and j > i:
            try:
                return json.loads(s[i:j + 1])
            except Exception:
                continue
    try:
        return json.loads(s)
    except Exception:
        return None


def build_state(drive: str | None = None, custom_path: str | None = None,
                scope: str = "all") -> dict:
    cfg = load_targets()
    dcfg = load_drives_cfg()
    letter = (drive or "C").rstrip(":").upper()
    targets = build_targets(letter, custom_path)
    if scope == "drive":
        targets = [t for t in targets if is_drive_specific(t)]
    return {
        "app": dcfg.get("app", {}),
        "presets": cfg.get("presets", []),
        "scope": scope,
        "targets": [{
            "id": t["id"], "base_id": t.get("base_id", t["id"]),
            # 自定义释义的键：前端编辑器保存时回传这个键，后端按它应用覆盖
            "ovkey": _override_key(t),
            # rule_id：英文界面按它在 kb-en.json 里找识别名与长文本的英文版
            "rule_id": "target:" + str(t.get("base_id", t["id"])),
            "label": expand(t.get("label", t["id"]), letter),
            "path": expand(t["path"], letter), "category": t.get("category", "其它"),
            "risk": t.get("risk", "无"), "needs_admin": bool(t.get("needs_admin")),
            "kind": t.get("kind", "tree"), "glob": t.get("glob", ""),
            "drive_specific": is_drive_specific(t),
            "scope": "drive" if is_drive_specific(t) else "user",
            "temp_target": bool(t.get("temp_target")),
            "discovered": bool(t.get("discovered")),
            "cleanable": bool(t.get("cleanable")),
            "custom": bool(t.get("custom")),
            "short": t.get("short", ""), "what": t.get("what", ""),
            "rebuild": t.get("rebuild", ""), "impact": t.get("impact", ""),
            "keep_if": t.get("keep_if", ""),
        } for t in targets],
        "disk": get_disk_info(letter),
        "drives": list_drives(),
        "current_drive": letter,
        "custom_path": custom_path or "",
        "is_admin": is_admin(),
        "schedule": schedule_state(),
        "history": load_history()[:10],
        "stats": history_stats(),
        "units": resolve_units(load_convert().get("units", [])),
        "custom_rules": load_custom_rules(),
        # 用户自定义聚合组：前端把占用树里命中的路径归拢进这些组
        "fs_groups": load_fs_groups(),
        "config_dir": str(CONFIG_DIR),
        # 数据存档目录：运行数据（含 config/）所在位置，界面自检面板展示，
        # 迁移/备份时整目录拷走即可
        "data_dir": str(DATA_DIR),
        "data_dir_redirected": DATA_DIR != BASE_DIR,
        # 实测可写性：自检面板据此给出「历史/配置能否保存」的结论
        "data_dir_writable": data_dir_writable(),
        # 首屏引导要用：加速是否可用、能不能自动装、许可证怎么算
        "wiztree": wiztree_status(),
        # 资源包元信息（不含逐条 entries，条目只在服务端参与识别）
        "packs": load_packs()["packs"],
        "packs_remote": load_packs_state().get("remote") or "",
        # AI 设置：密钥只回传「有没有」与尾号，绝不回传全文
        "ai": ai_public_config(),
    }


def build_scan(drive: str | None = None, custom_path: str | None = None,
               scope: str = "all") -> dict:
    letter = (drive or "C").rstrip(":").upper()
    targets = build_targets(letter, custom_path)
    if scope == "drive":
        targets = [t for t in targets if is_drive_specific(t)]
    results = []
    for t in targets:
        info = measure_target(t, letter)
        results.append({
            "id": t["id"], "base_id": t.get("base_id", t["id"]),
            "size": info.get("size", 0), "size_h": human(info.get("size", 0)),
            "files": info.get("files", 0),
            "items": [{**i, "size_h": human(i["size"])} for i in (info.get("items") or [])],
            "missing": bool(info.get("missing")), "error": info.get("error"),
        })
    results.sort(key=lambda r: (r["missing"], -r["size"]))
    return {"results": results, "disk": get_disk_info(letter),
            "is_admin": is_admin(), "drive": letter,
            "scan_time": datetime.now().strftime("%H:%M:%S")}


class Handler(BaseHTTPRequestHandler):
    server_version = f"OCleaner/{APP_VERSION}"

    def log_message(self, fmt, *args):
        pass

    # ---------- CSRF 防护 ----------
    # 威胁模型：用户浏览器里开着本工具，同时又打开了一个恶意网页。
    # 那个网页可以向 http://127.0.0.1:端口 发请求（浏览器不阻止访问回环地址），
    # 于是能借用户之手清空清理历史、弹资源管理器、甚至改配置。
    # 对抗性审查实测：跨站 DELETE /history 与 POST /reveal 都返回了 200。
    # 服务端既不校验 Origin 也不发 CORS 头，浏览器因此读不到响应，
    # 但**副作用照样发生** —— 必须显式拦。
    #
    # 规则：写操作若带了 Origin/Referer 且来源不是本机，一律拒绝。
    # 用 curl 等工具不带 Origin 时仍放行（那是本机用户自己的操作）。
    def _origin_ok(self) -> bool:
        for h in ("Origin", "Referer"):
            v = self.headers.get(h)
            if not v:
                continue
            try:
                host = (urlparse(v).hostname or "").lower()
            except Exception:
                return False
            if host not in ("127.0.0.1", "localhost", "::1", ""):
                return False
        return True

    def _deny_csrf(self) -> bool:
        """跨站写操作直接 403。返回 True 表示已拒绝。"""
        if self._origin_ok():
            return False
        self._json({"error": "拒绝跨站请求：本工具只接受从本机界面发起的写操作"}, 403)
        return True

    # ---------- 异常兜底 ----------
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        except Exception:
            self._fail()

    def _fail(self) -> None:
        print("\n[服务端错误] " + "=" * 50, file=sys.stderr, flush=True)
        traceback.print_exc()
        print("=" * 68, file=sys.stderr, flush=True)
        try:
            self._json({"error": "服务端内部错误，详见控制台窗口"}, 500)
        except Exception:
            pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        """读请求体。畸形输入一律当成空对象，交给各路由自行返回 400，
        避免抛异常变成 500。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        # 限制请求体大小：本工具的请求都很小，超大的必然是异常或恶意
        if n <= 0 or n > 4 * 1024 * 1024:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _query(self) -> dict:
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # ---------- 路由 ----------
    def do_GET(self):  # noqa: N802
        route = self.path.split("?")[0]
        q = self._query()
        if route in ("/", "/index.html"):
            try:
                html = (WEB_DIR / "index.html").read_bytes()
            except OSError:
                html = "<h1>web/index.html 缺失</h1>".encode("utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if route == "/__oc__/state":
            return self._json(apply_lang_payload(
                build_state(q.get("drive"), q.get("custom"),
                            q.get("scope", "all")), q.get("lang")))
        if route == "/__oc__/progress":
            # 前端在全盘扫描期间每 0.5 秒轮询一次，用来画真实进度条
            return self._json(prog_snapshot())
        if route == "/__oc__/packs":
            return self._json({"packs": load_packs(use_cache=False)["packs"],
                               "remote": load_packs_state().get("remote") or "",
                               "user_dir": str(USER_PACKS_DIR)})
        if route == "/__oc__/scan":
            return self._json(apply_lang_payload(
                build_scan(q.get("drive"), q.get("custom"),
                           q.get("scope", "all")), q.get("lang")))
        if route == "/__oc__/history":
            return self._json(load_history())
        if route == "/__oc__/fsgroups":
            return self._json({"groups": load_fs_groups()})
        if route == "/__oc__/disk-trend":
            data = _load_json(DISK_TREND_PATH, [], use_cache=False)
            return self._json(data if isinstance(data, list) else [])
        if route == "/__oc__/stats":
            return self._json(history_stats())
        if route == "/__oc__/overrides":
            return self._json(load_overrides())
        if route == "/__oc__/bundle/export":
            return self._json(_export_bundle())
        if route == "/__oc__/steam":
            return self._json(apply_lang_payload(discover_steam(), q.get("lang")))
        if route == "/__oc__/games":
            # 全平台游戏库（Steam + Epic + GOG），steam 负载原样透传在 .steam 里
            return self._json(apply_lang_payload(discover_games(), q.get("lang")))
        if route == "/__oc__/partition":
            p = q.get("path") or ""
            if not p:
                return self._json({"error": "缺少 path 参数"}, 400)
            return self._json(apply_lang_payload(
                analyze_dir_partition(p), q.get("lang")))
        if route == "/__oc__/subdirs":
            # 大清理项逐层拆子项：查 WizTree 常驻表，任意深度都是 O(1)
            p = q.get("path") or ""
            if not p:
                return self._json({"error": "缺少 path 参数"}, 400)
            try:
                limit = min(400, max(1, int(q.get("limit") or 80)))
            except ValueError:
                return self._json({"error": "limit 必须是整数"}, 400)
            return self._json(apply_lang_payload(
                list_subdirs(q.get("drive") or "", p, limit), q.get("lang")))
        if route == "/__oc__/wiztree":
            # 新用户引导要的信息全在这里：找到没有、是不是本目录自带、怎么装
            return self._json(wiztree_status())
        if route == "/__oc__/drives":
            return self._json({"drives": list_drives(), "is_admin": is_admin()})
        if route == "/__oc__/fullscan":
            # 旧接口名保留为别名，指向新的占用树
            letter = (q.get("drive") or "C").rstrip(":").upper()
            return self._json(apply_lang_payload(
                scan_disk_overview(letter), q.get("lang")))
        if route == "/__oc__/overview":
            letter = (q.get("drive") or "C").rstrip(":").upper()
            try:
                depth = int(q.get("depth") or DISK_TREE_MAX_DEPTH)
            except ValueError:
                return self._json({"error": "depth 必须是整数"}, 400)
            try:
                r = scan_disk_overview(letter, max(1, min(depth, 6)))
            except BaseException as e:
                # 线程级兜底：扫描线程里的异常（含 Exception 之外的类型）
                # 若直接放行，客户端只会拿到一条空响应，无从排查。
                print(f"[overview] 扫描线程异常退出：{type(e).__name__}: {e}",
                      file=sys.stderr)
                return self._json({"error": f"扫描线程异常退出"
                                            f"（{type(e).__name__}）"}, 500)
            return self._json(apply_lang_payload(r, q.get("lang")))
        if route == "/__oc__/dirlevel":
            # 整棵树已在 overview 里一次扫完，这里不再单独扫一层。
            # 保留该路由但明确返回提示，避免旧前端静默失败。
            return self._json({"error": "整棵树已在 /__oc__/overview 中返回，"
                                        "无需逐层请求", "children": []}, 410)
        if route == "/__oc__/identify":
            name = q.get("name") or ""
            path = q.get("path") or name
            kind = q.get("kind") or ("dir" if os.path.isdir(path) else "file")
            return self._json(identify(name, path, kind))
        return self._json({"error": "not found"}, 404)

    def do_DELETE(self):  # noqa: N802
        if self._deny_csrf():
            return
        route = self.path.split("?")[0]
        if route == "/__oc__/history":
            _atomic_write(HISTORY_PATH, [])
            return self._json({"ok": True})
        if route == "/__oc__/custom-rules":
            save_custom_rules([])
            return self._json({"ok": True})
        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self._deny_csrf():
            return
        route = self.path.split("?")[0]
        body = self._body()

        # ---- 自动获取 WizTree 便携版（可选加速，不装到系统里）----
        if route == "/__oc__/wiztree/install":
            if getattr(self.server, "wiz_busy", False):
                return self._json({"error": "正在下载中，请稍候"}, 409)
            self.server.wiz_busy = True
            try:
                r = acquire_wiztree()
            finally:
                self.server.wiz_busy = False
            return self._json({**r, "status": wiztree_status()},
                              200 if r.get("ok") else 500)

        # ---- 用户自定义聚合组（占用树的自由归组，纯显示层）----
        if route == "/__oc__/fsgroups":
            op = str(body.get("op") or "")
            groups = load_fs_groups()
            if op == "add":
                gid, name, paths = (body.get("id") or ""), (body.get("name") or ""), (body.get("paths") or [])
                if not isinstance(paths, list) or not paths:
                    return self._json({"error": "paths 不能为空"}, 400)
                if not str(name).strip() and not gid:
                    return self._json({"error": "组名不能为空"}, 400)
                norm = [str(p).replace("/", "\\").rstrip("\\").strip().lower()
                        for p in paths if str(p).strip()]
                if gid:
                    for g in groups:
                        if g["id"] == gid:
                            for p in norm:
                                if p not in g["paths"]:
                                    g["paths"].append(p)
                            break
                    else:
                        return self._json({"error": "找不到该聚合组"}, 404)
                else:
                    if not norm:
                        return self._json({"error": "paths 不能为空"}, 400)
                    groups.append({
                        "id": "grp-" + str(int(time.time() * 1000))[-9:],
                        "name": str(name).strip(),
                        "paths": norm,
                        "created": time.strftime("%Y-%m-%d %H:%M"),
                    })
            elif op == "remove":
                gid, paths = body.get("id") or "", (body.get("paths") or [])
                rest = [g for g in groups if g["id"] != gid] if not paths else groups
                if paths:
                    drop = {str(p).replace("/", "\\").rstrip("\\").strip().lower() for p in paths}
                    for g in rest:
                        if g["id"] == gid:
                            g["paths"] = [p for p in g["paths"] if p not in drop]
                    rest = [g for g in rest if g["paths"]]
                groups = rest
            elif op == "rename":
                gid, name = body.get("id") or "", str(body.get("name") or "").strip()
                if not name:
                    return self._json({"error": "组名不能为空"}, 400)
                for g in groups:
                    if g["id"] == gid:
                        g["name"] = name
            elif op == "delete":
                groups = [g for g in groups if g["id"] != (body.get("id") or "")]
            else:
                return self._json({"error": f"未知操作：{op}"}, 400)
            save_fs_groups(groups)
            return self._json({"ok": True, "groups": load_fs_groups()})

        # ---- 执行清理 ----
        if route == "/__oc__/clean":
            drive = (body.get("drive") or "C").rstrip(":").upper()
            custom_path = body.get("custom_path") or None
            ids = set(body.get("ids") or [])
            paths_in = body.get("paths") or []
            only = body.get("only_paths") or {}
            trash = bool(body.get("trash"))
            targets = build_targets(drive, custom_path)
            # 用户明确勾选了「建议保留 / 未识别」行时由前端置位（见下方 paths 段）；
            # 两个通道共用这一个开关。
            allow_nc = bool(body.get("allow_non_cleanable"))
            # 有效 cleanable=False 的目标（用户改成「建议保留」）默认拒绝清理，
            # 与旧版卡片勾选框禁用的行为对齐 —— 这条护栏原来只在前端。
            # 2026-10-13 起：allow_non_cleanable=True 时放行（用户在确认弹窗里
            # 明确勾选的建议保留/未识别项），系统目录保护照常生效。
            selected = [t for t in targets
                        if t["id"] in ids
                        and (t.get("cleanable") is not False or allow_nc)]

            # ---- 按路径清理（占用排行行内勾选的行）----
            # 安全边界与 /__oc__/clean-game 同一模式：**绝不信任前端说它可清理**，
            # 每条路径都由服务端重新 identify()，再过 check_deletable 的系统目录
            # 保护，缺一不可。
            # 2026-10-13 用户需求：建议保留 / 未识别的行也要可勾选（用户自己
            # 看过内容、明确要删的目录不该被工具拦死）。服务端因此接受
            # allow_non_cleanable=True 的请求放行非可清理路径 —— 但只放行
            # 「用户显式勾选」的这些路径，且系统目录保护与占用预检**原样生效**；
            # 前端在确认弹窗里必须如实提示「包含建议保留/未识别项」。
            skipped_paths: list[dict] = []
            for p in paths_in if isinstance(paths_in, list) else []:
                if not isinstance(p, str) or not os.path.isabs(p):
                    skipped_paths.append({"path": str(p)[:200],
                                          "reason": "拒绝：非绝对路径"})
                    continue
                p2 = os.path.normpath(p)
                if not os.path.isdir(p2):
                    skipped_paths.append({"path": p2, "reason": "目录不存在"})
                    continue
                info = identify(os.path.basename(p2.rstrip("\\")) or p2, p2, "dir")
                if not info.get("cleanable") and not allow_nc:
                    skipped_paths.append(
                        {"path": p2, "reason": "服务端识别为不可清理，拒绝"})
                    continue
                selected.append({
                    "id": f"tree:{p2.lower()}",
                    "label": os.path.basename(p2.rstrip("\\")) or p2,
                    "path": p2, "kind": "tree",
                    "category": info.get("category", "其它"),
                    "risk": info.get("risk", "中"),
                    "cleanable": bool(info.get("cleanable")),
                })

            if not selected:
                return self._json({"error": "没有选择任何清理项",
                                   "skipped_paths": skipped_paths}, 400)
            # 包含关系去重：外层勾选项完整包含内层勾选项时只清最小项，
            # 不重复删除、不重复计体积（界面预估体积同样按此口径）。
            selected, merged = dedup_nested_targets(selected)
            if not selected:
                return self._json({"error": "没有选择任何清理项",
                                   "skipped_paths": skipped_paths,
                                   "merged": merged}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "drive": drive,
                         "trigger": f"手动清理（{drive}:）",
                         "results": [], "freed": 0, "failed": 0,
                         "targets": len(selected)}
                for t in selected:
                    try:
                        r = clean_target(t, only.get(t["id"]), drive, trash=trash)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        r = _error_result(t, drive, exc)
                    entry["results"].append(r)
                    entry["freed"] += r["freed"]
                    entry["failed"] += r["failed"]
                entry["freed_h"] = human(entry["freed"])
                append_history(entry)
                return self._json(apply_lang_payload(
                    {"entry": entry, "disk": get_disk_info(drive),
                     "stats": history_stats(),
                     "skipped_paths": skipped_paths,
                     "merged": merged}, self._query().get("lang")))
            finally:
                self.server.busy = False

        # ---- 被占用文件：一键查「可能是谁占着」----
        if route == "/__oc__/locks":
            paths = body.get("paths")
            if not isinstance(paths, list):
                return self._json({"error": "paths 必须是路径数组"}, 400)
            return self._json(find_lock_holders(paths))

        # ---- 大文件排行（只读全盘遍历，列出最大的单个文件）----
        if route == "/__oc__/bigfiles":
            # limit 必须是整数：传非数字时 400 而不是 ValueError 崩成 500
            # （与 GET /subdirs 对 limit 的校验同一口径）
            try:
                limit_n = int(body.get("limit") or 0)
            except (TypeError, ValueError):
                return self._json({"error": "limit 必须是整数"}, 400)
            return self._json(big_files(str(body.get("drive") or ""),
                                        limit=limit_n))

        # ---- 一键清空「所选项」的回收站 ----
        # 按原始路径精确匹配回收站条目，只删配对的 $I/$R 文件，不动回收站
        # 里的其它内容（用户自己删的文件原样保留）。请求里的路径只用于
        # 匹配，真实删除永远发生在 $Recycle.Bin 内部。
        if route == "/__oc__/recycle-purge":
            paths = body.get("paths")
            if not isinstance(paths, list) or not paths:
                return self._json({"error": "paths 必须是非空路径数组"}, 400)
            if len(paths) > 200:
                return self._json({"error": "一次最多 200 个路径"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                out = purge_recycle_paths(paths)
            finally:
                self.server.busy = False
            # 释放量如实入账：与常规清理同一统计通道。
            # 回收站条目可能跨盘：从各结果路径提取盘符集合，逗号连接。
            drives = ",".join(sorted({
                (r.get("path") or "")[:1].upper() for r in out["results"]
                if r.get("path")}))
            entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                     "drive": drives,
                     "trigger": "清空所选项的回收站",
                     "results": out["results"], "freed": out["freed"],
                     "failed": 0, "targets": len(paths),
                     "freed_h": out["freed_h"]}
            append_history(entry)
            out["stats"] = history_stats()
            return self._json(out)

        # ---- 删除游戏前的存档查询（只读）----
        # 在「游戏最爱放存档」的几个位置找名字与这款游戏相关的目录。
        # 启发式匹配，结果按「疑似存档」对待；前端负责把「建议先备份」讲清楚。
        if route == "/__oc__/game-saves":
            p = str(body.get("path") or "")
            if not p or not os.path.isabs(p):
                return self._json({"error": "拒绝：路径必须是绝对路径"}, 400)
            saves = find_game_save_candidates(p, str(body.get("appid") or ""))
            return self._json({"saves": saves, "count": len(saves)})

        # ---- 删除单个游戏本体（Steam / Epic / GOG）----
        # 安全边界（重要）：这不是通用删除接口。路径必须**后端自己**也识别为
        # 游戏安装目录（steamapps\common 的直接子目录，或 Epic/GOG 清单里的
        # 精确安装路径），再过一遍 check_deletable 的系统目录保护。
        # 缺任何一层，这个端点就会退化成「页面可以删任意目录」的口子。
        if route == "/__oc__/clean-game":
            p = str(body.get("path") or "")
            ok, why = check_deletable(p, "tree")
            if not ok:
                return self._json({"error": why}, 400)
            norm = os.path.normpath(os.path.abspath(p))
            low = norm.lower()
            steam_common, _libs = steam_common_dirs()
            if os.path.dirname(low) in steam_common:
                plat = "steam"
            else:
                dirs, _glibs = game_install_dirs()
                plat = dirs.get(low, "")
            if not plat:
                # Epic/GOG 残留目录：与 Steam 孤儿同一信任级别 —— 必须正好是
                # 「平台安装根」的直接子目录，且此刻的平台记录里没有它。
                par = os.path.dirname(low)
                rts = platform_install_roots()
                if par in rts.get("epic", set()):
                    plat = "epic"
                elif par in rts.get("gog", set()):
                    plat = "gog"
            if not plat:
                return self._json({
                    "error": "拒绝：这个目录不是已识别的游戏本体"
                             "（只允许删除 Steam / Epic / GOG 的游戏安装目录）"}, 400)
            if not os.path.isdir(norm):
                return self._json({"error": "目录不存在（可能已删除）"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                t0 = time.time()
                name = os.path.basename(norm)
                disp, _zh = game_display_name(None, name)
                before = _size_of_dir(norm)
                okrm = rm(norm)
                # 释放量按「前-后」实测而不是「成功就记全量」：rm 用的是
                # ignore_errors 的 rmtree，部分文件被占用时目录会留下但已删掉
                # 一部分 —— 按「成功与否」二值记会把真实释放量报成 0，
                # 累积统计因此偏低失真。
                after = _size_of_dir(norm) if os.path.isdir(norm) else 0
                freed = max(0, before - after)
                if okrm:
                    err = None
                elif freed > 0:
                    err = "部分文件未能删除（可能被占用或需要管理员权限），目录还在"
                else:
                    err = "删除失败（文件可能被占用或需要管理员权限）"
                letter = norm[:1].upper()
                result = {
                    "id": f"game-{abs(hash(low)) % 10**8}", "target_id": "game",
                    "label": f"游戏：{disp or name}", "category": "游戏",
                    "path": norm, "drive": letter,
                    "before": before, "after": after, "freed": freed,
                    "deleted": 1 if okrm else 0,
                    "failed": 0 if okrm else 1, "skipped": [],
                    "error": err,
                    "seconds": round(time.time() - t0, 2),
                }
                append_history({
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "drive": letter,
                    "trigger": f"删除游戏（{letter}:）",
                    "results": [result], "freed": freed,
                    "failed": result["failed"], "targets": 1,
                    "freed_h": human(freed),
                })
                return self._json({
                    "ok": okrm, "freed": freed, "freed_h": human(freed),
                    "name": disp or name, "path": norm,
                    "disk": get_disk_info(letter),
                    "error": err,
                })
            finally:
                self.server.busy = False

        # ---- 定时设置 ----
        if route == "/__oc__/schedule":
            s = schedule_state()
            for k in ("enabled", "interval_hours", "preset", "drive"):
                if k in body:
                    s[k] = body[k]
            if s.get("enabled"):
                # interval_hours 必须是数字：历史数据损坏或前端 bug 传入字符串时
                # 直接 400，而不是 float() 崩成 500，更不能把垃圾值原样落盘
                try:
                    hrs = max(0.25, float(s.get("interval_hours") or 24))
                except (TypeError, ValueError):
                    return self._json(
                        {"error": "interval_hours 必须是数字"}, 400)
                s["interval_hours"] = hrs
                s["next_run"] = time.time() + hrs * 3600
            else:
                s["next_run"] = 0
            # 持锁写入：与 _run_scheduled 的锁内写回串行化，否则定时任务刚算好的
            # next_run 会被这里的陈旧快照覆盖（用户一改设置就立刻又触发一次）
            with _lock:
                cur = schedule_state()
                cur.update({k: s[k] for k in
                            ("enabled", "interval_hours", "preset", "drive",
                             "next_run") if k in s})
                if not cur.get("enabled"):
                    cur["next_run"] = 0
                _atomic_write(SCHEDULE_PATH, cur)
                s = cur
            return self._json(s)

        # ---- 自定义换算单位 ----
        if route == "/__oc__/units":
            cfg = load_convert()
            units = list(cfg.get("units", []))
            uid = (body.get("id") or "").strip()
            if not uid:
                return self._json({"error": "单位 id 不能为空"}, 400)
            if not any(u.get("id") == uid for u in units):
                units.append({
                    "id": uid,
                    "label": body.get("label") or uid,
                    "plural": body.get("plural") or body.get("label") or uid,
                    "bytes_per_unit": body.get("bytes_per_unit") or 0,
                    "note": body.get("note") or "",
                    "category": body.get("category") or "自定义",
                })
            cfg["units"] = units
            _atomic_write(CONVERT_PATH, cfg)
            _cache.pop(str(CONVERT_PATH), None)
            return self._json({"units": resolve_units(units), "stats": history_stats()})

        if route == "/__oc__/units/delete":
            cfg = load_convert()
            uid = body.get("id")
            cfg["units"] = [u for u in cfg.get("units", []) if u.get("id") != uid]
            _atomic_write(CONVERT_PATH, cfg)
            _cache.pop(str(CONVERT_PATH), None)
            return self._json({"units": resolve_units(cfg["units"]),
                               "stats": history_stats()})

        # ---- 用户自定义释义 / 结论（三条优先级的最上一条）----
        if route == "/__oc__/overrides/set":
            oid = str(body.get("id") or "").strip()
            # 键有两种形态：目标 base_id（不含斜杠），或归一化小写路径
            # （占用排行行右键自定义用的就是路径键，2026-10-13 起）。
            # 路径键只是查找键，不参与任何删除路径解析，放宽反斜杠限制；
            # 仍要求形如绝对盘符路径并限长，防止乱键写满配置文件。
            _is_path_key = bool(re.match(r"^[a-zA-Z]:\\", oid)) and len(oid) <= 500
            if (not oid
                    or len(oid) > (500 if _is_path_key else 200)
                    or ("/" in oid or ("\\" in oid and not _is_path_key))):
                return self._json({"error": "无效的清理项标识"}, 400)
            entry = {}
            for f in ("what", "rebuild", "impact", "keep_if"):
                v = body.get(f)
                if isinstance(v, str) and v.strip():
                    if len(v) > 2000:
                        return self._json({"error": "说明文本过长（上限 2000 字）"}, 400)
                    entry[f] = v.strip()
            for f in ("label", "note"):
                v = body.get(f)
                if isinstance(v, str) and v.strip():
                    if len(v) > 200:
                        return self._json({"error": "标签/说明过长（上限 200 字）"}, 400)
                    entry[f] = v.strip()
            if isinstance(body.get("cleanable"), bool):
                entry["cleanable"] = body["cleanable"]
            if isinstance(body.get("pin"), bool):
                entry["pin"] = body["pin"]
            if body.get("label_kind") in ("clean", "unk", "keep"):
                entry["label_kind"] = body["label_kind"]
            if isinstance(body.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", body["color"]):
                entry["color"] = body["color"].lower()
            data = load_overrides()
            if entry:
                data["targets"][oid] = entry
            else:
                data["targets"].pop(oid, None)   # 全部留空 = 恢复默认
            save_overrides(data)
            return self._json({"targets": data["targets"]})

        if route == "/__oc__/overrides/delete":
            oid = str(body.get("id") or "").strip()
            data = load_overrides()
            data["targets"].pop(oid, None)
            save_overrides(data)
            return self._json({"targets": data["targets"]})

        # ---- 自定义数据包：换算单位 / 释义覆盖 / 固定项规则 一次导入导出 ----
        # 导入逐项校验并报告每一项的跳过原因，所以它同时是 AI 生成结果的
        # 验收器：生成 -> 导入 -> 按报告修正 -> 再导入，循环到全部收下。
        if route == "/__oc__/bundle/import":
            if str(body.get("kind") or "") != "ocleaner-custom":
                return self._json({"error": "这不是 O-Cleaner 自定义数据包"
                                            "（kind 应为 ocleaner-custom）"}, 400)
            rep = {"units_added": 0, "units_updated": 0, "units_skipped": [],
                   "overrides_applied": 0, "overrides_skipped": [],
                   "rules_added": 0, "rules_updated": 0, "rules_skipped": []}

            # 1) 换算单位：按 id 更新、按 label 去重，其余新增
            cfg = load_convert()
            units = list(cfg.get("units", []))
            by_id = {str(u.get("id") or ""): u for u in units}
            by_label = {str(u.get("label") or ""): u for u in units}
            for i, u in enumerate(body.get("units") or []):
                if not isinstance(u, dict):
                    continue
                label = str(u.get("label") or "").strip()
                nbytes = _bundle_unit_bytes(u)
                if not label:
                    rep["units_skipped"].append(f"units[{i}] 缺名称")
                    continue
                if nbytes is None:
                    rep["units_skipped"].append(
                        f"「{label}」缺有效大小（bytes_per_unit 或 size.value/unit）")
                    continue
                uid = str(u.get("id") or "").strip()
                entry = {"label": label,
                         "plural": str(u.get("plural") or label).strip() or label,
                         "bytes_per_unit": nbytes,
                         "note": str(u.get("note") or "").strip(),
                         "category": str(u.get("category") or "自定义").strip()}
                if uid and uid in by_id:
                    by_id[uid].update(entry)
                    rep["units_updated"] += 1
                elif label in by_label:
                    rep["units_skipped"].append(f"「{label}」已有同名单位")
                else:
                    uid = uid or f"u{int(time.time() * 1000) % 10 ** 10}{i}"
                    entry["id"] = uid
                    units.append(entry)
                    by_id[uid] = entry
                    by_label[label] = entry
                    rep["units_added"] += 1
            cfg["units"] = units
            _atomic_write(CONVERT_PATH, cfg)
            _cache.pop(str(CONVERT_PATH), None)

            # 2) 释义/结论覆盖：与 overrides/set 同一套校验
            data = load_overrides()
            for k, v in (body.get("overrides") or {}).items():
                k = str(k).strip()
                # 路径键（占用排行右键自定义 / 自定义提级用的归一化路径）
                # 与目标键走同一套校验：形如盘符绝对路径且限长 500。
                # 此前一刀切拒绝含反斜杠的键，导致导出的行级覆盖导不回来
                # （2026-10-13 修复，随「自定义提级」一起走迁移数据）。
                _is_path_key = bool(re.match(r"^[a-zA-Z]:\\", k)) and len(k) <= 500
                if (not k or len(k) > (500 if _is_path_key else 200)
                        or "/" in k or ("\\" in k and not _is_path_key)):
                    rep["overrides_skipped"].append(f"「{k[:60]}」键不合法")
                    continue
                entry = None
                if isinstance(v, dict):
                    entry = {}
                    for f in ("what", "rebuild", "impact", "keep_if"):
                        t = v.get(f)
                        if isinstance(t, str) and t.strip():
                            if len(t) > 2000:
                                entry = None
                                break
                            entry[f] = t.strip()
                    for f in ("label", "note"):
                        t = v.get(f)
                        if isinstance(t, str) and t.strip() and len(t) <= 200:
                            entry[f] = t.strip()
                    if isinstance(v.get("cleanable"), bool):
                        entry["cleanable"] = v["cleanable"]
                    if isinstance(v.get("pin"), bool):
                        entry["pin"] = v["pin"]
                    if v.get("label_kind") in ("clean", "unk", "keep"):
                        entry["label_kind"] = v["label_kind"]
                    if isinstance(v.get("color"), str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v["color"]):
                        entry["color"] = v["color"].lower()
                    if not entry:
                        entry = None
                if entry is None:
                    rep["overrides_skipped"].append(f"「{k}」没有可用的字段")
                    continue
                data["targets"][k] = entry
                rep["overrides_applied"] += 1
            save_overrides(data)

            # 3) 固定项规则：与 custom-rules/add 同一套校验（id 必须真实存在）
            rules = load_custom_rules()
            valid_cache: dict = {}
            for i, r0 in enumerate(body.get("rules") or []):
                if not isinstance(r0, dict):
                    continue
                name = str(r0.get("name") or "").strip()
                ids = [str(x).strip() for x in (r0.get("ids") or []) if str(x).strip()]
                drive = (str(r0.get("drive") or "C")).rstrip(":").upper() or "C"
                if not name:
                    rep["rules_skipped"].append(f"rules[{i}] 缺规则名")
                    continue
                if not ids:
                    rep["rules_skipped"].append(f"「{name}」没有勾选任何清理项")
                    continue
                if drive not in valid_cache:
                    valid_cache[drive] = {t["id"]
                                          for t in build_targets(drive, None)}
                bad = [x for x in ids if x not in valid_cache[drive]]
                if bad:
                    rep["rules_skipped"].append(
                        f"「{name}」含 {len(bad)} 个当前不存在的清理项"
                        f"（如 {bad[0]}）")
                    continue
                existing = next((r for r in rules if r["name"] == name), None)
                if existing:
                    existing["ids"] = ids
                    existing["drive"] = drive
                    existing["auto"] = bool(r0.get("auto"))
                    rep["rules_updated"] += 1
                else:
                    rules.append({
                        "id": f"rule-{int(time.time() * 1000) % 10 ** 10}{i}",
                        "name": name, "ids": ids, "drive": drive,
                        "auto": bool(r0.get("auto")),
                        "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    rep["rules_added"] += 1
            save_custom_rules(rules)
            return self._json({"ok": True, **rep})

        # ---- 自定义规则（勾选固定项 → 命名保存 → 可套用/自动清理）----
        if route == "/__oc__/custom-rules":
            rules = load_custom_rules()
            action = body.get("action")

            if action == "add" or action == "update":
                name = (body.get("name") or "").strip()
                ids = [str(i) for i in (body.get("ids") or [])]
                drive = (body.get("drive") or "C").rstrip(":").upper()
                if not name:
                    return self._json({"error": "请给规则起一个名字"}, 400)
                if not ids:
                    return self._json({"error": "请先勾选至少一个清理项，再保存为规则"}, 400)
                # 校验：勾选的必须是当前真实存在的目标 id
                valid = {t["id"] for t in build_targets(drive, body.get("custom_path") or None)}
                bad = [i for i in ids if i not in valid]
                if bad:
                    return self._json({"error": f"有 {len(bad)} 个清理项已不适用于 {drive}: 盘，"
                                                f"请重新勾选"}, 400)
                existing = next((r for r in rules if r["name"] == name), None)
                if existing:
                    existing["ids"] = ids
                    existing["drive"] = drive
                    existing["auto"] = bool(body.get("auto"))
                    rule = existing
                else:
                    rule = {"id": f"rule-{int(time.time() * 1000) % 10**10}",
                            "name": name, "ids": ids, "drive": drive,
                            "auto": bool(body.get("auto")),
                            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                    rules.append(rule)
                save_custom_rules(rules)
                return self._json({"rules": rules, "saved": rule})

            if action == "delete":
                rules = [r for r in rules if r.get("id") != body.get("id")]
                save_custom_rules(rules)
                return self._json({"rules": rules})

            if action == "rename":
                new_name = (body.get("name") or "").strip()
                if not new_name:
                    return self._json({"error": "规则名不能为空"}, 400)
                for r in rules:
                    if r.get("id") == body.get("id"):
                        r["name"] = new_name
                save_custom_rules(rules)
                return self._json({"rules": rules})

            return self._json({"rules": rules})

        # ---- 执行一条自定义规则（勾选固定项的立即清理）----
        if route == "/__oc__/run-rule":
            rid = body.get("id")
            rule = next((r for r in load_custom_rules() if r["id"] == rid), None)
            if not rule:
                return self._json({"error": "规则不存在"}, 404)
            drive = (rule.get("drive") or "C").rstrip(":").upper()
            custom_path = body.get("custom_path") or None
            targets = build_targets(drive, custom_path)
            by_id = {t["id"]: t for t in targets}
            selected = [by_id[i] for i in rule["ids"] if i in by_id]
            if not selected:
                return self._json({"error": "规则里的清理项在当前盘上都不再适用，"
                                            "请重新勾选后覆盖保存"}, 400)
            if getattr(self.server, "busy", False):
                return self._json({"error": "已有清理任务在执行，请等待完成"}, 409)
            self.server.busy = True
            try:
                entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "drive": drive,
                         "trigger": f"规则「{rule['name']}」（{drive}:）",
                         "rule": rule["name"],
                         "results": [], "freed": 0, "failed": 0,
                         "targets": len(selected)}
                for t in selected:
                    try:
                        r = clean_target(t, None, drive)
                    except Exception as exc:  # noqa: BLE001
                        traceback.print_exc()
                        r = _error_result(t, drive, exc)
                    entry["results"].append(r)
                    entry["freed"] += r["freed"]
                    entry["failed"] += r["failed"]
                entry["freed_h"] = human(entry["freed"])
                append_history(entry)
                return self._json(apply_lang_payload(
                    {"entry": entry, "disk": get_disk_info(drive),
                     "stats": history_stats(), "rule": rule["name"]},
                    self._query().get("lang")))
            finally:
                self.server.busy = False

        # ---- 在资源管理器中打开 / 定位 ----
        if route == "/__oc__/reveal":
            path = expand(str(body.get("path") or ""))
            if os.path.exists(path) and IS_WINDOWS:
                np = os.path.normpath(path)
                if body.get("select"):
                    # explorer /select：打开父级并高亮本项；项本身不存在时退回打开父级
                    target = np if os.path.exists(np) else os.path.dirname(np)
                    subprocess.Popen(["explorer.exe", "/select,", target],
                                     creationflags=CREATE_NO_WINDOW)
                else:
                    subprocess.Popen(["explorer.exe", np],
                                     creationflags=CREATE_NO_WINDOW)
                return self._json({"ok": True})
            return self._json({"error": "路径不存在"}, 404)

        # ---- 资源包 ----
        if route == "/__oc__/packs/import":
            # 三种入参：content（包本体）、path（本机文件）、url（远端地址）
            data = body.get("content")
            if data is None and body.get("path"):
                data = str(body["path"])
            if data is None and body.get("url"):
                data, err = fetch_json(str(body["url"]))
                if err:
                    return self._json({"ok": False, "error": err}, 400)
            if data is None:
                return self._json({"ok": False,
                                   "error": "需要 content / path / url 之一"}, 400)
            ok, why = import_pack(data)
            res = {"ok": ok, "packs": load_packs(use_cache=False)["packs"]}
            res["message" if ok else "error"] = why
            return self._json(res, 200 if ok else 400)
        if route == "/__oc__/packs/toggle":
            pid, enable = str(body.get("id") or ""), body.get("enabled")
            if not pid:
                return self._json({"ok": False, "error": "缺少 id"}, 400)
            st = load_packs_state()
            dis = set(st["disabled"])
            dis.discard(pid) if enable else dis.add(pid)
            st["disabled"] = sorted(dis)
            save_packs_state(st)
            return self._json({"ok": True,
                               "packs": load_packs(use_cache=False)["packs"]})
        if route == "/__oc__/packs/delete":
            pid = str(body.get("id") or "")
            f = USER_PACKS_DIR / f"{os.path.basename(pid)}.json"
            if not pid or not f.is_file():
                # 内置包不可删，只能停用 —— 删了会被下次更新又拉回来
                return self._json({"ok": False,
                                   "error": "只能删除自己导入的包；内置包请用停用"},
                                  400)
            try:
                f.unlink()
            except OSError as e:
                return self._json({"ok": False, "error": str(e)}, 500)
            st = load_packs_state()
            st["disabled"] = [x for x in st["disabled"] if x != pid]
            save_packs_state(st)
            return self._json({"ok": True,
                               "packs": load_packs(use_cache=False)["packs"]})
        if route == "/__oc__/packs/update":
            if body.get("remote"):
                st = load_packs_state()
                st["remote"] = str(body["remote"]).strip()
                save_packs_state(st)
            return self._json(packs_update_from_remote())

        # ---- 接入自有 AI（密钥只在服务端使用，不回传）----
        if route == "/__oc__/ai/config":
            patch = {k: v for k, v in (body or {}).items() if k in _AI_DEFAULTS}
            # 前端回传空密钥表示「不改」，避免每次保存都把已存的密钥抹掉
            if not str(patch.get("api_key") or "").strip():
                patch.pop("api_key", None)
            save_ai_config(patch)
            return self._json({"ok": True, "ai": ai_public_config()})
        if route == "/__oc__/ai/test":
            return self._json(ai_test())
        if route == "/__oc__/ai/models":
            return self._json(ai_list_models())
        if route == "/__oc__/ai/explain":
            return self._json(ai_explain(body or {}))
        if route == "/__oc__/ai/plan":
            return self._json(ai_plan(body or {}))

        # ---- 检查应用更新（只读 GitHub Releases，不自动下载） ----
        if route == "/__oc__/app/update_check":
            return self._json(app_update_check())

        return self._json({"error": "not found"}, 404)


def pick_port(preferred: int) -> int:
    """找一个真正空闲的端口。探测必须用裸 bind，不能带 SO_REUSEADDR：
    Windows 上带了它，已被其他进程监听的端口也能 bind 成功，
    新旧实例会静默双绑定同一端口（请求随机落到旧实例上）。
    裸 bind 才是「这个端口此刻真的没人用」的判定；代价是刚关闭的端口
    会进入 TIME_WAIT 而被跳过 —— 顺延到下一个端口正是想要的行为。
    """
    for port in range(preferred, preferred + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
        if port != preferred:
            print(f"端口 {preferred} 已被占用（可能已有 O-Cleaner 在运行），"
                  f"改用 {port}")
        return port
    raise SystemExit(f"{preferred} 起连续 30 个端口都不可用")


def main() -> None:
    if IS_WINDOWS:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 清掉上次被强杀时遗留的 WizTree 导出文件（正常路径在 finally 里已删）
    stale = cleanup_stale_wiztree_exports()
    if stale:
        print(f"已清理 {stale} 个上次遗留的 WizTree 导出文件")

    ap = argparse.ArgumentParser(description="O-Cleaner 磁盘清理助手")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--version", action="store_true",
                    help="打印版本号后退出（排障/打包核对用，版本单点定义在 APP_VERSION）")
    ap.add_argument("--drive", default=None, help="命令行扫描时指定盘符，如 D")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--fullscan-only", action="store_true",
                    help="命令行扫描磁盘占用树并打印，不开服务")
    ap.add_argument("--depth", type=int, default=0,
                    help="配合 --fullscan-only 指定展开层数（默认 5）")
    ap.add_argument("--data-dir", default=None, metavar="PATH",
                    help="数据存档目录：config/ 与历史记录、定时任务等运行数据"
                         "的存放位置（也可用环境变量 OC_DATA_DIR；默认跟程序同目录，"
                         "整个文件夹拷走即完成数据迁移）")
    ap.add_argument("--lint", action="store_true",
                    help="只跑规则 linter（发布/贡献规则前用），不开服务")
    args = ap.parse_args()

    if args.version:
        print(f"O-Cleaner v{APP_VERSION}")
        return

    dcfg = load_drives_cfg()

    if args.lint:
        lint = BASE_DIR / "tools" / "lint_rules.py"
        if not lint.exists():
            print("找不到 tools/lint_rules.py")
            return
        import subprocess as _sp
        sys.exit(_sp.call([sys.executable, "-X", "utf8", str(lint), "--strict"]))

    if args.fullscan_only:
        letter = (args.drive or "C").rstrip(":").upper()
        depth = int(args.depth or DISK_TREE_MAX_DEPTH)
        print(f"O-Cleaner 磁盘占用树 —— {letter}: 盘"
              f"（逐层展开到 {depth} 层，请稍候）")
        r = scan_disk_overview(letter, max(1, min(depth, 6)))
        if r.get("error"):
            print("  失败：" + r["error"])
            return
        nodes = r["nodes"]
        by_parent: dict[str, list[dict]] = {}
        for n in nodes:
            by_parent.setdefault(n["parent"], []).append(n)

        # 根节点的键是规范化后的盘符根（"e:\\"），另外深度 1 的节点
        # 其 parent 也是这个键 —— 用「深度 1 的 parent」反推最稳，
        # 不要自己拼字符串（大小写、尾斜杠、normpath 都可能不一致）。
        root_key = next((n["parent"] for n in nodes if n.get("depth") == 1), "")

        def dump(parent_key: str, indent: str = "  ") -> None:
            kids = sorted(by_parent.get(parent_key, []), key=lambda x: -x["size"])
            for k in kids:
                kind = "目录" if k["type"] == "dir" else "文件"
                if k["cleanable"]:
                    advice = "[可清理]"
                elif k["recognized"]:
                    advice = "[建议保留]"
                else:
                    advice = "[未识别]  "
                print(f"{indent}{human(k['size']):>10}  {k['name'][:32]:<34}"
                      f"{kind}  {advice}  {(k['title'] or '')[:20]}")
                dump(os.path.normpath(k["path"]).lower(), indent + "  ")

        print(f"  扫描时间 {r['scan_time']}    节点 {r['node_count']} 个，"
              f"已识别 {r['explained']} 个，子项门槛 {r['min_child_h']}")
        print(f"  顶层合计 {r['scanned_total_h']}")
        print("-" * 104)
        print("        大小  名称 / 类型 / 建议 / 识别结果")
        print("-" * 104)
        dump(root_key)
        print("-" * 104)
        cl = [n for n in nodes if n["cleanable"]]
        print(f"  可清理合计 {human(sum(n['size'] for n in cl))}（{len(cl)} 个节点）")
        return

    if args.scan_only:
        letter = (args.drive or "C").rstrip(":").upper()
        print(f"O-Cleaner 扫描报告 —— {letter}: 盘")
        print(f"{'项目':<42}{'大小':>12}{'文件数':>10}  路径")
        print("-" * 100)
        total = 0
        for t in build_targets(letter):
            info = measure_target(t, letter)
            total += info.get("size", 0)
            print(f"{expand(t.get('label', t['id']), letter):<42}"
                  f"{human(info.get('size', 0)):>12}{info.get('files', 0):>10}  "
                  f"{expand(t['path'], letter)}")
        print("-" * 100)
        print(f"{'合计':<42}{human(total):>12}")
        return

    port = pick_port(args.port or int(dcfg.get("app", {}).get("port", 8770)))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.busy = False  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print()
    print("  O-Cleaner 已启动")
    print(f"  界面地址: {url}")
    print(f"  数据目录: {DATA_DIR}"
          + ("（--data-dir 重定向）" if DATA_DIR != BASE_DIR else ""))
    print(f"  配置文件: {CONFIG_DIR}")
    print(f"  管理员权限: {'是' if is_admin() else '否（系统目录的项目会提示权限不足）'}")
    print("  关闭此窗口即停止服务。")
    print()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
