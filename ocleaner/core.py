# -*- coding: utf-8 -*-
# P2-1 巨石拆分（2026-11-05）：本文件是 app.py 的实现主体整体迁入。
# 仓库根的 app.py 只保留 re-export shim + __main__ 入口；
# selftest / doctor / guard_matrix / unit_test 以
# `import ocleaner.core as app` 取得同一模块对象，monkeypatch 语义不变。
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
import platform
import re
import shutil
import socket
import stat
import string
import struct
import subprocess
import sys
import ipaddress
import traceback
import webbrowser
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError as _HTTPError  # noqa: F401 integrations 经 _core 引用
from urllib.request import Request as _UrlRequest, urlopen as _urlopen
# PS_MEASURE_TREE/PS_MEASURE_GLOB/PS_DIR_KIND/PS_STEAM_ORPHAN_SIZE 的
# 测量职责已由进程内 os.scandir 实现（P2-4），脚本本体保留：
# selftest 阶段六仍直接引用 app.PS_STEAM_ORPHAN_SIZE 钉 run_ps 数组契约。
# PS_MEASURE_TREE/PS_MEASURE_GLOB/PS_DIR_KIND 三个死模板已从
# ps_scripts.py 删除（职责迁 ocleaner/measure.py，P2-4）；
# PS_STEAM 仍被 games.py 经 _core 引用（Steam 清理脚本），勿删。
from ps_scripts import (PS_BIG_FILES, PS_LOCK_HOLDERS, PS_RECYCLE, PS_STEAM,  # noqa: F401 re-export
                        PS_STEAM_ORPHAN_SIZE, PS_TREE_WALK,  # noqa: F401 re-export
                        PS_UTF8_PREAMBLE)  # noqa: F401 selftest 引用钉契约
# P2-1 巨石拆分（2026-11-05 起步）：叶子模块迁入 ocleaner/ 包，  # noqa: F401 re-export
# app.py re-export 保持 `import app` 下游与裸名调用全部兼容。  # noqa: F401 re-export
from ocleaner.credentials import _dpapi_protect, _dpapi_unprotect  # noqa: F401 re-export
from ocleaner.measure import (_py_walk_files, _py_measure_tree,  # noqa: F401 re-export
                              _py_measure_glob, _py_dir_kind, _py_tree_size)  # noqa: F401 re-export
from ocleaner.store import (_cache, _lock, _cross_process_lock,  # noqa: F401 re-export
                            _load_json, _atomic_write)  # noqa: F401 re-export
from ocleaner.deleter import (rm, rm_tree_strict, _journal_stage,  # noqa: F401 re-export
                              _journal_done, recover_pending_deletes,  # noqa: F401 re-export
                              rm_trash, _precheck_locked_files,  # noqa: F401 re-export
                              _is_reparse_dir, _long_path)  # noqa: F401 re-export
from ocleaner.games import (discover_steam, _parse_vdf, steam_common_dirs,  # noqa: F401 re-export
                           game_install_dirs, platform_install_roots,  # noqa: F401 re-export
                           _epic_game_from_manifest, _gog_game_from_values,  # noqa: F401 re-export
                           _platform_orphans, discover_epic, discover_gog,  # noqa: F401 re-export
                           discover_games, game_display_name, gamelib_snapshot,  # noqa: F401 re-export
                           load_game_libs, save_game_libs, load_game_names,  # noqa: F401 re-export
                           record_game_history, _in_game_install,  # noqa: F401 re-export
                           _gamelib_children, GAME_INSTALL_MARKERS,  # noqa: F401 re-export
                           _GAMELIB_CACHE, _GAMELIB_CACHE_TTL, _GAMELIBS_LOCK,  # noqa: F401 re-export
                           _GAME_DIRS_CACHE,  # noqa: F401 re-export
                           _STEAM_COMMON_CACHE, _ORPHAN_ROOTS_CACHE,  # noqa: F401 re-export
                           _ORPHAN_NAME_EXCLUDES, STEAM_STATE_FLAGS)  # noqa: F401 re-export
from ocleaner.integrations import (_sync_cfg, _sync_save_cfg,  # noqa: F401 re-export
                              _sync_token, _gist_request, _sync_push,  # noqa: F401 re-export
                              _sync_fetch, _sync_pull, _sync_two_way,  # noqa: F401 re-export
                              load_ai_config, save_ai_config,  # noqa: F401 re-export
                              ai_public_config, ai_list_models, ai_chat,  # noqa: F401 re-export
                              ai_test, ai_explain, ai_explain_batch, ai_plan,  # noqa: F401 re-export
                              app_update_check, _dpapi_migrate_plaintext,  # noqa: F401 re-export
                              _AI_DEFAULTS)  # ai/config 路由过滤用  # noqa: F401 re-export
  # noqa: F401 re-export
BASE_DIR = Path(__file__).resolve().parent.parent
WIZTREE_PORTABLE_DIR = BASE_DIR / "portable" / "WizTree"  # noqa: F401 re-export
  # noqa: F401 re-export
  # noqa: F401 re-export
def _argv_data_dir() -> str:  # noqa: F401 re-export
    """启动参数里找 --data-dir（须在模块常量定值前生效，故在 import 期预扫）。"""  # noqa: F401 re-export
    for i, v in enumerate(sys.argv):  # noqa: F401 re-export
        if v == "--data-dir" and i + 1 < len(sys.argv):  # noqa: F401 re-export
            return sys.argv[i + 1]  # noqa: F401 re-export
        if v.startswith("--data-dir="):  # noqa: F401 re-export
            return v.split("=", 1)[1]  # noqa: F401 re-export
    return ""  # noqa: F401 re-export
  # noqa: F401 re-export
  # noqa: F401 re-export
# 数据存档目录：config/ 与 history.json、schedule.json、disk-trend.json 等  # noqa: F401 re-export
# 全部运行数据都落在这里。默认跟程序同目录（绿色便携：整个文件夹拷走 =  # noqa: F401 re-export
# 数据一起迁移，U 盘携带即插即用）；程序目录只读或想把数据单独存放时，  # noqa: F401 re-export
# 用 --data-dir <路径> 或环境变量 OC_DATA_DIR 重定向到任意可写位置。  # noqa: F401 re-export
DATA_DIR = Path(os.environ.get("OC_DATA_DIR") or _argv_data_dir() or BASE_DIR)  # noqa: F401 re-export
# 数据目录可写性的进程内缓存（None = 尚未探测），见 data_dir_writable()  # noqa: F401 re-export
_DATA_DIR_WRITABLE: bool | None = None
CONFIG_DIR = DATA_DIR / "config"
WEB_DIR = BASE_DIR / "web"

# 版本号只在这里定义一次。
# 此前它同时散落在三处（这里的 server_version、WizTree 下载的 User-Agent、
# config/drives.json 的 app.version），2026-10-12 升 v2.3 时漏改了后两处，
# 结果界面显示的仍是 2.2。收敛成一个常量后，drives.json 与打包脚本由自测断言
# 强制保持一致（见 tools/selftest.py 的「版本号三处一致」）。
APP_VERSION = "2.18"
APP_UA = f"O-Cleaner/{APP_VERSION}"

TARGETS_PATH = CONFIG_DIR / "targets.json"
DRIVES_PATH = CONFIG_DIR / "drives.json"
CONVERT_PATH = CONFIG_DIR / "convert.json"
FILETYPES_PATH = CONFIG_DIR / "filetypes.json"
CUSTOM_PATH = CONFIG_DIR / "custom-rules.json"
# 用户自定义聚合组：把占用树里任意路径归拢进自己命名的组（如「游戏」「工作」）。
# 纯显示层组织，不影响识别与清理判定；只存本机、不进版本库、不进分发包。
FS_GROUPS_PATH = CONFIG_DIR / "fs-groups.json"
# 用户自定义游戏库：游戏占用概览里手动添加的库/游戏目录（名称 + 路径）。
# 只读展示（量体积、列子项），不参与任何清理判定；只存本机、不进版本库。
# 用户自定义释义 / 结论覆盖：按清理项改写说明文案或翻转「可清理」结论。
# 用户显式操作的最后一手，优先级高于资源包与内置知识库；
# 只存本机、不进版本库、不进分发包，见 .gitignore。
OVERRIDES_PATH = CONFIG_DIR / "custom-overrides.json"
# 游戏中文名映射：纯显示用，不参与任何清理判断
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
GAMELIBS_PATH = CONFIG_DIR / "game-libs.json"
GAMENAMES_PATH = CONFIG_DIR / "game-names.json"
_SYNC_CFG_PATH = CONFIG_DIR / "sync.json"
# 运行数据目录（2026-10-29 起）：history/schedule/趋势/回执/删除中断日志/锁文件
# 全部收进 data/ 子目录，程序根目录不再散落一堆 json。旧版把这类文件直接
# 放在数据目录根下 —— 启动时 _migrate_runtime_files() 会把它们搬进来，
# 老用户无感升级。gitignore 整个 data/，运行数据永不进版本库。
RUNTIME_DIR = DATA_DIR / "data"
HISTORY_PATH = RUNTIME_DIR / "history.json"
# 各盘用量快照（磁盘趋势曲线的数据源）：每天每盘最多记一条，保留 90 天。
# 运行数据，不进版本库、不进分发包。
DISK_TREND_PATH = RUNTIME_DIR / "disk-trend.json"
SCHEDULE_PATH = RUNTIME_DIR / "schedule.json"
# 游戏库总体积快照（游戏占用趋势曲线的数据源）：每天最多一条，保留 180 天。
GAME_HISTORY_PATH = RUNTIME_DIR / "game-history.json"
AI_CACHE_PATH = RUNTIME_DIR / "ai-cache.json"
# 无图标游戏的默认占位图（用户可通过 /__oc__/cover-placeholder 自定义）。
# 存原始图片字节；不存在时回内置 SVG。运行数据，不进版本库、不进分发包。
COVER_PLACEHOLDER_PATH = RUNTIME_DIR / "cover-placeholder.img"

