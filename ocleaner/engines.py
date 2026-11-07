# -*- coding: utf-8 -*-
"""扫描引擎域（P2-1 域拆分收官，从 core.py 迁出）。

两个引擎：内置 NTFS MFT 直读（_mft_*，无需管理员与第三方工具，
秒级建全盘体积表）与 WizTree 外部引擎（探测/获取/导出解析，缺省在先
按实测择快）。`_WIZ_DIRS`/`_WIZ_CACHE` 扫描缓存与全部 WIZTREE_*/MFT_
常量随域迁移。

对 core 的反向依赖（DISK_TREE 阈值 / ext 工具 / 提权与 Authenticode /
_human 等）经 `_core.` 惰性引用；`_WIZ_DIRS` 留在 core（扫描编排、
dupfiles、快照钩子都要读写它），引擎内经 `_core.` 引用同一字典对象。
"""
import os
import re
import time
import ctypes
import struct
import array
import tempfile
import subprocess

import ocleaner.core as _core

WIZTREE_NAMES = ("WizTree64.exe", "WizTree.exe")

WIZTREE_HINTS = (
    r"C:\Program Files\WizTree",
    r"C:\Program Files (x86)\WizTree",
)

WIZTREE_PORTABLE_URL = "https://diskanalyzer.com/files/wiztree_4_33_portable.zip"

WIZTREE_SETUP_URL = "https://diskanalyzer.com/files/wiztree_4_33_setup.exe"

WIZTREE_HOME = "https://diskanalyzer.com/download"

WIZTREE_DIST_LICENSE = "https://diskanalyzer.com/distribution-license"

WIZTREE_LICENSE_NOTE = (
    "WizTree 是 Antibody Software Limited 的第三方软件，个人使用免费。"
    "本项目不随包分发它 —— 下载与解压都发生在你自己这台机器上。"
    "若要把 WizTree 随你的产品一起分发，须先向官方购买 Distribution License。"
)


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
    if not _core.IS_WINDOWS:
        return None
    # 1) 本目录便携版优先：可移动性的关键
    for n in WIZTREE_NAMES:
        p = str(_core.WIZTREE_PORTABLE_DIR / n)
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
        os.path.normpath(str(_core.WIZTREE_PORTABLE_DIR)).lower())
    ver = ""
    if path:
        try:
            out = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command",
                 f"(Get-Item -LiteralPath '{path.replace(chr(39), chr(39)*2)}')"
                 ".VersionInfo.FileVersion"],
                capture_output=True, timeout=20,
                creationflags=_core.CREATE_NO_WINDOW if _core.IS_WINDOWS else 0)
            ver = (out.stdout or b"").decode("utf-8", "replace").strip()
        except Exception:
            ver = ""
    return {
        "found": bool(path),
        "path": path,
        "portable": portable,               # 本目录自带 → 拷走就能用
        "version": ver,
        "enabled": _wiz_ok(),
        "portable_dir": str(_core.WIZTREE_PORTABLE_DIR),
        "portable_rel": "portable\\WizTree",
        "download_url": WIZTREE_PORTABLE_URL,
        "setup_url": WIZTREE_SETUP_URL,
        "homepage": WIZTREE_HOME,
        "dist_license_url": WIZTREE_DIST_LICENSE,
        "license_note": WIZTREE_LICENSE_NOTE,
        "cached_drives": sorted(_core._WIZ_DIRS.keys()),
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
              "portable_dir": str(_core.WIZTREE_PORTABLE_DIR), "log": []}

    # 已经有了就直接返回，不重复下载
    if wiztree_available().get("found"):
        st = wiztree_status()
        result.update(ok=True, path=st["path"], version=st["version"],
                      already=True)
        result["log"].append("已经可用，无需下载")
        return result

    try:
        _core.WIZTREE_PORTABLE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result["error"] = f"无法创建目录 {_core.WIZTREE_PORTABLE_DIR}：{e}"
        return result

    result["log"].append(f"从官方地址下载便携版：{WIZTREE_PORTABLE_URL}")
    try:
        req = _core._UrlRequest(
            WIZTREE_PORTABLE_URL, headers={"User-Agent": _core.APP_UA})
        with _core._urlopen(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("Content-Type") or "")
            raw = resp.read()
    except Exception as e:
        result["error"] = (f"下载失败：{type(e).__name__}: {e}。"
                           f"可手动下载 {WIZTREE_PORTABLE_URL} 后把内容解压到 "
                           f"{_core.WIZTREE_PORTABLE_DIR}")
        return result

    result["log"].append(f"下载完成 {_core.human(len(raw))}")
    # 基本校验：必须是 zip（PK 头）且体积合理，避免把错误页面当成压缩包解开
    if not raw.startswith(b"PK\x03\x04"):
        result["error"] = (f"下载到的不是 zip（Content-Type={ctype}，"
                           f"前 4 字节={raw[:4]!r}），已中止且未写入任何文件")
        return result
    if len(raw) < 1_000_000:
        result["error"] = f"下载体积异常偏小（{_core.human(len(raw))}），已中止"
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
            zf.extractall(str(_core.WIZTREE_PORTABLE_DIR))
        result["log"].append(f"解压 {len(names)} 个文件到 "
                             f"{_core.WIZTREE_PORTABLE_DIR}")
    except Exception as e:
        result["error"] = f"解压失败：{type(e).__name__}: {e}"
        return result

    # 解压后重新探测（find_wiztree 会优先看这个便携目录）
    global _WIZ_CACHE
    _WIZ_CACHE = None
    st = wiztree_status()
    if not st["found"]:
        result["error"] = (f"解压完成但没找到 WizTree64.exe，"
                           f"请检查 {_core.WIZTREE_PORTABLE_DIR} 的内容")
        return result
    sig = _core._authenticode_status(st["path"])
    if sig != "Valid":
        result["error"] = (f"解压出的 WizTree64.exe 数字签名校验未通过"
                           f"（Authenticode 状态：{sig}），为防运行被篡改的"
                           f"二进制已中止。请从官方地址重新下载后重试")
        return result
    result["log"].append("WizTree64.exe 签名校验通过（Authenticode: Valid）")
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
                           creationflags=_core.CREATE_NO_WINDOW if _core.IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        return None, f"WizTree 导出超时（{timeout} 秒）"
    except OSError as e:
        return None, f"WizTree 启动失败：{e}"
    if not os.path.isfile(csv_path):
        err = (p.stderr or b"").decode("utf-8", "replace").strip()[:200]
        return None, f"WizTree 未生成导出文件（exit={p.returncode}）{err}"
    return csv_path, ""

