# -*- coding: utf-8 -*-
"""删除护栏域（P2-1 域拆分收官，从 core.py 迁出）。

check_deletable 九层护栏链（受保护目录/个人数据域/盘根/保留设备名/
重解析点/已安装程序特征/聚合组/覆盖规则…）与其签名库常量、
_guarded_zone 分区判定、已安装程序与浏览器档案识别、覆盖规则存取。

本模块是被 core.py「再绑定」的迁出域：core 顶部
`from ocleaner.guard import ...` 保持全部既有调用点的裸名兼容；
selftest / guard_matrix 的 50 项护栏矩阵断言直接落到本模块。
对 core 与其他域的依赖（identify / games 安装目录 / _long_path /
BASE_DIR）经 `_core.` 惰性引用 —— monkeypatch 对这里可见。
"""
import os
import re

import ocleaner.core as _core

PROTECTED_DIR_NAMES = {
    "windows", "system32", "syswow64", "winsxs", "installer", "fonts",
    "assembly", "servicing", "program files", "program files (x86)",
    "programdata", "recovery", "system volume information", "$recycle.bin",
    "users", "default", "public", "appdata", "boot", "perflogs", "msocache",
    "onedrive", "documents and settings",
    # 护栏升级（2026-10-21）：共享组件与商店应用本体。
    #   common files —— 多个程序共用的 DLL / 运行时，删掉会一片程序都起不来；
    #   windowsapps —— 商店应用安装目录，删掉等于把所有 UWP 应用连数据一起废掉。
    "common files", "windowsapps",
}

USER_DATA_DIR_NAMES = {
    "documents", "desktop", "downloads", "pictures", "videos", "music",
    "favorites", "links", "contacts", "searches", "saved games",
    "my documents", "my pictures", "my music", "my videos", "3d objects",
}

PROTECTED_AT_DRIVE_ROOT = {"windows", "program files", "program files (x86)",
                           "programdata", "users", "recovery", "perflogs"}

ALLOWED_CONTAINER_NAMES = {"temp", "tmp", "cache", "caches", ".cache",
                           "logs", "log", "crashdumps", "minidump", "prefetch"}

RESERVED_DEVICE_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$"} \
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}

_ZONE_LABEL = {
    "user": "个人数据目录",
    "appdata": "程序数据目录",
    "programdata": "全机共享的程序数据目录",
    "programfiles": "已安装程序目录",
}

def _guarded_zone(parts: list[str]) -> str:
    """判断路径是否落在「受保护域」内，返回域标签（空串表示不受域保护）。

    受保护域 = 系统盘上这几棵子树：
      * ``Users\\<用户>\\<一级目录>``      —— 这个人的个人数据
      * ``Users\\<用户>\\AppData\\<L>``     —— 这个人的程序配置（L = Local/Roaming/LocalLow）
      * ``ProgramData\\<X>``               —— 全机共享的程序数据
      * ``Program Files[ (x86)]\\<X>``      —— 已安装程序本体

    **为什么需要「域」而不是逐条名单**：名字规则只比对末级段名，保护住
    ``Documents`` 本身，却放行了它下面的 ``Documents\\Adobe`` —— 而用户在
    占用排行里下钻之后勾选的，恰恰是后者。上次真实事故（改名暂存了
    ``Documents\\Adobe``）走的就是这条缝。位置规则同理，只管 ``Users\\<u>\\``
    一级。所以保护必须**按子树继承**，而不是只保护树根。

    域内的缓存/临时/日志容器不继承保护（见 check_deletable 里的豁免），
    那正是清理工具该干的活。
    """
    if not parts:
        return ""
    low = [p.lower() for p in parts]
    head = low[0]
    if head == "users" and len(parts) >= 3:
        second = low[2]
        if second in ALLOWED_CONTAINER_NAMES:
            return ""
        if second in {"appdata", "local settings"}:
            # AppData 本身由名字名单拦死；这里管它下面那一层
            if len(parts) < 4:
                return ""
            if low[3] in ALLOWED_CONTAINER_NAMES:
                return ""
            return "appdata"
        return "user"
    if head == "programdata" and len(parts) >= 2:
        return "" if low[1] in ALLOWED_CONTAINER_NAMES else "programdata"
    if head in ("program files", "program files (x86)") and len(parts) >= 2:
        return "" if low[1] in ALLOWED_CONTAINER_NAMES else "programfiles"
    return ""

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
    # 核心特征：只有浏览器资料目录才会有的结构
    core = {"user data", "local storage", "indexeddb", "session storage",
            "web data", "login data"}
    # 辅助特征：浏览器确实有，但**别的软件也常用**，所以单个不算数。
    # 实测反例：`E:\Chrome` 其实是个 AI 工具合集，里面 `sd-webui\extensions`
    # 命中了 extensions 就被判成浏览器资料目录；开发项目里有 history /
    # preferences 也很常见。因此辅助特征必须凑够两个才作数。
    aux = {"service worker", "extensions", "bookmarks", "preferences", "history"}
    # 弱特征：通用缓存目录名，必须与上面的证据同时出现才算数
    weak = {"cache", "code cache", "gpucache", "shadercache", "blob_storage",
            "default", "profile", "profiles"}
    try:
        names = {e.name.lower() for e in os.scandir(path)}
    except OSError:
        return False
    if _core._browser_evidence(names, core, aux):
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
            if _core._browser_evidence(sub_names, core, aux):
                return True
        except OSError:
            continue
    return False