# 内置占位图：手柄轮廓 SVG，随包分发，恒可用。配色中性（半透明灰），
# 在浅色/深色主题和 22px 缩略图下都不突兀。
_PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#9aa3ad" opacity="0.28"/>'
    '<path d="M22 22h20c7 0 12 5 13 12l1.5 10c.5 4-2.5 8-7 8-3 0-5-1.5-7-4'
    'l-2.5-3.5c-1.2-1.6-3-2.5-5-2.5H29c-2 0-3.8.9-5 2.5L21.5 48c-2 2.5-4 4-7 4'
    '-4.5 0-7.5-4-7-8L9 34c1-7 6-12 13-12z" fill="#7d8794" opacity="0.55"/>'
    '<path d="M18 30v8M14 34h8" stroke="#4c5560" stroke-width="2.4"'
    ' stroke-linecap="round" opacity="0.7"/>'
    '<circle cx="42" cy="31.5" r="2.2" fill="#4c5560" opacity="0.7"/>'
    '<circle cx="48" cy="36.5" r="2.2" fill="#4c5560" opacity="0.7"/>'
    "</svg>"
)
from ocleaner.engines import (find_wiztree, wiztree_available, wiztree_status,
                             acquire_wiztree, cleanup_stale_wiztree_exports,
                             _wiztree_export, _wiztree_parse, _wiztree_children,
                             _wiztree_cleanup, _wiz_table, _wiz_ok, _mft_k32,
                             _mft_volume_ntfs, _mft_open_volume, _mft_read_at,
                             _mft_parse_at, _mft_parse_record, _mft_name_decode,
                             _mft_runlist, _mft_build_table, _mft_agg_paths,
                             _mft_big_files, _wiztree_raw_nodes, _WIZ_CACHE,
                             WIZTREE_NAMES, WIZTREE_HINTS, WIZTREE_PORTABLE_URL,
                             WIZTREE_SETUP_URL, WIZTREE_HOME, WIZTREE_DIST_LICENSE,
                             WIZTREE_LICENSE_NOTE,
                             _MFT_FSCTL_GET_NTFS_VOLUME_DATA, _MFT_K32_READY)  # noqa: F401 re-export
from ocleaner.guard import (check_deletable, _guarded_zone,
                            _looks_like_installed_program,
                            _looks_like_browser_profile,
                            _looks_like_game_install,
                            _content_says_program_body, _override_key,
                            _apply_target_override, ALLOWED_CONTAINER_NAMES,
                            RESERVED_DEVICE_NAMES, ZONE_CACHE_EXEMPT,
                            PROTECTED_AT_DRIVE_ROOT, PROTECTED_DIR_NAMES,
                            USER_DATA_DIR_NAMES, _ZONE_LABEL,
                            _WEIGHT_MIN_BYTES, _CONTENT_SCAN_ENTRY_CAP,
                            _CONTENT_SCAN_SUBDIR_CAP, _PROGRAM_WEIGHT_EXTS,
                            _PROGRAM_EXE_DLL_MIN,
                            BROWSER_CACHE_DIRS)  # noqa: F401 re-export  # noqa: F401 re-export

from ocleaner.analysis import (_save_scan_snapshot, snapshot_diff,  # noqa: F401 re-export
                               fs_search, trend_forecast,  # noqa: F401 re-export
                               BIG_FILES_FLOOR_PS,  # noqa: F401 re-export
                               BIG_FILES_FLOOR_WIZ, BIG_FILES_LIMIT,  # noqa: F401 re-export
                               _wiz_mtime_norm, _wiztree_big_files, big_files,  # noqa: F401 re-export
                               _watch_bigfiles, DUP_FLOOR_MB, DUP_MAX_GROUPS,  # noqa: F401 re-export
                               DUP_MAX_PER_GROUP, _sample_hash, _full_sha256,  # noqa: F401 re-export
                               _load_bloatware, _parse_uninstall_reg_key,  # noqa: F401 re-export
                               bloatware_scan, bloatware_sweep, junction_move,  # noqa: F401 re-export
                               junction_restore, _load_appclean_apps, appclean_scan,  # noqa: F401 re-export
                               _appclean_cache_allowed, appclean_clean,  # noqa: F401 re-export
                               glob_dirs, DUP_PROGRESS, _BIG_CACHE, _APPCLEAN_CACHE,  # noqa: F401 re-export
                               _BLOAT_CACHE, _DUP_CACHE, dupfiles_scan, _downloads_dir,  # noqa: F401 re-export
                               downloads_advice, _dir_size, downloads_archive,  # noqa: F401 re-export
                               downloads_archive_undo)  # noqa: F401 re-export
from ocleaner.server import Handler  # noqa: F401 re-export
from ocleaner.scheduler import (DEFAULT_SCHEDULE, schedule_state,  # noqa: F401 re-export
                                _resolve_preset, _schedule_run_interval,  # noqa: F401 re-export
                                _apply_preset_to_targets, _scheduler_loop,  # noqa: F401 re-export
                                _run_scheduled, _error_result)  # noqa: F401 re-export
from ocleaner.receipts import (load_history, append_history,  # noqa: F401 re-export
                               _write_receipt, list_receipts,  # noqa: F401 re-export
                               read_receipt)  # noqa: F401 re-export