def _wiztree_children(dirs: dict[str, int], path: str, limit: int = 60) -> list[dict]:
    """从目录体积表里取出某个目录的**直接**子目录（按体积降序）。

    判「直接子目录」用路径里的反斜杠个数，比 endswith/startswith 组合可靠：
    只比较层数就不会把 D:\\a\\bc 误当成 D:\\a\\b 的子项。
    """
    key, pre, depth = _core._wiztree_direct_prefix(path)
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

def _wiztree_parse(csv_path: str, on_progress=None) -> dict:
    """解析 WizTree CSV，返回 {"dirs": {路径:体积}, "files": {父目录:[(名,大小)]}, "rows": n}。

    为什么要连「直接文件」也存下来：原来那套展开逻辑每层会同时列出**子目录与大文件**
    （`hasChildren` 与文件条目混在同一层），只看目录会让用户觉得「文件不见了」。
    WizTree 的 CSV 正好把两者都给全了，顺手存下来即可完全复刻原行为。

    只保留达到体积门槛的条目，避免把 C 盘 77 万行全部留在内存里。
    **盘根例外**：盘根的直接子目录与直接文件一律保留（数量只有几十个），
    否则 `AMTAG.BIN`、`DumpStack.log.tmp` 这类几 KB 的默认隐藏项连门都进不来。

    另外顺手聚合**全量文件**的扩展名分布（`ext`: {后缀: [字节, 个数]}）——
    「这盘的空间都被什么类型吃了」是概览页最直观的问题之一，CSV 每行
    反正要过一遍，聚合只多一次后缀切分；门槛裁剪在这里不适用（小文件
    也是类型分布的一部分）。

    on_progress(frac) 是可选回调，按读到的字节数报进度（0~1）。
    这一步在 C 盘上会读上百 MB，是整条链路里最长的纯 CPU 阶段，
    不报进度的话界面只能一直显示「解析中」。
    """
    dirs: dict[str, int] = {}
    files: dict[str, list] = {}
    exts: dict[str, list] = {}
    floor = max(0, int(_core.DISK_TREE_MIN_CHILD_MB)) * 1024 * 1024
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
            else:
                # 扩展名分布统计**全量文件**（含被门槛裁掉的），见 docstring
                _core._ext_bump(exts, path, size)
                if size >= floor or low.count("\\") <= 1:
                    # 盘根下的文件同样无条件保留：门槛只是省内存的手段，而盘根
                    # 一共就几十个文件，留下几个 KB 的完全无所谓。不这么做的话，
                    # AMTAG.BIN / DumpStack.log.tmp 这类**很小但默认隐藏**的文件
                    # 在解析阶段就被丢掉，下游 _core._is_hidden_sys 的属性判定根本
                    # 拿不到它们 —— 「隐藏项」页签因此比资源管理器少几项。
                    # 深层文件仍按门槛裁剪，内存不受影响。
                    parent = os.path.dirname(low)
                    files.setdefault(parent, []).append(
                        (path.rstrip("\\").split("\\")[-1], size, path))
    if on_progress:
        try:
            on_progress(1.0)
        except Exception:
            pass
    return {"dirs": dirs, "files": files, "rows": n, "ext": exts}