BROWSER_CACHE_DIRS = {
    "cache", "code cache", "gpucache", "shadercache", "grshadercache",
    "dawngcache", "dawngraphitecache", "component crx cache",
    "media cache", "application cache", "service worker",
    "cachestorage", "scriptcache", "jscache", "blob_storage",
}
ZONE_CACHE_EXEMPT = ALLOWED_CONTAINER_NAMES | BROWSER_CACHE_DIRS | {
    "inetcache", "dxcache", "glcache", "d3dscache", "explorer",
    "deliveryoptimization", "wer", "tempstate", "webcache",
}

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
    if _core._in_game_install(path):
        return True
    try:
        info = _core.identify(os.path.basename(path.rstrip("\\/")) or "", path, "dir")
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

def check_deletable(path: str, kind: str = "tree",
                    allow_user_data: bool = False) -> tuple[bool, str]:
    """判断某个清理目标是否允许操作。返回 (是否允许, 拒绝原因)。

    注意：kind="files" 时 path 是**要扫描的目录**（不是要删的文件），
    真正的删除对象是它内部匹配 glob 的文件，所以同样按目录校验。

    allow_user_data：**仅在用户显式勾选了该目标时为 True**。
    它只放宽「受保护域」里的整目录删除 —— 个人数据（文档/桌面/下载/图片…）、
    程序数据（AppData 各分支、ProgramData、Program Files 下的子目录）。
    系统目录（Windows / System32 / Program Files 本体…）、工具自身、盘根、
    保留设备名、junction 穿透这些硬保护一律不受它影响 —— 那些删了系统就
    坏了，不该由用户的一次误点决定。

    默认 False。预设、自动发现、定时清理、规则清理**永远**用默认值，
    所以「不做删」这一侧由代码结构保证，而不是靠调用方自觉。
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
    # 8.3 短名展开要在归一化之前做：C:\PROGRA~1\... 的段名是 PROGRA~1，
    # 不展开就绕过了整张名单（Windows 每个目录都有这种短名别名）。
    raw = _core._long_path(raw)
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

    # 保留设备名（2026-10-21 护栏升级）：CON / NUL / COM1 这类名字在 Win32
    # 层是设备而不是文件。路径里出现它们时，os.remove / rmtree 打到的是设备
    # 句柄 —— 表现为无提示失败，极端情况下会写坏设备流。直接拒绝。
    for p in parts:
        if p.split(".")[0].strip().lower() in RESERVED_DEVICE_NAMES:
            return False, f"拒绝：{p} 是 Windows 保留设备名"

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

    # 受保护域（2026-10-21 护栏升级）：整目录删除默认拒绝，需显式勾选。
    # 上面两条规则只比对**末级段名 / Users 一级位置**，保护住的是树根；
    # 用户在实际界面上勾的是下钻之后的子目录（Documents\Adobe 这种），
    # 那正是上次真实事故走的缝。这里按**子树继承**兜住。
    # 豁免：本身是缓存/临时/日志容器，或是某个容器目录的直接子项 ——
    # 这类东西删了会自动重建，正是清理工具该干的活。
    zone = _guarded_zone(parts)
    if zone:
        # 缓存豁免（ZONE_CACHE_EXEMPT）：路径中任何一段是已知缓存目录名，就按
        # 「派生数据区」处理，不锁。为什么看整条链而不只看末级/父级：
        # `%LOCALAPPDATA%\Temp\<某个包>\node_modules` 这种目标，末级和父级都
        # 不是缓存名，但它明明就在 Temp 里 —— 只看一层会把这类正常清理项误拦
        # （CI 首次跑全量时就是这么红的）。
        _ex = ZONE_CACHE_EXEMPT
        _is_cache = any(p.lower() in _ex for p in parts)
        if not _is_cache:
            if allow_user_data:
                return True, f"用户已明确选择：{_ZONE_LABEL[zone]}"
            return False, (f"拒绝：{parts[-1]} 位于{_ZONE_LABEL[zone]}内，"
                           "整目录删除默认不做"
                           "（可在界面上显式勾选后清理，将移入回收站）")

    # 用户数据目录（2026-10-21）：默认拒绝，用户显式勾选时放行。
    # 「不做删，做成可选」—— 工具不主动推荐、不进预设、不进定时清理，
    # 但只要用户在界面上明确选了它，就按用户的意愿执行。
    if leaf in USER_DATA_DIR_NAMES and leaf not in ALLOWED_CONTAINER_NAMES:
        if allow_user_data:
            return True, "用户已明确选择：个人数据目录"
        return False, (f"拒绝：{parts[-1]} 属于个人数据目录，默认不清理"
                       "（可在界面上显式勾选后清理）")

    # 位置保护（2026-10-21）：C:\Users\<用户名>\ 的直接子目录默认不许整体删除。
    # 光靠上面的名字名单挡不干净 —— 中文系统的「文档 / 桌面 / 下载」在磁盘上
    # 可能被 OneDrive 重定向成中文目录名，用户也可能自建「工作资料」之类的
    # 一级目录，名单永远列不全。按**位置**兜底：一个用户 Home 下的一级目录
    # 基本都是这个人的个人数据。同样遵循「默认拦、显式可选」。
    # 只放行明确的缓存/临时容器，它们删掉会自动重建。
    if len(parts) == 3 and parts[0].lower() == "users" \
            and leaf not in ALLOWED_CONTAINER_NAMES \
            and leaf not in {"appdata", "local settings"}:
        if allow_user_data:
            return True, "用户已明确选择：个人数据目录"
        return False, (f"拒绝：{parts[1]}\\{parts[2]} 是用户 Home 下的一级目录，"
                       f"属于个人数据，默认不清理（可在界面上显式勾选后清理）")

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
        _home_low = os.path.expanduser("~").lower().rstrip("\\")
        for i in range(len(parts) - 1):
            if parts[i].lower() in _safe_ancestors:
                continue
            anc_path = os.path.join(drive_root, *parts[:i + 1])
            # 用户 Home 永远不参与「已安装程序」判定：Home 下含年份的
            # 目录太常见（数据库备份、照片 2024……两个就能凑成误判），
            # 而 Home 本质是各种东西的混杂地，不可能是单一程序安装目录。
            if anc_path.lower().rstrip("\\") == _home_low:
                continue
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
        base_low = str(_core.BASE_DIR).lower()
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
        # allow_user_data 要透传，否则「用户显式选了一个普通目录、但它是
        # 指向自己 Documents 的符号链接」会被重新判死；
        # 反过来 junction 指向 C:\Windows 时，递归后仍然命中系统目录规则。
        return check_deletable(real, kind, allow_user_data)

    # 不可读时不在这里判定拒绝，交由上层报「需要管理员权限」，
    # 这样 C:\\Windows\\Temp 能给出可读提示而不是硬性拒绝
    return True, ""

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