def _image_mime(data: bytes) -> str:
    """按 magic bytes 认图：只认 PNG / JPEG / GIF / SVG(文本)。认不出回空串。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    try:
        text = data[:512].decode("utf-8", errors="ignore").lstrip()
    except Exception:                          # noqa: BLE001
        return ""
    if text.startswith("<svg") or text.startswith("<?xml"):
        return "image/svg+xml"
    return ""


def _migrate_runtime_files() -> None:
    """老版本把运行数据散在数据目录根下；搬进 data/（一次性、幂等）。"""
    moves = [
        DATA_DIR / "history.json", DATA_DIR / "disk-trend.json",
        DATA_DIR / "schedule.json", DATA_DIR / "pending-deletes.json",
    ]
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        for old in moves:
            new = RUNTIME_DIR / old.name
            if old.exists() and not new.exists():
                old.replace(new)
        old_rc = DATA_DIR / "receipts"
        new_rc = RUNTIME_DIR / "receipts"
        if old_rc.is_dir() and not new_rc.exists():
            old_rc.replace(new_rc)
    except OSError:
        pass        # 迁移失败不挡启动：新文件照常写在 data/，旧文件留在原地


_migrate_runtime_files()          # 模块导入即迁移：老布局一次性收进 data/

# 绝不允许删除的目录名（即使配置文件被改坏也拦得住）
# 用户自己的数据目录（2026-10-21）：这些目录默认不清理、不进推荐、不进预设，
# 但**用户可以自己选**（显式勾选 → allow_user_data=True）——「不做删，做成可选」。
# 与上面的系统目录区别在于：系统目录即便用户勾选也绝不删（删了系统就坏了），
# 而这几个是用户自己的东西，要不要清由用户自己决定，工具只负责不替他做决定。
# 这些名字只有在“盘符根目录的直接子目录”层级才整体保护；
# 更深的同名目录（例如 C:\Windows\Temp）允许清理，由更深层的规则把关。
# 允许作为“清理容器”的目录名（即使名字本身在通用保护名单里）

# ---- 护栏升级（2026-10-21）----
# 保留设备名：路径中任何一段命中，Windows 会把它解释成设备而不是文件，
# 删除这类路径实际打到的是设备句柄，行为不可预期（甚至无提示失败）。

# 「受保护域」：系统盘上这几棵子树里的**整目录删除**一律默认拒绝，
# 需用户显式勾选（allow_user_data=True）才放行。理由见 _guarded_zone()。





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


# 当前实例实际绑定的端口（一键提权重启时原样传给新实例）
_BOUND_PORT: int | None = None


def elevate_relaunch() -> dict:
    """一键提权：弹 UAC 以管理员身份重启自身，成功后本实例延迟退出让位。

    新实例带 --port（沿用当前端口）+ --wait-free（等老实例退出释放端口），
    浏览器照常自动打开 —— 用户直接落到管理员会话。
    新实例优先用 pythonw.exe 启动且以 SW_HIDE 显示：管理员实例不再弹
    黑色命令行窗口（GUI 页面照常可用；pythonw 下 print 是无害的 no-op）。
    UAC 被取消 / 失败时本实例**不退出**（rc<=32 即失败，ShellExecuteW 约定）。"""
    if not IS_WINDOWS:
        return {"ok": False, "error": "仅 Windows 支持一键提权"}
    if is_admin():
        return {"ok": False, "error": "当前已经是管理员权限，无需提权"}
    import sys
    script = str((BASE_DIR / "app.py").resolve())
    # 提权后不再多开新浏览器页——旧页面本来就在
    # 轮询同端口等新实例就绪（收到 is_admin 后 location.reload() 落到
    # 管理员会话），新实例带 --no-browser 即可避免「旧页恢复 + 新页又开」
    # 的双页残留。能点到提权按钮的页面必然活着，无条件加是安全的。
    port_arg = (f" --port {_BOUND_PORT} --wait-free 25 --no-browser"
                if _BOUND_PORT else " --no-browser")
    # pythonw.exe = 无控制台的同一解释器；没有（如嵌入式发行版精简掉它）
    # 就退回 python.exe —— 此时窗口交给 SW_HIDE 压掉，两条路都不弹窗。
    target = Path(sys.executable).with_name("pythonw.exe")
    exe = str(target) if target.is_file() else sys.executable
    rc = ctypes.windll.shell32.ShellExecuteW(
        None, "runas", exe, f'"{script}"{port_arg}', str(BASE_DIR), 0)  # 0 = SW_HIDE
    if rc <= 32:
        return {"ok": False,
                "error": f"提权未完成（UAC 被取消或失败，代码 {rc}）"}

    def _bye():
        # 给响应包留出发送时间；新实例由 --wait-free 负责等端口
        try:
            print("  [提权] 正在让出端口，切换到管理员实例…")
        except Exception:
            pass
        os._exit(0)

    threading.Timer(1.2, _bye).start()
    return {"ok": True}


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
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)   # 全新安装时 data/ 还不存在
        probe = RUNTIME_DIR / f".oc_write_probe_{os.getpid()}"
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

    # P2-4：进程内测量（免 powershell.exe 冷启动，180–350ms/次）
    if kind == "files":
        r = _py_measure_glob(path, str(t.get("glob", "*")))
    else:
        r = _py_measure_tree(path)
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
FALLBACK_SIZING_BUDGET_S = 8                        # 无 MFT 加速时，单层量体积的时间预算
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

# 官方下载地址（diskanalyzer.com，Antibody Software Limited）。
# 优先用**便携版 zip**：免安装、不需要管理员权限，解压到本目录后整个
# O-Cleaner 文件夹拷到别的电脑也能直接用 —— 这正是「可移动」的关键。
# 第三方许可说明。**必须如实告知**：它决定了能不能把 WizTree 随包分发。
# 便携版解压到这里（相对 O-Cleaner 根目录，跟着文件夹走）

















def _wiztree_direct_prefix(path: str) -> tuple[str, str, int]:
    """「直接子目录」判定的公共参数：规范化键、子目录前缀、目标层数。"""
    key = os.path.normpath(path).lower()
    pre = key if key.endswith("\\") else key + "\\"
    return key, pre, pre.count("\\")




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




def _ext_of(path: str) -> str:
    """路径 → 小写扩展名（不含点，≤16 字符；无扩展名返回空串）。

    类型分布关心的是「大块头都是什么类型」：.mp4/.iso/.zip 这类主干类型，
    不追求覆盖怪异命名。双扩展名（.tar.gz）按最后一段算。
    """
    name = path.rstrip("\\").rsplit("\\", 1)[-1]
    if "." not in name:
        return ""
    ext = name.rsplit(".", 1)[-1].lower()
    return ext[:16]


def _ext_bump(exts: dict, path: str, size: int) -> None:
    """把一个文件按扩展名累计进分布表（[字节, 个数]）。"""
    e = _ext_of(path)
    st = exts.get(e)
    if st is None:
        exts[e] = [size, 1]
    else:
        st[0] += size
        st[1] += 1


def _ext_top(exts: dict, top: int = 12) -> list:
    """分布表 → 前 top 类型列表（体积降序），其余并入 other。

    输出条目 {"ext", "size", "count"}；空串键表示「无扩展名」。
    """
    if not exts:
        return []
    items = sorted(exts.items(), key=lambda kv: -kv[1][0])
    out = [{"ext": k, "size": v[0], "count": v[1]} for k, v in items[:top]]
    rest = items[top:]
    if rest:
        out.append({"ext": "__other__",
                    "size": sum(v[0] for _k, v in rest),
                    "count": sum(v[1] for _k, v in rest)})
    return out


def _ext_of_raw(raw) -> str:
    """UTF-16LE 原始名 → 小写扩展名（不做全名解码，省百万次 decode）。

    UTF-16LE 的 ASCII 区里 '.' 就是 b'.\\x00'，从最后一个往后截一小段
    解码即可 —— 热路径上只为扩展名解码几个字符。
    """
    i = raw.rfind(b".\x00")
    if i < 0:
        return ""
    return raw[i + 2:].decode("utf-16-le", "replace").lower()[:16]


def _ext_bump_raw(exts: dict, raw, size: int) -> None:
    """按 UTF-16LE 原始名把文件累计进扩展名分布表。"""
    e = _ext_of_raw(raw)
    st = exts.get(e)
    if st is None:
        exts[e] = [size, 1]
    else:
        st[0] += size
        st[1] += 1


_FT_EPOCH_DIFF = 116444736000000000   # FILETIME(1601) → Unix 纪元的 100ns 差


def _ft_to_iso(ft: int) -> str:
    """NTFS FILETIME → 本地时区 'YYYY-MM-DD HH:MM'；0 或异常返回空串。"""
    if not ft or ft <= 0:
        return ""
    try:
        ts = (ft - _FT_EPOCH_DIFF) / 10_000_000
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


# 默认隐藏的系统项：资源管理器勾着「隐藏受保护的操作系统文件」时不显示，
# 但 MFT/完整递归都能扫到。前端把这些集中到一个「隐藏项」页签里，
# 避免「扫出来的和资源管理器看到的不一样」让用户困惑。
# 除目录外还收 NTFS 的根级元文件（$MFT / $LogFile / $Extend 等）：
# 它们是文件系统自身的记账数据，只存在于盘根，用户删不了也不该删。
_HIDDEN_SYS_NAMES = {
    "$recycle.bin", "recycler", "system volume information", "config.msi",
    "pagefile.sys", "hiberfil.sys", "swapfile.sys",
    "dumpstack.log", "dumpstack.log.tmp", "$winreagent",
    "$mft", "$mftmirr", "$logfile", "$volume", "$attrdef", "$bitmap",
    "$boot", "$badclus", "$secure", "$upcase", "$extend",
    # Store 托管的应用目录：系统建立与回收，用户从不手建；部分盘上它只有
    # ReadOnly 属性（实测 D:\WindowsApps），Explorer 语义上仍是系统项，
    # 不进名单就会单独混在占用排行里。
    "windowsapps",
}

_FILE_ATTR_HIDDEN = getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)


def _is_hidden_sys(name: str, path: str = "", attrs: int | None = None) -> bool:
    """是否「资源管理器默认看不见」的项。

    判据以**磁盘上的真实属性**为准，不再是名字名单：Windows 资源管理器
    默认关着「隐藏的项目」（HIDDEN 属性即隐藏），所以只要带 Hidden 就算。
    此前只看那 20 来个名字，实测漏掉的不少 —— `C:\\Recovery`、
    `C:\\ProgramData`、`C:\\OneDriveTemp`、`C:\\AMTAG.BIN`、`D:\\DumpStack.log`
    都带 Hidden 却不在名单里，于是混在排行里，和资源管理器对不上
    （设计约束「默认隐藏的系统项还是不全」正是这个）。

    名单保留作为兜底，覆盖属性读不到的两种情况：
      * NTFS 元文件（$MFT / $LogFile…）根本 stat 不到；
      * 某些清理工具会把条目的隐藏属性抹掉，但语义上仍是系统项。
    """
    # 名字比对前先去掉尾部的点与空白：MFT 扫描（WizTree）会把 NTFS 元文件
    # 报成 "$LogFile." 这类带尾点的名字（它不是 Win32 名，os.stat 也够不着），
    # 不归一化就匹配不上名单，结果元文件漏进排行。
    if str(name or "").rstrip(". ").lower() in _HIDDEN_SYS_NAMES:
        return True
    if attrs is None:
        if not path:
            return False
        try:
            attrs = os.stat(path, follow_symlinks=False).st_file_attributes
        except (OSError, AttributeError, ValueError):
            return False
    return bool(int(attrs) & _FILE_ATTR_HIDDEN)


# ======================================================================
# 自研 MFT 直读引擎（2026-10-31）：不装 WizTree 也能走 MFT 加速。
# 原理：以管理员权限打开卷句柄（\\.\D:），FSCTL_GET_NTFS_VOLUME_DATA 拿到
# $MFT 的起点与记录大小，顺 runlist 顺序读全部文件记录，解析每条记录的
# $FILE_NAME（父引用 + 名字）与 $DATA（真实体积）属性，目录由 $INDEX_ROOT
# 识别。产出与 _wiztree_parse **同形**的 table，直接复用 _wiztree_raw_nodes
# 的展开链路 —— 两条引擎路径的树形状完全一致。
# 约束与兜底：只读不写；需要管理员（卷 DASD 句柄）且卷是 NTFS；任何一步
# 失败都返回 None，由调用方静默回落 PowerShell 递归，绝不让扫描挂掉。
# 性能：纯 Python 解析百万级记录约数十秒，仍远快于 PS 逐目录递归（分钟级）。
# ======================================================================

























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


# ======================================================================
#  引擎实测速度缓存 + 用户引擎偏好（2026-11-03 定稿：缺省 WizTree 在先）
# ======================================================================
# 每个盘记录 MFT / WizTree 两引擎最近一次端到端耗时。选择逻辑分两层：
#   * 用户显式偏好 `_pref`（auto / wiztree / mft，前端下拉 + 端点可改）；
#   * auto 模式「谁快用谁」：两引擎都有实测值且 MFT 快 10% 以上才让
#     MFT 先跑，**缺省（无实测值）WizTree 在先** —— 设计约束「扫描还是
#     慢，改用默认 WizTree」；MFT 是无 WizTree 时的加速手段与自研储备。
# 耗时在扫描成功时顺带记录；非首选引擎的耗时由后台校准线程补测（每
# 14 天一次，两边对称），否则永远攒不到实测值。缓存落盘
# config/engine_speed.json 跨会话保留（"_pref" 键也存这里）。
_ENGINE_SPEEDS: dict = {}
_ENGINE_SPEED_FILE = CONFIG_DIR / "engine_speed.json"
_ENG_CALIB_LOCK = threading.Lock()
_ENG_PREFS = ("auto", "wiztree", "mft")


def _eng_speeds_load() -> None:
    global _ENGINE_SPEEDS
    try:
        _ENGINE_SPEEDS = json.loads(
            _ENGINE_SPEED_FILE.read_text(encoding="utf-8"))
    except Exception:
        _ENGINE_SPEEDS = {}
    # 跨设备优化：整文件夹拷到另一台电脑时，耗时实测值随文件一起过来，
    # 但那是旧机器的磁盘速度 —— 「自动」可能据此选中较慢的引擎，且要等
    # 14 天后台校准才纠正。落盘时记下计算机名（platform.node()，只存在
    # 本机不出网），发现与记录不一致（含旧版本文件没有 _host 字段）就
    # 清空各盘实测值（保留用户 _pref），让两引擎从缺省顺序重新开始并
    # 立即触发后台校准。同机重启则原样保留。
    try:
        host = platform.node()
    except Exception:
        host = ""
    if host and _ENGINE_SPEEDS.get("_host") != host:
        keep: dict = {}
        p = _ENGINE_SPEEDS.get("_pref")
        if p in _ENG_PREFS:
            keep["_pref"] = p
        keep["_host"] = host
        _ENGINE_SPEEDS = keep
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _ENGINE_SPEED_FILE.write_text(
                json.dumps(_ENGINE_SPEEDS, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass


def _eng_speed_record(letter: str, engine: str, secs: float) -> None:
    """记录一次实测耗时并落盘。异常静默（缓存坏了不影响扫描本身）。"""
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        return
    if not letter or engine not in ("mft", "wiztree") or secs <= 0:
        return
    m = _ENGINE_SPEEDS.setdefault(letter, {})
    m[engine] = round(secs, 2)
    m["at"] = time.time()
    _ENGINE_SPEEDS["_host"] = platform.node()
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _ENGINE_SPEED_FILE.write_text(
            json.dumps(_ENGINE_SPEEDS, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _eng_pref() -> str:
    """用户引擎偏好；缺省/非法值一律 auto（谁快用谁，缺省 WizTree 在先）。"""
    p = _ENGINE_SPEEDS.get("_pref")
    return p if p in _ENG_PREFS else "auto"


def _eng_set_pref(p: str) -> str:
    """保存用户引擎偏好并落盘；非法值抛 ValueError（路由层转 400）。"""
    p = str(p or "").strip().lower()
    if p not in _ENG_PREFS:
        raise ValueError(f"未知引擎偏好：{p!r}（可选 auto / wiztree / mft）")
    _ENGINE_SPEEDS["_pref"] = p
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _ENGINE_SPEED_FILE.write_text(
            json.dumps(_ENGINE_SPEEDS, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return p


def _eng_prefer_mft(letter: str) -> bool:
    """auto 模式的「谁快用谁」：两引擎都有近期实测且 MFT 明显更快
    （>10%，留滞后带防抖动）时返回 True；证据不足一律 False ——
    2026-11-03 起缺省 WizTree 在先，MFT 需要拿出实测优势才上位。"""
    m = _ENGINE_SPEEDS.get(letter) or {}
    mft_s, wiz_s = m.get("mft"), m.get("wiztree")
    if not mft_s or not wiz_s:
        return False
    try:
        return float(mft_s) < float(wiz_s) * 0.9
    except (TypeError, ValueError):
        return False


def _eng_calibrate_wiztree(letter: str) -> None:
    """后台补测 WizTree 端到端耗时（导出 + 解析）。每 14 天一次、
    互斥执行、绝不抛异常 —— 它只是给「谁快用谁」攒数据，失败了等下次。"""
    if not _wiz_ok():
        return
    m = _ENGINE_SPEEDS.get(letter) or {}
    if m.get("wiztree") and time.time() - (m.get("at") or 0) < 14 * 86400:
        return
    if not _ENG_CALIB_LOCK.acquire(blocking=False):
        return
    try:
        csv_path, _werr = _wiztree_export(letter)
        if not csv_path:
            return
        try:
            t0 = time.time()
            _wiztree_parse(csv_path)
            _eng_speed_record(letter, "wiztree", time.time() - t0)
        finally:
            _wiztree_cleanup(csv_path)
    except Exception:
        pass
    finally:
        _ENG_CALIB_LOCK.release()


def _eng_calibrate_mft(letter: str, root: str) -> None:
    """后台补测 MFT 端到端耗时（直读 + 构表）。WizTree 升为缺省首选后
    MFT 恒不主跑、攒不到实测值 —— 与 _eng_calibrate_wiztree 对称，
    管理员 + NTFS 才有意义，其余情况静默退出。"""
    if os.name != "nt" or not is_admin() or not _mft_volume_ntfs(root):
        return
    m = _ENGINE_SPEEDS.get(letter) or {}
    if m.get("mft") and time.time() - (m.get("at") or 0) < 14 * 86400:
        return
    if not _ENG_CALIB_LOCK.acquire(blocking=False):
        return
    try:
        t0 = time.time()
        tbl, info = _mft_build_table(letter, root, ext_stats=True)
        if tbl is not None:
            _eng_speed_record(letter, "mft",
                              (info or {}).get("secs") or (time.time() - t0))
    except Exception:
        pass
    finally:
        _ENG_CALIB_LOCK.release()





def _scan_overview_impl(drive: str, max_depth: int = DISK_TREE_MAX_DEPTH) -> dict:
    """磁盘概览：一次遍历生成整棵占用树，供界面逐层下钻。

    三条扫描路径，按适配度与**实测速度**自动选择（2026-11-01 起自研 MFT
    升为首选；2026-11-02 起进一步「谁快用谁」—— 两引擎都有实测耗时且
    WizTree 明显更快时先走 WizTree，失败仍回落 MFT）：

    1. **自研 MFT 直读**（管理员 + NTFS）：进程内直读 $MFT 产出全盘明细，
       无外部依赖、无临时文件，「任意目录逐层拆子项」是内存查表。
    2. **WizTree**（非管理员，或 MFT 不可用，或实测更快）：调用
       `WizTree64.exe <盘> /export=<csv>` 借它读 MFT，同形表。
    3. **PowerShell 递归**（兜底）：整棵树在单个 PS 进程内递归完成。

    三条路径用同一套展开参数与同一套节点形状，界面表现一致。
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
    r: dict = {}
    werr = ""

    # 引擎链：
    # 用户嫌 C 盘扫描慢 —— WizTree 由成熟引擎负责读盘，MFT 退居「没装
    # WizTree 时的自研加速 + 离线依赖兜底」。两层选择：
    #   * 用户显式偏好（/__oc__/engine/pref，auto / wiztree / mft）；
    #   * auto 模式谁快用谁：两引擎都有实测且 MFT 快 10% 以上才让 MFT 先，
    #     缺省（无实测值）WizTree 在先。
    mft_ok = IS_WINDOWS and is_admin() and _mft_volume_ntfs(root)
    wiz_ok = _wiz_ok()
    pref = _eng_pref()
    if pref == "wiztree":
        try_wiz_first = wiz_ok
    elif pref == "mft":
        try_wiz_first = False
    else:
        try_wiz_first = wiz_ok and (not mft_ok or not _eng_prefer_mft(letter))

    def _run_wiztree() -> bool:
        """WizTree 加速路径。成功置 engine="wiztree" 并返回 True。"""
        nonlocal engine, table, r, werr
        prog_set("export", f"正在用 WizTree 读取 {letter}: 盘的 MFT 明细", 10,
                 indeterminate=True)
        csv_path, werr = _wiztree_export(letter)
        if not csv_path:
            return False
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
                 "total": total, "by_ext": _ext_top(table.get("ext") or {})}
            engine = "wiztree"
            # 表留在内存里，供「逐层拆子项」与后续下钻直接查
            with _lock:
                _WIZ_DIRS[letter] = {"table": table, "at": time.time(),
                                     "parse_s": round(parsed, 2),
                                     "engine": "wiztree"}
                _save_scan_snapshot(letter)
            _eng_speed_record(letter, "wiztree", parsed)
            return True
        except Exception as e:
            table = None
            werr = f"WizTree 导出解析失败：{e}"
            return False
        finally:
            # 导出的 CSV 很大（C 盘约 120 MB，含 77 万行），
            # 表已经进内存了就必须删掉它 —— 否则一个「磁盘清理工具」
            # 每扫一次就往临时目录塞一百多 MB，自己变成垃圾制造者。
            _wiztree_cleanup(csv_path)

    def _run_mft() -> bool:
        """自研 MFT 直读路径。成功置 engine="mft" 并返回 True。"""
        nonlocal engine, table, r, werr
        mft_tbl, mft_info = _mft_build_table(
            letter, root, ext_stats=True,
            on_progress=lambda frac: prog_set(
                "mft", f"正在直读 {letter}: 盘的 MFT（MFT 引擎，"
                       f"{int(frac * 100)}%）", 8 + 62 * frac))
        if mft_tbl is None:
            werr = f"MFT 引擎不可用（{mft_info}），回退下一引擎"
            return False
        try:
            prog_set("tree", "正在按体积展开目录树", 72)
            roots, rootfiles, total = _wiztree_raw_nodes(
                root, mft_tbl, depth_cap,
                DISK_TREE_MIN_CHILD_MB * 1024 * 1024,
                DISK_TREE_MIN_DESCEND_RATIO, DISK_TREE_MIN_DESCEND_FLOOR,
                DISK_TREE_MAX_PER_DIR, DISK_TREE_MAX_NODES,
                DISK_TREE_MAX_DESCENTS)
            r = {"root": root.lower(), "nodes": roots, "rootfiles": rootfiles,
                 "total": total, "by_ext": _ext_top(mft_tbl.get("ext") or {})}
            engine = "mft"
            table = mft_tbl
            with _lock:
                _WIZ_DIRS[letter] = {"table": table, "at": time.time(),
                                     "parse_s": mft_info["secs"],
                                     "engine": "mft"}
            _eng_speed_record(letter, "mft", mft_info.get("secs") or 0)
            return True
        except Exception as e:
            table = None
            werr = f"MFT 引擎展开失败：{e}"
            return False

    if try_wiz_first:
        if not _run_wiztree() and mft_ok:
            _run_mft()
    elif mft_ok:
        if not _run_mft() and wiz_ok:
            _run_wiztree()
    elif wiz_ok:
        _run_wiztree()
    else:
        werr = "未安装 WizTree"
    # 加速路径失败不影响可用性，静默回退到 PowerShell；失败原因记在 payload 里

    if engine == "mft" and wiz_ok:
        # 后台补测 WizTree 耗时：MFT 主跑时 WizTree 永远量不到，没有实测
        # 值「谁快用谁」就无从谈起。校准线程自己判过期/互斥/兜异常。
        threading.Thread(target=_eng_calibrate_wiztree, args=(letter,),
                         daemon=True).start()
    if engine == "wiztree" and mft_ok:
        # 对称补测 MFT 耗时（2026-11-03）：WizTree 升为缺省首选后 MFT 恒
        # 不主跑 —— 不补测它就永远拿不到实测值，auto 模式无从比较。
        threading.Thread(target=_eng_calibrate_mft, args=(letter, root),
                         daemon=True).start()

    if engine not in ("wiztree", "mft"):
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
            # 默认隐藏的系统项标记：前端据此把它们收进「隐藏项」页签
            "hidden_sys": _is_hidden_sys(name, path),
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
            # 整目录永久排除（#36）：占用排行里仍显示（用户要看它占多少），
            # 但带标记 —— 前端据此显示「已排除」徽章并禁止勾选。
            if ov.get("excluded") is True:
                item["excluded"] = True
                item["cleanable"] = False
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
        wiz_dirs_tbl = wiz_tbl.get("dirs") \
            if engine in ("wiztree", "mft") else None
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
        wiz_dirs_tbl2 = wiz_tbl.get("dirs") \
            if engine in ("wiztree", "mft") else None
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
        if engine in ("wiztree", "mft") else None
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
                         else "MFT 直读加速" if engine == "mft"
                         else "PowerShell 递归（未安装 WizTree）"),
        "engine_note": (
            f"WizTree 导出 {table['rows']:,} 行，解析 "
            f"{_WIZ_DIRS.get(letter, {}).get('parse_s', '?')} 秒"
            if engine == "wiztree" else
            (f"MFT 直读 {table['rows']:,} 条记录，解析 "
             f"{_WIZ_DIRS.get(letter, {}).get('parse_s', '?')} 秒"
             if engine == "mft" else (werr or ""))),
        # 结构化的行数/秒数：前端英文界面用它拼本地化文案（engine_note 是中文）
        "engine_rows": (table["rows"] if engine in ("wiztree", "mft") else None),
        "engine_secs": (_WIZ_DIRS.get(letter, {}).get("parse_s")
                        if engine in ("wiztree", "mft") else None),
        # 扩展名分布（Top 12 + 其他）：仅 WizTree/MFT 两条加速路径有 ——
        # 都是把全量文件扫进内存表后顺手聚合的；PS 递归路径拿不到全量
        # 文件清单，前端按缺省隐藏该区块。
        "by_ext": (r.get("by_ext") or [] if engine in ("wiztree", "mft") else []),
        "subdir_ready": bool(engine in ("wiztree", "mft")),
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
    partial = False          # 回退路径量体积超时后置位（见下方 else 分支）
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
        engine = cached.get("engine") or "wiztree"
    else:
        engine = "powershell"
        # 无 MFT 加速时要现场递归量体积，一个 C:\Windows 就要 58 秒（实测）。
        # 用户看到的是「点了展开，界面卡住一分钟」，比数据不准更糟。
        # 所以给测量加时间预算：超时后剩下的子项标 size_unknown 并如实返回，
        # 前端显示「未测」，而不是让整个请求挂在那儿。
        _budget = time.time() + FALLBACK_SIZING_BUDGET_S
        partial = False
        try:
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            unknown = False
                            if time.time() < _budget:
                                sz = _size_of_dir(e.path)
                            else:
                                sz, unknown, partial = 0, True, True
                            children.append({"path": e.path, "name": e.name,
                                             "size": sz,
                                             "size_unknown": unknown,
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

    # 快照表里没有记录 ≠ 目录里真的没有东西（2026-10-21 修）：
    # WizTree 导出表只收 ≥DISK_TREE_MIN_CHILD_MB 的子项，一个「全是小文件」
    # 的目录在表里就是空的。以前这里直接把 total=0 返回给前端，界面显示
    # 「该层 0 个子项」，用户以为目录是空的 —— 实际里面可能躺着几百个文件。
    # 现在改为实地探查一次，如实说明「都低于快照门槛」，并把子项列出来
    # （体积现场测；条目太多时只测文件名，避免下钻卡住）。
    below_threshold = False
    if cached and cached.get("table") and not children and total == 0:
        try:
            with os.scandir(p) as it:
                found = []
                for e in it:
                    try:
                        found.append(e)
                    except OSError:
                        continue
            if found:
                below_threshold = True
                total = len(found)
                # 条目不多时值得逐个量体积；多了就只给名字与文件大小，
                # 否则一次下钻要走上万次 stat，体感比空白更糟。
                do_size = len(found) <= 60
                for e in found[:limit]:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                        st = e.stat()
                        sz = (_size_of_dir(e.path) if (is_dir and do_size)
                              else 0) if is_dir else st.st_size
                        children.append({
                            "path": e.path, "name": e.name, "size": sz,
                            "modified": datetime.fromtimestamp(
                                st.st_mtime).strftime("%Y-%m-%d"),
                            "type": "dir" if is_dir else "file",
                            "size_unknown": bool(is_dir and not do_size),
                            # 属性顺手带出来，省一次 stat（下面标 hidden_sys 用）
                            "attrs": getattr(st, "st_file_attributes", None),
                        })
                    except OSError:
                        continue
                children.sort(key=lambda x: -x["size"])
        except OSError:
            pass

    for c in children:
        c["size_h"] = human(c["size"])
        # 与占用树同一套标记：下钻视图里前端据此把隐藏项收进「隐藏项」页签
        c["hidden_sys"] = _is_hidden_sys(c.get("name") or "", c.get("path") or "",
                                         c.pop("attrs", None))
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
        # 该层子项全部低于快照门槛（WizTree 表里没记录），是现场探查补出来的。
        # 前端据此提示「本层子项都很小，未计入快照」，而不是显示「0 个子项」。
        "below_threshold": below_threshold,
        "min_child_mb": DISK_TREE_MIN_CHILD_MB,
        # 回退路径（无 MFT 加速）量体积超时：剩下的子项体积未知，
        # 前端要如实标注「未测」，不能当成 0 处理。
        "partial": partial,
        "note": ("无 MFT 加速：本层体积为现场测量，超时部分标为未测"
                 if engine != "wiztree" else ""),
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
# 这正是设计约束「D/E 盘老是冒出包管理器/浏览器/临时文件」的根因：
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

# WizTree 探测结果（None = 还没探测过）
# 最近一次 WizTree 导出的目录体积表：盘符 → {"dirs": {path:size}, "at": 时间戳}
# 有了它，「点开大项看二级目录」就只是内存查表，不必再扫盘。
# 表一直保留到同盘符的下一次扫描覆盖 —— 下钻靠它，过期驱逐反而会让
# 「隔几分钟再点开子项」退化成现场扫盘，不做 TTL。
_WIZ_DIRS: dict[str, dict] = {}






def load_targets() -> dict:
    return _load_json(TARGETS_PATH, {"presets": [], "targets": []})


def load_drives_cfg() -> dict:
    return _load_json(DRIVES_PATH, {"app": {}, "profiles": {}})


def load_convert() -> dict:
    return _load_json(CONVERT_PATH, {"display": {}, "units": []})


def load_filetypes() -> dict:
    return _load_json(FILETYPES_PATH, {"folder_rules": [], "file_rules": []})




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
        # 整目录永久排除（#36）：true 时该项从清理清单里消失（占用排行
        # 里仍可见但带「已排除」徽章），不被「全选可清理」收进去。
        if isinstance(v.get("excluded"), bool):
            entry["excluded"] = v["excluded"]
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
        "fs_groups": load_fs_groups(),
    }


def _import_bundle(body: dict) -> dict:
    """自定义数据包导入的落盘逻辑（原 /bundle/import handler 内联逻辑原样
    抽出，供路由与云同步共用）：逐项校验并**合并**进本地文件 —— 已有同名
    单位/规则更新、其余新增，绝不先清空再写入，返回各项计数与跳过原因。
    kind 不是 ocleaner-custom 时抛 ValueError（路由转 400）。"""
    if str((body or {}).get("kind") or "") != "ocleaner-custom":
        raise ValueError("这不是 O-Cleaner 自定义数据包"
                         "（kind 应为 ocleaner-custom）")
    rep = {"units_added": 0, "units_updated": 0, "units_skipped": [],
           "overrides_applied": 0, "overrides_skipped": [],
           "rules_added": 0, "rules_updated": 0, "rules_skipped": [],
           "groups_added": 0, "groups_updated": 0}

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

    # 4) 自定义聚合组（可选字段，兼容旧版导出的包）
    if isinstance(body.get("fs_groups"), list):
        groups = load_fs_groups()
        by_gname = {str(g.get("name") or ""): g for g in groups if isinstance(g, dict)}
        for g0 in body.get("fs_groups") or []:
            if not isinstance(g0, dict):
                continue
            gname = str(g0.get("name") or "").strip()[:60]
            gpaths = [os.path.normpath(str(x).strip()).lower()
                      for x in (g0.get("paths") or []) if str(x).strip()]
            if not gname or not gpaths:
                continue
            if gname in by_gname:
                merged = list(dict.fromkeys((by_gname[gname].get("paths") or []) + gpaths))
                by_gname[gname]["paths"] = merged[:200]
                rep["groups_updated"] += 1
            else:
                groups.append({
                    "id": str(g0.get("id") or f"grp-{int(time.time() * 1000) % 10 ** 10}"),
                    "name": gname,
                    "drive": str(g0.get("drive") or "").rstrip(":").upper(),
                    "paths": list(dict.fromkeys(gpaths))[:200],
                })
                rep["groups_added"] += 1
        save_fs_groups(groups)
    return rep


# ======================================================================
#  云端同步
# ======================================================================
# 自定义数据（换算单位 / 释义覆盖 / 固定项规则，与手工导出包同一份数据）
# 存到用户**自己**的 GitHub 私密 Gist：个人访问令牌（需 gist 权限）只存
# 本机 config/sync.json，不进仓库、不经过任何第三方服务器。
# 「双向同步」= 拉取合并（远端按 bundle/import 的逐项校验并进本地，
# 不覆盖本地已有项）→ 把合并后的全集推回远端，两端收敛为并集；
# 删除不传播（v1 语义，界面有说明）。Gist 首次推送自动创建并记住 id。


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




RECEIPT_DIR = RUNTIME_DIR / "receipts"
# 改名暂存的中断恢复日志：rm_tree_strict 改名前后登记，启动时据此还原
PENDING_DELETES_PATH = RUNTIME_DIR / "pending-deletes.json"
# 清理历史与回执档案已迁出至 ocleaner/receipts.py


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




# ---- 内容特征判定：删除决策不能只看目录名，还要看目录里装的是什么 ----
# 名称规则（cache/temp/quark/download…）只是猜测，目录内容才是证据。
# 祖先路径里一个 "quark" 就能把装着完整 Python 运行时与模型的程序目录
# 判成「云盘数据目录 · 可清理」，因此凡是名称规则判出「可清理」的目录，
# 都要过一遍内容特征；命中程序本体特征就强制改判不可清理（宁可少删，也不误删）。






def _browser_evidence(names: set, core: set, aux: set) -> bool:
    """一组目录名是否构成「这是浏览器资料目录」的证据。

    单个核心特征就够（Local Storage / IndexedDB 基本只在浏览器里出现）；
    辅助特征要凑够两个 —— 单独一个 extensions 或 history 到处都是。
    """
    if names & core:
        return True
    return len(names & aux) >= 2


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
# 判定不依赖目录名单：与浏览器同名的目录可能装着完全无关的数据，
# 统一走 _looks_like_browser_profile()，必须在目录内部真看到
# Cache / Code Cache 这类缓存子目录才认。

# 「受保护域」里的缓存豁免名单（2026-10-21 护栏升级）。
# 域规则保护的是**整目录删除**；域内有些目录装的是纯派生数据，删了自动重建，
# 那正是清理工具该干的活 —— 把它们一并锁死等于让新产品什么都不敢删。
# 这一组比 ALLOWED_CONTAINER_NAMES 更具体：浏览器代码缓存、显卡着色器缓存、
# 系统网络缓存、缩略图缓存等。名单里的名字都必须是「删了会自动重建」的派生数据，
# 往里加名字前先确认这一点。

# 盘根按 glob 清零散文件（v2.9）：这是「盘根一律不许删」的唯一例外。
# 「%DRIVE% 盘未完成下载分片」这类目标只删盘根**顶层**匹配 *.part 的文件，
# 不递归、不删目录、不动子项 —— 但它此前被「必须是绝对路径」直接拦死
# （`%DRIVE%` 展开成 `C:` 是盘符相对路径），功能一直是坏的。
# 开口的条件必须窄到「只能清一类扩展名」，所以：
#   * 只接受 `*.ext` 形式（单个点 + 扩展名），拒绝 `*`、`*.*`、`**`、`*.part*`；
#   * 只对 `kind="files"` 生效（删除循环天然只处理顶层匹配文件，不递归）
#   * 只对「路径就是盘根」生效，任何子目录仍走 check_deletable 的常规保护链。
_DRIVE_ROOT_GLOB_RE = re.compile(r"^\*\.[A-Za-z0-9][A-Za-z0-9_.\-]{0,15}$")


def drive_root_glob_ok(glob: str) -> bool:
    """盘根清文件时，glob 是否窄到只剩一类扩展名（`*.part` 这种）。"""
    return bool(_DRIVE_ROOT_GLOB_RE.match(str(glob or "").strip()))


def _is_drive_root_path(path: str) -> bool:
    """path 是否正好是某个盘的根（`C:\\`），不是盘符相对路径 `C:`。

    刻意不走 abspath：`abspath("C:")` 返回的是「C 盘上的当前工作目录」，
    与盘根无关，拿它判断会把不是盘根的路径误认成盘根。
    """
    return bool(re.fullmatch(r"[A-Za-z]:[\\/]", str(path or "").strip()))




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
    r = _py_dir_kind(path)   # P2-4：进程内单层清单，免 PS 冷启动
    if r.get("error"):
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
        # P2-4：进程内测量（免临时清单文件 + PS 冷启动）。临时清单文件
        # 方案曾在带安全删除钩子的宿主环境里耗尽删除配额、钩子抛
        # SystemExit 打断请求（实测复现过）—— 现已无任何落盘。
        sizes = {}
        for _p in need_size[:80]:
            sizes[os.path.normpath(_p).lower()] = _py_tree_size(_p)
        for x in cleanable + keep:
            if x["type"] == "dir":
                x["size"] = sizes.get(os.path.normpath(x["path"]).lower(), 0)
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



def _ts(v) -> str:
    try:
        n = int(v or 0)
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d") if n > 0 else ""
    except (ValueError, OSError, OverflowError):
        return ""


# Steam 根目录缓存：discover_steam() 成功时更新，/__oc__/game-cover 用它
# 定位 librarycache。放模块级而非请求内变量 —— 封面请求不需要重跑 PS。
_STEAM_ROOTS_CACHE: list[str] = []
# /steam 响应级 60s 缓存（多标签共享；fresh=1 绕过）。只存一种语言的
# 载荷：lang 变了就重算 —— 语言切换是低频操作，不值得按语言存多份。
_STEAM_RESP_CACHE: dict = {"at": 0.0, "data": None, "lang": ""}



# ======================================================================
#  Epic / GOG 游戏发现（v2.2 新增）
# ======================================================================









# ---- Epic / GOG 的残留目录（孤儿）----
# 与 Steam 孤儿同一信任级别：只看「安装根」的**直接子目录**，且此刻的平台
# 记录里没有它。安装根 = 平台自己记录的安装目录的父目录 ∪ 默认安装根。
# 启动器自己的目录（如 Epic Games Launcher）会被显式排除。










# ======================================================================
#  自定义游戏库（游戏占用概览 → 「自定义游戏库」面板）
# ======================================================================
# 用户手动添加的「游戏库或单个游戏目录」：只做只读展示（量体积、列顶层
# 子项），绝不参与清理判定 —— 这里的路径是用户自己给的，安全护栏（盘根、
# 受保护域等）都不对它做假设，所以它只许看、不许删。















# ======================================================================
#  安全校验与删除
# ======================================================================





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








# ---- 改名暂存的中断恢复 ----






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



def recycle_restore(paths: list) -> dict:
    """回收站一键原位还原（v2.12「可逆与精算版」核心项）。

    解析 $Recycle.Bin 的 $I 元数据按原始路径精确匹配，把 $R 数据原子
    移回原位并删除 $I 元数据。原位被占用/已存在时明确报错（不覆盖）。
    """
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


SNAPSHOTS_DIR = RUNTIME_DIR / "snapshots"

# ---- 分析中心域已迁出（v2.17 Task 1） ----
# _save_scan_snapshot / snapshot_diff / fs_search 已迁至 ocleaner/analysis.py
# （经顶部 import 再绑定保持裸名兼容）；SNAPSHOTS_DIR 依赖 RUNTIME_DIR
# （--data-dir 重定向），留在本模块，迁出侧经 `_core.` 惰性引用。

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
        # kind=files 的目标只删自己这一层的匹配文件，**不覆盖任何子目录**，
        # 因此它不能当「外层容器」。反例（2026-10-21 实测）：盘根分片目标的
        # path 是 "C:\"，归一化后 "c:" 成了所有 C 盘路径的前缀，于是它被当成
        # 包含全部 C 盘勾选项的外层，按「只清理最小项」被静默跳过 —— 用户勾了
        # 盘根分片 + 任意其他 C 盘项，分片永远不会被清，历史里还写着「已包含
        # 更小的勾选项」。它是内层没关系（外层 tree/回收站确实覆盖它）。
        if t.get("kind") == "files":
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
                 drive: str | None = None, trash: bool = False,
                 allow_user_data: bool = False) -> dict:
    """删除一个目标的文件。only_paths 非空时只删这些子项。

    allow_user_data：用户显式勾选了「个人数据目录」类目标时为 True，
    透传给 check_deletable。默认 False —— 定时清理、规则清理、预设清理
    都不会传它，所以这些路径只可能由用户在界面上亲手选中。

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

    ok, why = check_deletable(path, kind, allow_user_data)
    if not ok:
        # 盘根清零散文件（v2.9）：唯一一个绕过「盘根一律不删」的入口，条件写死
        # 在这里而不是散到 check_deletable —— 后者不知道 glob，一旦让它开口，
        # 任何调用方（含第三方资源包）都能用 `*` 清盘根。
        # 这里的三重收窄：只对 kind="files"（删除循环只处理顶层匹配文件、不递归
        # 不删目录）、路径必须正好是盘根、glob 必须窄到 `*.ext`。
        if (kind == "files" and _is_drive_root_path(path)
                and drive_root_glob_ok(t.get("glob"))):
            ok, why = True, f"盘根顶层按 {t.get('glob')} 清零散文件"
            result["drive_root_files"] = True
        else:
            result["error"] = why
            return result
    if allow_user_data and why:
        # 用户自选的个人数据目录：结果里如实带上标记，历史记录也能看出来
        result["user_data_optin"] = True
        result["note"] = why
        # 护栏升级（2026-10-21）：显式选择「个人数据 / 程序数据」这类整目录
        # 删除时，**一律强制移入回收站**。「可选」这一档的全部意义就是可后悔 ——
        # 用户即便关掉了「高风险项进回收站」，这里也不放行直删：
        # 一次误点的代价是整个目录不可恢复，而回收站的代价只是暂时不释放空间。
        if kind != "recyclebin" and not use_trash:
            use_trash = True
            result["note"] = why + "；已强制移入回收站（可从回收站还原）"

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
            if _is_reparse_dir(item):
                # 外部审计 P1-2：嵌套 Junction / 目录链接 —— 只摘挂载点
                # 本身（os.rmdir 对重解析点不跟随目标），sz 记 0。
                try:
                    os.rmdir(item)
                    result["deleted"] += 1
                except OSError as exc:
                    result["failed"] += 1
                    if len(result["skipped"]) < 20:
                        result["skipped"].append(
                            f"{os.path.basename(item)}"
                            f"（链接移除失败：{exc.strerror or exc}）")
            elif os.path.isdir(item) and not os.path.islink(item):
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


_PROTECTED_PROCESS_NAMES = {
    "system", "idle", "system idle process", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "svchost.exe", "dwm.exe", "explorer.exe", "conhost.exe",
    "audiodg.exe", "fontdrvhost.exe", "sihost.exe", "taskmgr.exe",
    "runtimebroker.exe", "searchindexer.exe",
}


def _pid_process_name(pid: int) -> str:
    """用 tasklist 读取指定 PID 的进程映像名（失败回空串）。"""
    if not IS_WINDOWS or pid <= 4:
        return ""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=CREATE_NO_WINDOW,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if line.startswith('"'):
                return line.split('"', 2)[1].strip()
    except Exception:
        pass
    return ""


def stop_lock_processes(pids: list) -> dict:
    """按用户明确勾选的 PID 列表安全停止非系统关键进程。"""
    if not IS_WINDOWS:
        return {"ok": False, "error": "进程停止仅支持 Windows",
                "stopped": [], "skipped": [], "failed": []}
    self_pid = os.getpid()
    parent_pid = os.getppid()
    stopped: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    seen: set[int] = set()
    for raw in _as_list(pids)[:20]:
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
        if pname.lower() in _PROTECTED_PROCESS_NAMES:
            skipped.append({"pid": pid, "name": pname, "reason": "受保护的系统关键进程禁止结束"})
            continue
        try:
            r = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, timeout=10,
                creationflags=CREATE_NO_WINDOW,
            )
            if r.returncode == 0:
                stopped.append({"pid": pid, "name": pname or str(pid)})
            else:
                err = (r.stderr or r.stdout or "").strip() or f"exit {r.returncode}"
                failed.append({"pid": pid, "name": pname or str(pid), "error": err})
        except Exception as exc:  # noqa: BLE001
            failed.append({"pid": pid, "name": pname or str(pid), "error": str(exc)})
    return {"ok": True, "stopped": stopped, "skipped": skipped, "failed": failed}

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
    # 整目录永久排除（#36）：被排除的目标直接不进清理清单 —— 自动发现
    # 每次扫描都会重新提出这些目录，不在这里拦就等于没排除。
    # 键两种形态都认：base_id 键（普通目标）与归一化路径键（树行右键）。
    if any((v or {}).get("excluded") for v in user_ov.values()):
        def _is_excluded(t: dict) -> bool:
            if (user_ov.get(_override_key(t)) or {}).get("excluded"):
                return True
            low = os.path.normpath(t.get("path", "")).lower()
            return (user_ov.get(low) or {}).get("excluded") is True
        out = [t for t in out if not _is_excluded(t)]
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

    # 近 30 天按天聚合（#33 趋势页）：日期轴均匀补零 —— 没清理的日子也是
    # 柱状图的一部分（全空柱=这段时间没跑，比「柱子突然挤在一起」诚实）。
    by_day: dict[str, dict] = {}
    today = datetime.now().date()
    day_keys = [(today - timedelta(days=29 - i)).isoformat() for i in range(30)]
    for k in day_keys:
        by_day[k] = {"day": k, "freed": 0, "runs": 0}
    for h in hist:
        day = str(h.get("time") or "")[:10]
        slot = by_day.get(day)
        if slot is None:
            continue                      # 30 天窗口外的旧记录不上图
        slot["freed"] += int(h.get("freed") or 0)
        slot["runs"] += 1
    days = [{"day": k, "freed": v["freed"], "runs": v["runs"],
             "freed_h": human(v["freed"])} for k, v in by_day.items()]

    return {
        "run_count": run_count,
        "total_freed": total_freed, "total_freed_h": human(total_freed),
        "total_failed": total_failed,
        "first_time": first_ts, "last_time": last_ts,
        "by_day": days,
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
#  定时清理域已迁出至 ocleaner/scheduler.py
# ======================================================================


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


def _is_unsafe_remote(u: str) -> str:
    """检查要抓取的远端地址是否属于不该访问的目标，返回拒绝原因（空=放行）。

    威胁模型：这是本机单用户工具，且写操作有同源校验，能被利用的前提是
    用户自己点了「从 URL 导入」并粘贴了一个恶意地址。所以这里不做
    「禁掉所有内网」那么激进（用户完全可能从局域网文件服务器导资源包），
    只挡三类：协议滥用、带凭据的 URL、以及云元数据/本机回环这类
    「读到就是凭据泄露」的目标。
    """
    try:
        from urllib.parse import urlsplit
        sp = urlsplit(u)
    except Exception:
        return "地址无法解析"
    if sp.scheme.lower() not in ("http", "https"):
        return "只支持 http/https 地址"
    if sp.username or sp.password:
        return "地址里不能带账号密码"
    host = (sp.hostname or "").strip().lower()
    if not host:
        return "地址缺少主机名"
    blocked_hosts = {"metadata.google.internal", "metadata", "instance-data"}
    if host in blocked_hosts:
        return "拒绝访问云元数据地址"
    # 回环与「未指定」地址：本机工具没有理由通过 HTTP 抓自己
    if host in ("localhost", "localhost.localdomain", "::1", "0.0.0.0", "[::1]"):
        return "拒绝访问本机回环地址"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_unspecified or ip.is_link_local:
            return "拒绝访问回环/链路本地地址"
    except ValueError:
        pass          # 不是 IP 字面量，是域名，放行
    return ""


def _authenticode_status(path: str) -> str:
    """返回 exe 的 Authenticode 签名校验状态（Valid / NotSigned / …）。

    外部审计 P1-3：WizTree 便携包下载解压后会以当前进程权限（往往是
    管理员）直接执行 —— 此前只查 zip 头与体积，被篡改的包会被照跑。
    官方发行版带 Antibody Software 的有效签名，签名不对即拒绝执行。
    """
    try:
        out = run_ps("(Get-AuthenticodeSignature -LiteralPath "
                     + json.dumps(str(path)) + ").Status.ToString()")
        return str(out or "").strip().strip('"') or "Unknown"
    except Exception:
        return "Unknown"


def fetch_json(url: str, timeout: int = 20):
    """取一个 JSON。协议与目标限制见 _is_unsafe_remote（入口强制校验）。"""
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        return None, "只支持 http/https 地址"
    # 外部审计 P1-3：docstring 声称有目标限制但此前并未实现 ——
    # /packs/update 允许请求体写入 remote 后直接 fetch，构成 SSRF 面。
    # 这里统一收口：回环/链路本地/云元数据一律拒绝。
    unsafe = _is_unsafe_remote(u)
    if unsafe:
        return None, f"拒绝访问该地址：{unsafe}"
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
    # v3.0 信任分级：index v2 支持对象条目 {"file", "sha256"}——下载后先做
    # 哈希核验，不符即拒绝导入（防传输损坏与清单被篡改后的静默导入）。
    # 字符串条目保持 v1 兼容（无校验）。
    expected: dict[str, str] = {}
    for n in names:
        if isinstance(n, dict) and n.get("file"):
            fname0 = os.path.basename(str(n["file"]))
            if str(n.get("sha256") or ""):
                expected[fname0] = str(n["sha256"]).strip().lower()
            names[names.index(n)] = fname0
    done, failed = [], []
    for n in names:
        fname = os.path.basename(str(n))
        if not fname.lower().endswith(".json"):
            continue
        data, e2 = fetch_json(f"{remote}/{fname}")
        if e2:
            failed.append(f"{fname}：{e2}")
            continue
        if fname in expected:
            digest = hashlib.sha256(
                json.dumps(data, ensure_ascii=False, sort_keys=True)
                .encode("utf-8")).hexdigest()
            # 兼容远端格式化差异：再试原始语义哈希（对 key 排序的紧凑序列化）
            if digest != expected[fname]:
                failed.append(f"{fname}：SHA-256 与清单不符，已拒绝导入"
                              "（远端包可能被篡改或清单过期）")
                continue
        ok, why = import_pack(data)
        (done if ok else failed).append(f"{fname}：{why}" if ok else f"{fname}：{why}")
    return {"ok": bool(done), "remote": remote, "updated": done,
            "failed": failed, "packs": load_packs(use_cache=False)["packs"]}

# ======================================================================
#  分析中心域已迁出（v2.17 Task 1）
# ======================================================================
# 大文件排行（big_files）与重复检测哈希原语（_sample_hash / _full_sha256）
# 已迁至 ocleaner/analysis.py；下方四个签名库 / 搬家记录路径常量依赖
# CONFIG_DIR（--data-dir 重定向），留在本模块，迁出侧经 `_core.` 引用。

BLOATWARE_BUILTIN_PATH = CONFIG_DIR / "bloatware-builtin.json"
BLOATWARE_CFG_PATH = CONFIG_DIR / "bloatware.json"
APPCLEAN_CFG_PATH = CONFIG_DIR / "appclean.json"
MOVED_JUNCTIONS_PATH = CONFIG_DIR / "moved-junctions.json"

# ---- 毒瘤清除 / Junction 搬家 / 应用专清 / 重复检测 / 下载建议已迁出 ----
# （v2.17 Task 1 -> ocleaner/analysis.py，经顶部 import 再绑定；
#   下方单位换算与历史统计留在本模块。）

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
            # 预设目标本身就是「本工具判定可清理」的那批（界面上带勾选框的
            # 就是它们），所以缺省为真。此前这里直接读 t.get("cleanable")，
            # 而 26 项内置目标都没写这个字段 → 接口对全部目标回传
            # cleanable:false，与产品行为自相矛盾。用户覆盖里显式写 false
            # 的仍然生效（key 存在就不取默认值）。
            "cleanable": bool(t.get("cleanable", True)),
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

# ======================================================================
#  HTTP 路由层已迁出（v2.17 Task 5）
# ======================================================================
# class Handler(BaseHTTPRequestHandler)（/__oc__/* 全部路由与入口守卫）
# 已迁至 ocleaner/server.py，经顶部 import 再绑定保持 `app.Handler`
# 兼容；路由内对业务函数经 `_core.` 惰性引用。下方 pick_port / 托盘 /
# 自动清理 / main() 仍属本模块。

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


def _calc_app_window_rect() -> tuple[int, int, int, int]:
    """按当前桌面工作区计算居中的横向矩形默认窗口 (x, y, w, h)（逻辑像素）。"""
    left, top, work_w, work_h = 0, 0, 1440, 900
    try:
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            ww = rect.right - rect.left
            wh = rect.bottom - rect.top
            if ww >= 640 and wh >= 480:
                scale = 1.0
                try:
                    dpi = ctypes.windll.user32.GetDpiForSystem()
                    if dpi and dpi > 96:
                        scale = dpi / 96.0
                except Exception:
                    pass
                left = int(rect.left / scale)
                top = int(rect.top / scale)
                work_w = int(ww / scale)
                work_h = int(wh / scale)
    except Exception:
        pass
    w = max(min(work_w - 40, 1180), min(int(work_w * 0.80), 1480))
    # 横向矩形（约 16:9.5 比例），避免接近正方形或过高
    h = min(work_h - 48, max(660, int(w * 0.58)))
    x = left + max(0, (work_w - w) // 2)
    y = top + max(0, (work_h - h) // 2)
    return x, y, w, h


def _open_app_window(url: str) -> None:
    """Edge --app 无边框窗口（v3.0 原生体验方向）：零依赖实现桌面化外观。

    使用独立 app-profile 目录确保 --window-size / --window-position
    不被已运行的浏览器进程忽略，按横向矩形居中启动；找不到回退默认浏览器。
    """
    x, y, w, h = _calc_app_window_rect()
    prof_dir = str(RUNTIME_DIR / "app-profile")
    try:
        os.makedirs(prof_dir, exist_ok=True)
    except OSError:
        prof_dir = ""
    candidates = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    ]
    for exe in candidates:
        if os.path.isfile(exe):
            try:
                cmd = [exe, "--new-window", f"--app={url}",
                       f"--window-size={w},{h}", f"--window-position={x},{y}"]
                if prof_dir:
                    cmd.append(f"--user-data-dir={prof_dir}")
                subprocess.Popen(cmd, creationflags=CREATE_NO_WINDOW)
                return
            except OSError:
                continue
    webbrowser.open(url)   # 兜底：默认浏览器


ARCHIVE_LOG_PATH = RUNTIME_DIR / "downloads-archive-log.json"

# ---- 下载目录归档已迁出（v2.17 Task 1） ----
# downloads_archive / downloads_archive_undo 已迁至 ocleaner/analysis.py；
# ARCHIVE_LOG_PATH 依赖 RUNTIME_DIR 且被 unit_test monkeypatch，留在本模块。

def _tray_worker(url: str) -> None:
    """系统托盘常驻（v3.0 方向七后半，纯 ctypes 零依赖）。

    仅 --tray 时启动：左键双击打开界面；右键菜单＝打开界面 /
    立即安全清理（后台线程跑 run_auto_clean）/ 退出（连服务一起停）。
    任何步骤失败都静默退出托盘 —— 服务照常运行，只是没有图标。
    """
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        # 64 位句柄/参数必须显式声明 argtypes，否则回调里转发会 OverflowError
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_longlong

        WM_USER_TRAY = 0x0400 + 1          # 托盘回调消息
        IDM_OPEN, IDM_CLEAN, IDM_QUIT = 1001, 1002, 1003
        nid_cb = 0

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT),
                        ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT),
                        ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128),
                        ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD),
                        ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT),
                        ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD),
                        ("guidItem", ctypes.c_ubyte * 16),
                        ("hBalloonIcon", wintypes.HICON)]

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_longlong, wintypes.HWND, wintypes.UINT,
            ctypes.c_size_t, ctypes.c_longlong)

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_USER_TRAY:
                if lparam == 0x0203:                       # 左键双击打开（避免单击/双击重复弹多页）
                    webbrowser.open(url)
                elif lparam == 0x0205:                     # 右键抬起
                    user32.SetForegroundWindow(hwnd)
                    hmenu = user32.CreatePopupMenu()
                    user32.AppendMenuW(hmenu, 0, IDM_OPEN, "打开界面")
                    user32.AppendMenuW(hmenu, 0, IDM_CLEAN, "立即安全清理")
                    user32.AppendMenuW(hmenu, 0x800, 0, None)  # 分隔线
                    user32.AppendMenuW(hmenu, 0, IDM_QUIT, "退出")
                    pos = wintypes.POINT()
                    user32.GetCursorPos(ctypes.byref(pos))
                    user32.TrackPopupMenu(
                        hmenu, 0x0080, pos.x, pos.y, 0, hwnd, None)  # TPM_RIGHTBUTTON|RETURNCMD 关闭
                    user32.DestroyMenu(hmenu)
            elif msg == 0x0111:                            # WM_COMMAND
                if wparam & 0xFFFF == IDM_OPEN:
                    webbrowser.open(url)
                elif wparam & 0xFFFF == IDM_CLEAN:
                    import threading as _t
                    _t.Thread(target=run_auto_clean, args=("C",),
                              daemon=True).start()
                    nid = NOTIFYICONDATAW(
                        cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=hwnd,
                        uID=nid_cb, uFlags=0x10, szInfo="安全清理已在后台开始"
                        "（只清建议清理项，高风险入回收站）",
                        szInfoTitle="O-Cleaner")
                    shell32.Shell_NotifyIconW(1, ctypes.byref(nid))  # NIM_MODIFY
                elif wparam & 0xFFFF == IDM_QUIT:
                    user32.PostQuitMessage(0)
            elif msg == 0x0010:                            # WM_CLOSE
                user32.PostQuitMessage(0)
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        wndproc = WNDPROC(_wndproc)
        hinst = kernel32.GetModuleHandleW(None)
        wc = ctypes.c_void_p()
        # WNDCLASSW 手工布局：style/cbClsExtra/cbWndExtra/hInstance/hIcon/
        # hCursor/hbrBackground/lpszMenuName/lpszClassName/hCursor 处的 lpfnWndProc
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT),
                        ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]
        wc = WNDCLASSW(style=0, lpfnWndProc=wndproc, hInstance=hinst,
                       lpszClassName="OCleanerTray")
        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            return
        hwnd = user32.CreateWindowExW(
            0, "OCleanerTray", "O-Cleaner", 0, 0, 0, 0, 0, None, None,
            hinst, None)
        if not hwnd:
            return
        hicon = user32.LoadIconW(None, 32512)   # IDI_APPLICATION
        nid = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW),
                              hWnd=hwnd, uID=1, uFlags=0x01 | 0x02 | 0x04,
                              uCallbackMessage=WM_USER_TRAY, hIcon=hicon,
                              szTip="O-Cleaner —— 双击打开 / 右键菜单")
        nid_cb = 1
        if not shell32.Shell_NotifyIconW(0, ctypes.byref(nid)):  # NIM_ADD
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        shell32.Shell_NotifyIconW(2, ctypes.byref(nid))          # NIM_DELETE
        # 托盘退出＝整个应用退出（托盘是 --tray 模式的唯一生命周期入口）
        os._exit(0)
    except Exception:  # noqa: BLE001
        pass   # 托盘失败不致命：服务照常，用户可用浏览器访问


def run_auto_clean(drive: str) -> dict:
    """无头单次清理（v2.15 CLI Automation，配任务计划程序用）。

    「safe 预设」的定义即：只清 cleanable=True 的目标（知识库判定
    「建议清理」的才入选，建议保留/未识别一律不碰），高风险项一律入
    回收站（可恢复），全程记历史与回执。输出 JSON 摘要供上层调度。
    """
    drive = (drive or "C").rstrip(":").upper()
    targets = [t for t in build_targets(drive)
               if t.get("cleanable") is not False]
    targets, _merged = dedup_nested_targets(targets)
    results = []
    for t in targets:
        try:
            results.append(clean_target(t, None, drive, trash=True))
        except Exception as exc:  # noqa: BLE001
            results.append(_error_result(t, drive, exc))
    freed = sum(r.get("freed", 0) or 0 for r in results
                if isinstance(r, dict))
    failed = sum(r.get("failed", 0) or 0 for r in results
                 if isinstance(r, dict))
    try:
        append_history({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "drive": drive,
            "trigger": f"自动安全清理（{drive}:）",
            "results": results, "freed": freed, "failed": failed,
            "targets": len(targets), "freed_h": human(freed),
        })
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    return {"drive": drive, "targets": len(targets),
            "freed": freed, "freed_h": human(freed), "results": results}


def main() -> None:
    _dpapi_migrate_plaintext()   # 批次 A3：明文凭据惰性迁移（加密回写）
    if IS_WINDOWS:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    # 清掉上次被强杀时遗留的 WizTree 导出文件（正常路径在 finally 里已删）
    stale = cleanup_stale_wiztree_exports()
    if stale:
        print(f"已清理 {stale} 个上次遗留的 WizTree 导出文件")

    # 上次删除被中断（强杀 / 安全钩子 / 断电）时，目标目录停在改名暂存态：
    # 用户看到的是「我的文件夹不见了，多了个 xxx.oc-deleting-12345678」。
    # 启动时按日志把还没删完的改回原名，把内容交还给用户。
    try:
        for item in recover_pending_deletes():
            if item.get("restored"):
                print(f"已恢复上次中断的删除：{item['restored']}")
            elif item.get("failed"):
                print(f"未能恢复 {item['failed']}：{item.get('error')}")
    except Exception:
        traceback.print_exc()

    ap = argparse.ArgumentParser(description="O-Cleaner 磁盘清理助手")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--version", action="store_true",
                    help="打印版本号后退出（排障/打包核对用，版本单点定义在 APP_VERSION）")
    ap.add_argument("--drive", default=None, help="命令行扫描时指定盘符，如 D")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--tray", action="store_true",
                    help="系统托盘常驻模式（左键开界面 / 右键菜单：立即安全"
                         "清理 / 退出）；配 pythonw.exe 可完全无窗口")
    ap.add_argument("--app", action="store_true",
                    help="用 Edge 无边框应用窗口打开界面（v3.0 原生桌面体验，"
                         "配 pythonw.exe 可无黑窗常驻）")
    ap.add_argument("--scan-only", action="store_true")
    ap.add_argument("--fullscan-only", action="store_true",
                    help="命令行扫描磁盘占用树并打印，不开服务")
    ap.add_argument("--auto-clean", action="store_true",
                    help="无头单次清理（safe 预设：只清 cleanable 目标，"
                         "高风险入回收站），打印 JSON 摘要后退出；"
                         "配合 --drive 与 Windows 任务计划程序使用")
    ap.add_argument("--depth", type=int, default=0,
                    help="配合 --fullscan-only 指定展开层数（默认 5）")
    ap.add_argument("--data-dir", default=None, metavar="PATH",
                    help="数据存档目录：config/ 与历史记录、定时任务等运行数据"
                         "的存放位置（也可用环境变量 OC_DATA_DIR；默认跟程序同目录，"
                         "整个文件夹拷走即完成数据迁移）")
    ap.add_argument("--wait-free", type=int, default=0, metavar="SECONDS",
                    help="启动前等指定端口空闲（一键提权重启时老实例让位用，"
                         "配合 --port 固定端口）。等不到就照常顺延端口。")
    ap.add_argument("--lint", action="store_true",
                    help="只跑规则 linter（发布/贡献规则前用），不开服务")
    args = ap.parse_args()
    if getattr(args, "auto_clean", False):
        print(json.dumps(run_auto_clean(args.drive or "C"), ensure_ascii=False,
                         indent=2))
        return

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

    preferred = args.port or int(dcfg.get("app", {}).get("port", 8770))
    # 一键提权重启：老实例延迟退出让位，新实例在这里等端口真正空出来
    # （最多 --wait-free 秒；等不到就照常顺延，不让启动卡死）。
    if args.wait_free > 0:
        deadline = time.time() + max(1, args.wait_free)
        while time.time() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", preferred)) != 0:
                    break                    # 连不上 = 空闲
            time.sleep(0.3)
        else:
            print(f"  等待端口 {preferred} 空闲超时，改用顺延端口。")
    port = pick_port(preferred)
    global _BOUND_PORT
    _BOUND_PORT = port
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.busy = False  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()

    url = f"http://127.0.0.1:{port}/"
    print()
    print(f"  O-Cleaner 已启动（v{APP_VERSION}）")
    print(f"  界面地址: {url}")
    print(f"  数据目录: {DATA_DIR}"
          + ("（--data-dir 重定向）" if DATA_DIR != BASE_DIR else ""))
    print(f"  配置文件: {CONFIG_DIR}")
    print(f"  管理员权限: {'是' if is_admin() else '否（系统目录的项目会提示权限不足）'}")
    print("  关闭此窗口即停止服务。")
    print()
    if getattr(args, "tray", False):
        # 托盘模式：HTTP 服务放后台线程，主线程跑托盘消息循环；
        # 托盘「退出」会 os._exit 结束整个进程。
        threading.Timer(0.8, _tray_worker, args=(url,)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    if not args.no_browser:
        if getattr(args, "app", False):
            threading.Timer(0.8, lambda: _open_app_window(url)).start()
        else:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止。")
    finally:
        httpd.server_close()