_MFT_FSCTL_GET_NTFS_VOLUME_DATA = 0x00090064

def _mft_runlist(buf: bytes, off: int) -> list[tuple[int, int]]:
    """解析非驻留属性的 runlist，返回 [(起始 LCN, 簇数), ...]。"""
    runs: list[tuple[int, int]] = []
    lcn = 0
    n = len(buf)
    while off < n and buf[off] != 0:
        header = buf[off]
        off += 1
        len_len = header & 0x0F
        off_len = (header >> 4) & 0x0F
        if not len_len or off + len_len > n:
            break
        clusters = int.from_bytes(buf[off:off + len_len], "little")
        off += len_len
        if off_len and off + off_len > n:
            break
        delta = int.from_bytes(buf[off:off + off_len], "little", signed=True) \
            if off_len else 0
        off += off_len
        lcn += delta
        if clusters and lcn > 0:
            runs.append((lcn, clusters))
    return runs

def _mft_parse_at(buf: bytearray, off: int, frs_size: int,
                  with_mtime: bool = False):
    """解析 buf[off:off+frs_size] 处的单条 MFT 文件记录（原地 fixup）。

    返回 (parent_frn, name_raw, real_size, is_dir, mtime)；记录无效/损坏
    返回 None。name_raw 是 $FILE_NAME 的 UTF-16LE 原始字节（None 表示无
    可用名字）——**解码推迟给调用方**：记录循环里九成以上是达不到门槛的
    小文件，树构建不需要它们的名字（profile 实测 UTF-16 解码占解析时长
    约两成）；扩展名统计可从原始字节里廉价抽取（UTF-16LE 的 ASCII 区
    b".\\x00" 定界）。mtime 仅 with_mtime=True 时解析（$STANDARD_
    INFORMATION 修改时间，FILETIME 整数），目录树路径不付这笔开销。
    """
    end = off + frs_size
    if off + 0x40 > end or buf[off:off + 4] != b"FILE":
        return None
    usa_off = buf[off + 4] | (buf[off + 5] << 8)
    usa_cnt = buf[off + 6] | (buf[off + 7] << 8)
    # 更新序列修正：每 512 字节扇区末 2 字节被检查值占用，须换回真实数据，
    # 否则跨扇区边界的属性字段会读到脏字节（体积算错、名字乱码）。
    if 0 < usa_off and usa_cnt > 1 and usa_off + usa_cnt * 2 <= frs_size:
        for i in range(1, usa_cnt):
            o = off + i * 512 - 2
            if o + 2 > end:
                return None
            buf[o] = buf[off + usa_off + i * 2]
            buf[o + 1] = buf[off + usa_off + i * 2 + 1]
    parent = -1
    name_raw = None
    size = 0
    is_dir = False
    mtime = 0
    # 热点路径全用 struct.unpack_from 直接内联（up 局部别名省一次属性查
    # 找）：记录解析每盘跑百万次，切片 + int.from_bytes 的每次小拷贝和
    # lambda 间接调用都是主要 CPU 成本。type+len 合并为一次 u64 读，
    # 每条属性省一次 unpack_from 调用（基准合计：内联版比切片版快约六成）。
    up = struct.unpack_from
    used = up("<I", buf, off + 0x18)[0] or frs_size
    a = off + up("<H", buf, off + 0x14)[0]
    # 行走边界用「记录已用大小」：最后一条属性之后到记录尾部是零填充，
    # 读到 a_len==0 就当属性区结束（不能当损坏 —— 真实 MFT 大多如此）。
    while off + 0x10 <= a <= end - 16 and a - off < used:
        tl = up("<Q", buf, a)[0]
        a_type = tl & 0xFFFFFFFF
        a_len = tl >> 32
        if a_type == 0xFFFFFFFF:
            break
        if a_len == 0:
            break
        if a_len < 24 or a + a_len > end or a_len % 8:
            return None                      # 属性越界/未对齐：记录视为损坏
        nonres = buf[a + 8]
        if a_type == 0x30 and nonres == 0:   # $FILE_NAME（驻留）
            if name_raw is None:
                c = a + up("<H", buf, a + 0x14)[0]
                if 0x42 <= c - off <= frs_size - 0x42:
                    parent = up("<Q", buf, c)[0] & 0xFFFFFFFFFFFF
                    nl = buf[c + 0x40]
                    ns = buf[c + 0x41]
                    if ns != 2 and 0 < nl <= 255:   # ns==2 是 DOS 8.3 短名
                        name_raw = buf[c + 0x42:c + 0x42 + nl * 2]
        elif a_type == 0x80:                 # $DATA
            if nonres:
                if not size:
                    size = up("<Q", buf, a + 0x38)[0]
            else:
                size = up("<I", buf, a + 0x10)[0]
            # 主数据流已到且父已定 → 提前收工。NTFS 属性按类型码排序
            # （0x10 < 0x30 < 0x80），$DATA 之后只可能是 DOS 短名/ADS 之类
            # 解析用不上的尾巴 —— 真实记录（STD_INFO + Win32 名 + DOS 名 +
            # DATA）每条省 2/5 的属性行走。$DATA 前置于 $FILE_NAME 的异常
            # 记录 parent 仍为 -1，不 break，按旧路径走全。
            if parent >= 0:
                break
        elif a_type == 0x90:                 # $INDEX_ROOT → 目录
            is_dir = True
        elif a_type == 0x10 and with_mtime and not mtime and nonres == 0:
            c = a + up("<H", buf, a + 0x14)[0]
            if off + 0x18 <= c <= end - 0x18:  # 修改时间在 STD_INFO 内容 +8
                mtime = up("<Q", buf, c + 8)[0]
        a += a_len
    if parent < 0:
        return None
    return parent, name_raw, size, is_dir, mtime

def _mft_parse_record(rec: bytearray, frs_size: int):
    """兼容入口：解析独立缓冲里的单条记录（selftest 与外部调用）。"""
    return _mft_parse_at(rec, 0, frs_size, False)

def _mft_name_decode(raw) -> str:
    """$FILE_NAME 原始字节 → 字符串（惰性解码的唯一入口）。"""
    return raw.decode("utf-16-le", "replace") if raw else ""

_MFT_K32_READY = False

def _mft_k32():
    """kernel32 句柄函数签名只配一次 —— 64 位下 HANDLE 不设 argtypes/restype
    会被截成 32 位 c_int，句柄静默损坏（这类 bug 控制台里毫无征兆）。"""
    global _MFT_K32_READY
    k32 = ctypes.windll.kernel32
    if not _MFT_K32_READY:
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
        k32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.c_void_p]
        k32.SetFilePointerEx.argtypes = [
            ctypes.c_void_p, ctypes.c_longlong,
            ctypes.POINTER(ctypes.c_longlong), ctypes.c_ulong]
        k32.ReadFile.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CloseHandle.restype = ctypes.c_ulong
        _MFT_K32_READY = True
    return k32

def _mft_open_volume(letter: str):
    """打开卷 DASD 句柄（只读）。非管理员/失败返回 None。"""
    k32 = _mft_k32()
    GENERIC_READ = 0x80000000
    h = k32.CreateFileW("\\\\.\\{}:".format(letter), GENERIC_READ, 3, None, 3, 0, None)
    if not h:
        return None
    return h

def _mft_volume_ntfs(root: str) -> bool:
    """卷文件系统是否 NTFS（MFT 引擎只对 NTFS 有意义）。"""
    buf = ctypes.create_unicode_buffer(32)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None,
                                                      None, None, buf, 32)
    return bool(ok) and buf.value.upper().startswith("NTFS")

def _mft_read_at(h, offset: int, size: int, buf) -> int:
    """从卷句柄绝对偏移读 size 字节进 buf（buf 须 ≥ size），返回实读字节数。"""
    k32 = _mft_k32()
    moved = ctypes.c_longlong(0)
    if not k32.SetFilePointerEx(h, ctypes.c_longlong(offset),
                                ctypes.byref(moved), 0):
        return 0
    got = ctypes.c_ulong(0)
    view = (ctypes.c_char * size).from_buffer(buf)
    if not k32.ReadFile(h, view, size, ctypes.byref(got), None):
        return 0
    return got.value

def _mft_build_table(letter: str, root: str, on_progress=None,
                     file_floor_mb: int | None = None,
                     ext_stats: bool = False, with_mtime: bool = False):
    """直读 $MFT 产出与 _wiztree_parse 同形的 table。

    返回 (table|None, info)；info 为 {"secs", "records"} 或失败原因字符串。
    内存策略：frn = 记录号（MFT 天然稠密），parent/size 走稠密数组
    （每条 8 字节），名字只给目录与达标文件留 —— 百万级记录也稳在几十 MB。
    file_floor_mb：文件名字保留门槛（MB）。默认与目录体积门槛一致
    （_core.DISK_TREE_MIN_CHILD_MB）；大文件排行调用时传更低的值 —— 排行要
    10 MB 级的文件，目录树只关心 ≥50 MB 的（见 big_files 的 MFT 路径）。
    ext_stats：聚合全量文件扩展名分布（table["ext"]，与 _wiztree_parse
    同形）——「空间被什么类型吃了」这类概览问题直接查表。
    with_mtime：解析 $STANDARD_INFORMATION 修改时间（仅达标文件，
    table["mtimes"] = {完整路径: 'YYYY-MM-DD HH:MM'}，供大文件排行按
    时间过滤）。两条开关都会增加少量每记录开销，目录树路径不付。
    """
    if os.name != "nt" or not _core.is_admin():
        return None, "需要管理员权限"
    if not _mft_volume_ntfs(root):
        return None, "卷不是 NTFS"
    k32 = _mft_k32()
    h = _mft_open_volume(letter)
    if not h:
        return None, "无法打开卷句柄（需要管理员权限）"
    try:
        # NTFS_VOLUME_DATA_BUFFER 实际 96 字节（5 个 LARGE_INTEGER 计数
        # + 4 个 DWORD + 5 个 LARGE_INTEGER）。曾经给 80 字节导致
        # DeviceIoControl 直接失败（提权探针实测），字段偏移也一并修正：
        #   40 BytesPerSector · 44 BytesPerCluster · 48 BytesPerFileRecordSegment
        #   52 ClustersPerFileRecordSegment · 56 MftValidDataLength · 64 MftStartLcn
        vdb = ctypes.create_string_buffer(96)
        br = ctypes.c_ulong(0)
        if not k32.DeviceIoControl(h, _MFT_FSCTL_GET_NTFS_VOLUME_DATA, None, 0,
                                   vdb, 96, ctypes.byref(br), None):
            return None, "FSCTL_GET_NTFS_VOLUME_DATA 失败"
        bpc = struct.unpack_from("<I", vdb, 44)[0]        # BytesPerCluster
        frs = struct.unpack_from("<I", vdb, 48)[0]        # BytesPerFileRecordSegment
        cpf = struct.unpack_from("<i", vdb, 52)[0]        # ClustersPerFileRecordSegment
        mft_vdl = struct.unpack_from("<q", vdb, 56)[0]    # MftValidDataLength
        mft_lcn = struct.unpack_from("<q", vdb, 64)[0]    # MftStartLcn
        # ClustersPerFileRecordSegment：>0 时 FRS = cpf × 簇；<0 时 FRS 打包
        # 进簇（FRS = 簇 × 2^|cpf|）；**==0 表示未打包**，此时以
        # BytesPerFileRecordSegment（恒 1024）为准。曾经把 0 也当打包处理
        # 算出 frs_size=4096 —— 每 4 条真实记录只解析出 1 条，目录树断裂
        # （提权探针实测：D 盘 15.5 万「记录」实际只覆盖 620,800 条中的 1/4）。
        frs_size = frs if cpf >= 0 else bpc * (2 ** (-cpf))
        if frs_size <= 0 or bpc <= 0:
            return None, "卷参数异常"

        # 记录 0 就是 $MFT 自己：读它拿 $DATA 的 runlist（MFT 的全部盘区）
        head = bytearray(frs_size)
        if _mft_read_at(h, mft_lcn * bpc, frs_size, head) != frs_size:
            return None, "读取 $MFT 记录失败"
        head_info = _mft_parse_record(head, frs_size)
        if head_info is None:
            return None, "$MFT 记录解析失败"
        # $MFT 的 $DATA runlist：非驻留属性从内容偏移处开始
        runs: list[tuple[int, int]] = []
        off = head[0x14] | (head[0x15] << 8)
        while 0 < off <= frs_size - 16:
            a_type = int.from_bytes(head[off:off + 4], "little")
            if a_type == 0xFFFFFFFF:
                break
            a_len = int.from_bytes(head[off + 4:off + 8], "little")
            if a_len < 24 or off + a_len > frs_size:
                break
            if a_type == 0x80 and head[off + 8] != 0:
                c = off + (head[off + 0x20] | (head[off + 0x21] << 8))
                runs = _mft_runlist(head[c:frs_size], 0)
                break
            off += a_len
        if not runs:
            return None, "$MFT runlist 为空"

        est_records = max(1, mft_vdl // frs_size)
        floor = _core.DISK_TREE_MIN_CHILD_MB * 1024 * 1024
        # 文件名字保留门槛：目录树关心的是「达到目录门槛的文件」，
        # 大文件排行要更低的地板（10 MB 级）。目录门槛始终不变。
        f_floor = (file_floor_mb or _core.DISK_TREE_MIN_CHILD_MB) * 1024 * 1024
        t0 = time.time()
        arr_parent = array("q")
        arr_size = array("q")
        arr_flags = bytearray()            # bit0: 目录
        dir_names: dict[int, bytes] = {}   # frn -> 目录名（UTF-16LE 原始字节，
                                           # 2026-11-03 起惰性解码：35 万目录里
                                           # 最终进树的只有千余条，全量解码
                                           # 白付 ~0.4s；消费端 _mft_agg_paths
                                           # 按需解码）
        file_keep: dict[int, tuple[str, int, int]] = {}  # frn -> (名, 体积, 父)
        file_mtime: dict[int, int] = {}    # frn -> FILETIME（仅 with_mtime）
        exts: dict[str, list] = {}         # 扩展名 -> [字节, 个数]（仅 ext_stats）
        records = 0

        def read_chunks():
            """按 runlist 顺序吐出 (缓冲, 可用字节数)。

            常驻缓冲原地解析：记录不再逐条切片拷贝（百万级 1KB 分配），
            块间零头（跨 run 边界不足一条记录的尾巴）留在缓冲头部，仅做
            每块一次 ≤1KB 的小搬运 —— 旧版每 32MB 块要整块 memcpy 两次。
            """
            CH = max(frs_size * 1024, 16 * 1024 * 1024) - \
                (max(frs_size * 1024, 16 * 1024 * 1024) % frs_size)
            buf = bytearray(CH + frs_size)     # 尾部留一条记录给零头拼接
            held = 0                           # 头部已持有的上块零头字节数
            for lcn, clusters in runs:
                pos = lcn * bpc
                left = clusters * bpc
                while left > 0:
                    want = min(left, CH)
                    want -= want % frs_size
                    if want <= 0:
                        break
                    got = _mft_read_at(h, pos, want,
                                       memoryview(buf)[held:held + want])
                    if got <= 0:
                        return
                    held += got
                    usable = held - held % frs_size
                    yield buf, usable
                    held -= usable             # ≤ frs_size 的零头挪回头部
                    if held:
                        buf[:held] = buf[usable:usable + held]
                    pos += got
                    left -= got
            # 尾块不足一条记录：丢弃（截断的卷尾）

        # 读/解析流水线（2026-11-03）：**实测负优化，已回退**。C 盘探针：
        # 串行 read 4.44s + 解析 ~6.1s = 11.09s；改后台读线程 + 队列深度 3
        # 后 read 段膨胀到 21.14s、总耗时 35.55s —— 解析端纯 Python 几乎
        # 不放 GIL，读线程每次 DeviceIoControl 后重新拿 GIL 都要挨系统
        # 切换间隔（1140 次 × 1MB 的往返被拉长 5 倍）。ctypes 释放 GIL
        # 不等于重叠免费：**GIL 争用成本 > I/O 重叠收益**，此类流水线
        # 在纯 Python 消费端面前不要重试。提速改走「加大读块」路线
        # （减少系统调用次数，见 CH 取值）。
        for chunk, usable in read_chunks():
            for roff in range(0, usable, frs_size):
                idx = records
                records += 1
                in_use = roff + 0x18 <= usable and (chunk[roff + 0x16] & 0x01)
                info = _mft_parse_at(chunk, roff, frs_size,
                                     with_mtime) if in_use else None
                if info is None:
                    arr_parent.append(0)
                    arr_size.append(0)
                    arr_flags.append(0)
                    continue
                p, name_raw, size, is_dir, mtime = info
                arr_parent.append(p if 0 <= p < (1 << 47) else 0)
                arr_size.append(size)
                arr_flags.append(1 if is_dir else 0)
                if is_dir:
                    if name_raw and idx != 5:
                        dir_names[idx] = name_raw     # 惰性解码（见上）
                elif name_raw:
                    if ext_stats:
                        _core._ext_bump_raw(exts, name_raw, size)
                    if size >= f_floor or p == 5:
                        # 与 _wiztree_parse 同一门槛：达标文件 + 盘根全保留
                        # （AMTAG.BIN 这类小体积隐藏文件必须进「隐藏项」页签）
                        # 文件门槛可用 file_floor_mb 单独调低（大文件排行路径）
                        file_keep[idx] = (_mft_name_decode(name_raw), size, p)
                        if with_mtime and mtime:
                            file_mtime[idx] = mtime
            if on_progress and records % 65536 < frs_size // 1024 + 1:
                try:
                    on_progress(min(1.0, records / est_records))
                except Exception:
                    pass

        # 目录体积聚合 + 路径拼装（2026-11-03 定向化）：只为「会进树的
        # 目录」（体积达标 / 一层 / 文件父目录）拼路径，35 万目录里省掉
        # 99% 的 UTF-16 解码与字符串拼接（C 盘实测 ~1s）。
        dir_total, dir_path = _mft_agg_paths(
            arr_parent, arr_size, arr_flags, dir_names, root,
            floor, {p for (_n, _s, p) in file_keep.values()})

        dirs_out: dict[str, int] = {}
        for frn, path in dir_path.items():
            sz = dir_total.get(frn, 0)
            if frn == 5:
                dirs_out[root.lower()] = sz
            elif sz >= floor or path.count("\\") <= 1:
                # 与旧版同一过滤：链上为「文件父目录」补建的路径若不达标
                # 不进树（文件行照旧留在 files_out，与旧行为一致）
                dirs_out[path] = sz
        files_out: dict[str, list] = {}
        mtimes: dict[str, str] = {}
        for frn, (name, size, p) in file_keep.items():
            ppath = dir_path.get(p)
            if ppath is None:
                continue                    # 父目录不在树里（孤儿记录）
            full = ppath.rstrip("\\") + "\\" + name
            files_out.setdefault(ppath, []).append((name, size, full))
            if with_mtime and frn in file_mtime:
                mtimes[full] = _core._ft_to_iso(file_mtime[frn])
        table_out = {"dirs": dirs_out, "files": files_out, "rows": records,
                     "ext": exts}
        if with_mtime:
            table_out["mtimes"] = mtimes
        secs = round(time.time() - t0, 2)
        return table_out, {"secs": secs, "records": records}
    except Exception as e:                  # noqa: BLE001 —— 加速路径绝不挂掉扫描
        return None, f"MFT 直读异常：{type(e).__name__}: {e}"
    finally:
        try:
            _mft_k32().CloseHandle(h)   # 与句柄函数同一套签名配置
        except Exception:
            pass

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

    # 子项索引（2026-11-03 性能修复）：旧版每展开一个节点就把 dirs 全量扫
    # 一遍（O(节点数 × 目录数)——C 盘 ~40 万目录 × 预算 420 节点 ≈ 1.7 亿
    # 次 startswith/count 字符串比较，是「C 盘扫描慢」的主凶，比 MFT 解析
    # 本身还贵一个量级）。改为一次遍历按「父路径」分桶 + 桶内预排序，
    # kids_of 退化为一次字典查找 + 截前 max_per_dir。
    # 桶键与 kids_of 的 pre 一致（目录路径带尾反斜杠；盘根 "c:\" 天然就是
    # 顶层桶）。根目录自身条目（"c:" 去尾后无父）落进永不查询的兜底桶，
    # 恰好等价于旧版 `p == root_key` 的排除。
    children: dict[str, list[tuple[str, int]]] = {}
    for p, sz in dirs.items():
        key = p.rstrip("\\")
        i = key.rfind("\\")
        par = key[:i + 1] if i > 0 else key[:3]     # "c:\users"→"c:\"；畸形键兜底
        children.setdefault(par, []).append((p, sz))
    for bucket in children.values():
        bucket.sort(key=lambda t: -t[1])            # 体积降序预排，展开时免排序

    def min_descend(parent_size: int) -> int:
        d = int(parent_size * ratio)
        return max(int(floor), min(d, 4_000_000_000))

    def kids_of(path_key: str) -> list[dict]:
        """某一层的候选子项（目录 + 文件），按体积降序取前 max_per_dir。"""
        pre = path_key if path_key.endswith("\\") else path_key + "\\"
        items = []
        for p, sz in children.get(pre, ()):
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
    # —— 同样走子项索引（顶层桶键恰好是带尾反斜杠的 root_key）
    tops = []
    pre = root_key if root_key.endswith("\\") else root_key + "\\"
    for p, sz in children.get(pre, ()):
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
    # 隐藏项不受门槛限制，否则「隐藏项」页签会缺项：pagefile/hiberfil 这类
    # 体积大还好，AMTAG.BIN 这种几 KB 的隐藏文件会被 min_child 直接挡掉。
    # 判据用属性（_core._is_hidden_sys），不再只认名字名单。
    rootfiles = []
    for name, sz, full in files.get(root_key, ()):
        if sz >= min_child or _core._is_hidden_sys(name, full):
            rootfiles.append({"name": name, "path": full.lower(),
                              "size": sz, "modified": ""})
    return out, rootfiles, total

_WIZ_CACHE: dict | None = None

def _wiz_table(letter: str) -> dict:
    """取某盘符已缓存的 WizTree 体积表（可能为空 dict，调用方无需判 None）。"""
    return _core._WIZ_DIRS.get(letter, {}).get("table", {}) or {}

def _mft_agg_paths(arr_parent, arr_size, arr_flags, dir_names, root: str,
                   floor: int = 0, need_frns: "set[int] | None" = None):
    """由稠密数组聚合目录体积并拼全路径（纯函数，selftest 可测）。

    arr_*：以 frn 为下标的稠密数组（parent/size/flags，见
    _mft_build_table）；dir_names：frn → 目录名（**UTF-16LE 原始字节**，
    2026-11-03 起惰性解码）。
    返回 (dir_total, dir_path)。聚合两段式 O(记录数 + 目录边)：
    ①每个文件把体积记到**直接父目录**头上（1 次 dict 操作/文件，替代
    旧版沿 parent 链逐跳累加的 O(文件×深度)——百万文件 × 十来层是千万级
    dict 操作，基准 595ms → 105ms）；②一次 BFS 定序（父先于子）后反向
    聚合（子先于父）：total[d] = direct[d] + Σ total[child]。
    顺带修复旧版 hops<128 截断：超过 128 层的链会丢文件贡献，本版无此限。
    孤儿目录（不在根可达树上）不出现在结果里，与路径表一致。
    路径**定向拼装**（2026-11-03）：C 盘 35 万目录里进树的只有千余条，
    旧版给每一层每一目录都做 UTF-16 解码 + 字符串拼接白付 ~1s。本版只为
    「体积 ≥ floor、一层目录、以及 need_frns（达标文件的父目录）的祖先
    链」拼路径；floor=0 时全部可达目录都拼（等价旧行为，测试用）。
    """
    direct: dict[int, int] = {}
    n_recs = len(arr_size)
    for idx in range(n_recs):
        sz = arr_size[idx]
        if not sz or (arr_flags[idx] & 1):
            continue
        p = arr_parent[idx]
        if 0 < p < n_recs:
            direct[p] = direct.get(p, 0) + sz

    # 目录树拓扑（不需要名字）：从根（frn 5）BFS 定序；key 格式与
    # _wiztree_parse 对齐（盘根带尾反斜杠，其余不带）。
    kids: dict[int, list[int]] = {}
    for frn in dir_names:
        p = arr_parent[frn]
        if 0 < p < n_recs:
            kids.setdefault(p, []).append(frn)
    order = [5]
    qi = 0
    while qi < len(order):
        cur = order[qi]
        qi += 1
        order.extend(kids.get(cur, ()))
    dir_total: dict[int, int] = {}
    for d in reversed(order):
        s = direct.get(d, 0)
        for c in kids.get(d, ()):
            s += dir_total[c]
        dir_total[d] = s

    # 谁需要路径：根 / 体积达标 / 一层（parent==5）/ 文件父目录
    in_tree = set(order)
    want = {5}
    for frn in order:
        if frn != 5 and (dir_total.get(frn, 0) >= floor
                         or arr_parent[frn] == 5):
            want.add(frn)
    for frn in (need_frns or ()):
        if frn in in_tree:
            want.add(frn)

    # 按 parent 链向上补齐祖先（memo 化：链上每个名字只解码一次）
    dir_path: dict[int, str] = {5: root.lower()}
    for frn in want:
        chain = []
        cur = frn
        seen = set()                      # 环防御：损坏的 parent 链不成死循环
        while cur not in dir_path:
            if not 0 < cur < n_recs or cur in seen:
                chain = []
                break
            chain.append(cur)
            seen.add(cur)
            cur = arr_parent[cur]
        if not chain:
            continue
        base = dir_path[cur]
        for f in reversed(chain):
            nm = dir_names.get(f)
            if not nm:
                base = None               # 链上有无名目录：与旧版一致不给路径
                break
            base = (base if base.endswith("\\") else base + "\\") \
                + _mft_name_decode(nm)
            dir_path[f] = base
        if base is None:
            for f in chain:               # 回滚半截链
                dir_path.pop(f, None)
    return dir_total, dir_path

def _mft_big_files(tbl: dict, floor: int) -> list:
    """从 MFT 明细缓存抽取达标文件行 (path, size, mtime)，按体积降序。

    与 _wiztree_big_files 同一输出形态 —— big_files 的三条引擎路径
    （wiztree / mft / powershell）在 rows 之后共用同一套 identify 流程。
    mtime 仅在 build_table 以 with_mtime=True 生成时有值（大文件排行
    路径会传），否则为空串。
    """
    mtimes = tbl.get("mtimes") or {}
    rows = [(full, sz, mtimes.get(full, ""))
            for _parent, items in (tbl.get("files") or {}).items()
            for _name, sz, full in items if sz >= floor]
    rows.sort(key=lambda x: -x[1])
    return rows
