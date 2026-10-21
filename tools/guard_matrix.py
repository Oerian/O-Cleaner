# -*- coding: utf-8 -*-
"""
删除护栏对抗性矩阵（门禁）。

为什么单独一个门禁：
    UI 端到端测试（cdp_check.js，85 项）验的是「界面能跑通」，
    规则 linter 自测（_lint_selftest.py，44 项）验的是「规则本身没写错」，
    而 check_deletable 的**保护语义**没有任何自动化覆盖 —— 2026-10-21
    第二轮审计里，正是因为缺少这一层，`C:\\Users\\<name>\\Documents`
    被整目录真实删除（freed=14336, deleted=3）直到人工对抗测试才发现。

测什么（双向）：
    A. 必须拒绝：盘根 / 系统目录 / 工具自身 / 空串 / 路径绕过 / 8.3 短名
    B. 必须放行：Temp 一类缓存目录 —— 这几项是**防过度防护**的，
       少了它们会让护栏越加越严，最后什么都不敢删（功能失效）。
    C. 仅显式可选：用户自己的数据目录（文档/桌面/下载…）
       默认必须拒绝（预设、自动发现、定时清理走这条），
       显式勾选（allow_user_data=True）后才允许。
    C2. 域继承：受保护目录**下面**的子目录必须继承同一档保护 ——
       用户在占用排行里下钻后勾的正是这些，只保护树根等于没保护。
    D. 显式可选也不能放行的：系统目录。删了系统就坏了，
       不该由用户的一次误点决定。
    E. 8.3 短名（PROGRA~1）不得绕过位置保护。
    + 规则单元自检：名单内容与域判定（路径型用例测不到的部分，
      例如本机造不出名为 CON 的目录，只能直接断言规则函数）。

用法：
    python -X utf8 tools/guard_matrix.py            # 全绿 rc=0，有失败 rc=1
    python -X utf8 tools/guard_matrix.py -v         # 打印每一项

注意：本脚本只调用 check_deletable（纯判断，不删除任何东西），
      clean_target 涉及真删，不在这里测。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

# 数据目录指向临时目录，避免脚本顺手读写用户真实配置。
# （check_deletable 本身不读写配置，这是双保险。）
if "OC_DATA_DIR" not in os.environ:
    os.environ["OC_DATA_DIR"] = tempfile.mkdtemp(prefix="oc-guard-matrix-")

import app  # noqa: E402

B = chr(92)          # 反斜杠：避免源码里堆满转义
HOME = os.path.expanduser("~")


def j(*parts: str) -> str:
    """拼 Windows 路径。"""
    return B.join(parts)


def _ok(path: str, allow: bool = False):
    try:
        return app.check_deletable(path, "tree", allow)
    except Exception as e:                        # noqa: BLE001
        return f"EXC:{type(e).__name__}", str(e)


def build_cases():
    """返回 [(分组, 路径, 期望(默认), 期望(显式可选), 说明)]。

    期望值 None 表示「这一项与显式开关无关，不测」。
    """
    tmp_a = tempfile.mkdtemp(prefix="oc-guard-ok-a-")
    tmp_b = tempfile.mkdtemp(prefix="oc-guard-ok-b-")
    # 「别处的同名目录」：不在 C:\Users\<u>\ 下，位置规则管不到，只能靠名字名单。
    # 这两个用例是**名单规则唯一的独立覆盖** —— 少了它们，把名单清空也不会
    # 让门禁变红（位置规则会替它挡住 Users 下那几个），等于放弃了对名单的验证。
    nm_root = tempfile.mkdtemp(prefix="oc-guard-nm-")
    tmp_docs = os.path.join(nm_root, "Documents")
    tmp_desk = os.path.join(nm_root, "Desktop")
    for d in (tmp_docs, tmp_desk):
        os.makedirs(d, exist_ok=True)
    # 用户 Temp 下再放一层：验证「缓存容器不继承域保护」——
    # 少了这条，受保护域会把 %LOCALAPPDATA%\Temp 的子目录一起锁死（功能失效）。
    tmp_in_temp = None
    try:
        tmp_in_temp = tempfile.mkdtemp(
            prefix="oc-guard-ok-", dir=os.path.join(HOME, "AppData", "Local", "Temp"))
    except OSError:
        pass
    cases = [
        # ---------- A. 必须拒绝 ----------
        ("A 盘根与无效输入", "C:" + B, False, None, "盘符根"),
        ("A 盘根与无效输入", "C:", False, False, "盘符相对（无斜杠）"),
        ("A 盘根与无效输入", "D:" + B, False, None, "另一盘根"),
        ("A 盘根与无效输入", "", False, None, "空字符串"),
        ("A 盘根与无效输入", j("Z:", "nonexistent-oc"), False, None, "不存在的盘"),
        ("A 盘根与无效输入", "relative" + B + "path", False, None, "相对路径"),

        ("A 系统目录", j("C:", "Windows"), False, False, "Windows"),
        ("A 系统目录", j("C:", "Windows", "System32"), False, False, "System32"),
        ("A 系统目录", j("C:", "Program Files"), False, False, "Program Files"),
        ("A 系统目录", j("C:", "Program Files (x86)"), False, False,
         "Program Files (x86)"),
        ("A 系统目录", j("C:", "Users"), False, False, "用户根"),

        ("A 工具自身", str(HERE), False, False, "仓库根"),
        ("A 工具自身", str(HERE / "app.py"), False, None, "仓库内文件"),
        ("A 工具自身", j(str(HERE), "..", HERE.name), False, None, "仓库迂回路径"),
        ("A 工具自身", str(HERE / "config"), False, None, "仓库数据目录"),

        ("A 路径绕过", j("C:", "Windows", "..", "Windows", "System32"),
         False, None, "带 .. 的迂回"),
        ("A 路径绕过", B + B + "?" + B + j("C:", "Windows"),
         False, None, "长路径前缀"),

        # ---------- B. 必须放行（防过度防护）----------
        ("B 应放行", tmp_a, True, True, "普通临时目录"),
        ("B 应放行", tmp_b, True, True, "另一临时目录"),
        ("B 应放行", j("C:", "Windows", "Temp"), True, True, "Windows Temp"),
        ("B 应放行", os.path.join(HOME, "AppData", "Local", "Temp"),
         True, True, "用户 Temp"),

        # ---------- C. 仅显式可选（个人数据目录）----------
        ("C 仅显式可选", os.path.join(HOME, "Documents"), False, True, "文档"),
        ("C 仅显式可选", os.path.join(HOME, "Desktop"), False, True, "桌面"),
        ("C 仅显式可选", os.path.join(HOME, "Downloads"), False, True, "下载"),
        ("C 仅显式可选", os.path.join(HOME, "Pictures"), False, True, "图片"),
        ("C 仅显式可选", os.path.join(HOME, "Videos"), False, True, "视频"),
        ("C 仅显式可选", os.path.join(HOME, "Music"), False, True, "音乐"),
        ("C 仅显式可选", os.path.join(HOME, "Favorites"), False, True, "收藏夹"),
        ("C 仅显式可选", os.path.join(HOME, "Saved Games"), False, True,
         "保存的游戏"),
        ("C 仅显式可选", os.path.join(HOME, "AppData"), False, None, "AppData"),
        # 名字规则的独立覆盖（位置规则管不到这里）
        ("C 仅显式可选", tmp_docs, False, True, "别处的 Documents 目录（按名字保护）"),
        ("C 仅显式可选", tmp_desk, False, True, "别处的 Desktop 目录（按名字保护）"),

        # ---------- C2. 受保护域：保护按子树继承 ----------
        # 2026-10-21 护栏升级。原来的规则只保护「树根」（Documents 本身），
        # 而用户在占用排行里下钻之后勾的是子目录 —— 上次真实事故
        # （Documents\Adobe 被改名暂存）就是从这条缝进来的。
        ("C2 域继承", os.path.join(HOME, "Documents", "Adobe"), False, True,
         "文档下的子目录（必须继承保护）"),
        ("C2 域继承", os.path.join(HOME, "AppData", "Local"), False, True,
         "AppData\\Local 整体"),
        ("C2 域继承", os.path.join(HOME, "AppData", "Roaming"), False, True,
         "AppData\\Roaming 整体"),
        ("C2 域继承", os.path.join(HOME, "AppData", "LocalLow"), False, True,
         "AppData\\LocalLow 整体"),
        ("C2 域继承", os.path.join(HOME, "AppData", "Local", "Microsoft"),
         False, True, "AppData 下的程序数据"),
        ("C2 域继承", j("C:", "ProgramData", "Microsoft"), False, True,
         "ProgramData 下的程序数据"),
        ("C2 域继承", j("C:", "ProgramData", "Package Cache"), False, True,
         "安装包缓存（删了无法修复/卸载）"),

        # ---------- D. 显式可选也必须拒绝 ----------
        ("D 显式也拒绝", j("C:", "Windows"), False, False, "Windows"),
        ("D 显式也拒绝", j("C:", "Program Files"), False, False, "Program Files"),
        ("D 显式也拒绝", str(HERE), False, False, "仓库根"),
        ("D 显式也拒绝", j("C:", "Windows", "System32"), False, False, "System32"),
        ("D 显式也拒绝", j("C:", "Program Files", "Common Files"), False, False,
         "共享组件（多个程序共用，删了集体起不来）"),
        ("D 显式也拒绝", j("C:", "Program Files", "WindowsApps"), False, False,
         "商店应用本体"),
        ("D 显式也拒绝", j("C:", "Users", "Public"), False, False, "公共目录"),

        # ---------- E. 8.3 短名不得绕过 ----------
        ("E 8.3 短名", j("C:", "PROGRA~1"), False, False, "PROGRA~1（应展开为 Program Files）"),

        # ---------- F. 盘根仍不删 ----------
        # 盘根只允许「按 *.ext 清零散文件」这一条窄口，且窄口写在 clean_target
        # 里、不在 check_deletable 里 —— 后者不知道 glob，让它开口就等于
        # 允许任何调用方用 `*` 清盘根。kind="files" 的情形见规则单元自检。
        ("F 盘根不删", "C:" + B, False, False, "盘根"),
    ]
    # 缓存容器不继承域保护（条件用例：本机能建出 %LOCALAPPDATA%\Temp 子目录才测）
    if tmp_in_temp:
        cases.append(("B 应放行", tmp_in_temp, True, True,
                      "用户 Temp 的子目录（缓存容器不继承域保护）"))
    # 缓存目录的**深层**子项也必须放行：`%TEMP%\<某个包>\node_modules` 的末级
    # 和父级都不是缓存名，但它明明就在 Temp 里。只按一层豁免会误拦这类正常
    # 清理项（CI 首次跑全量自测时就是被这条挡红的）。
    deep_cache = os.path.join(tmp_in_temp or tempfile.mkdtemp(prefix="oc-guard-deep-"),
                              "some-pkg", "node_modules")
    try:
        os.makedirs(deep_cache, exist_ok=True)
        cases.append(("B 应放行", deep_cache, True, True,
                      "缓存目录的深层子项（整链豁免）"))
    except OSError:
        pass
    return cases


def unit_failures() -> list:
    """规则单元自检：路径型用例测不到的东西（名单内容、域判定）。

    为什么单独一段：像「保留设备名」「受保护域」这类规则，在本机上根本
    造不出对应的真实路径（Windows 不允许建名为 CON 的目录，ProgramData
    下建目录要管理员），路径型断言会因为「目标不存在」而「通过」——
    那是假绿。所以直接对判定函数与名单本身做断言。
    """
    bad = []
    need_dev = {"con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"}
    if not need_dev <= set(app.RESERVED_DEVICE_NAMES):
        bad.append(f"保留设备名名单缺失：{sorted(need_dev - set(app.RESERVED_DEVICE_NAMES))}")
    zone_cases = [
        (["Users", "u", "Documents", "Adobe"], "user"),
        (["Users", "u", "Documents"], "user"),
        (["Users", "u", ".ssh"], "user"),
        (["Users", "u", "AppData", "Local"], "appdata"),
        (["Users", "u", "AppData", "Local", "Microsoft"], "appdata"),
        (["Users", "u", "AppData"], ""),          # AppData 本身由名字名单管
        (["Users", "u", "AppData", "Local", "Temp"], "appdata"),  # 域内，但叶子是容器 → 由豁免放行
        (["Users", "u", "Temp"], ""),
        (["ProgramData", "Microsoft"], "programdata"),
        (["ProgramData", "Temp"], ""),
        (["Program Files", "Foo"], "programfiles"),
        (["Program Files (x86)", "Foo"], "programfiles"),
        (["Program Files", "Foo", "cache"], "programfiles"),
        (["Windows", "Temp"], ""),
        (["Games", "Foo"], ""),
    ]
    for parts, want in zone_cases:
        got = app._guarded_zone(list(parts))
        if got != want:
            bad.append(f"_guarded_zone({parts}) 期望 {want!r} 实际 {got!r}")
    # 绝不删名单里必须常驻的几项
    for name in ("common files", "windowsapps", "appdata", "windows",
                 "system32", "program files", "users", "$recycle.bin"):
        if name not in app.PROTECTED_DIR_NAMES:
            bad.append(f"PROTECTED_DIR_NAMES 缺失 {name!r}")

    # 盘根窄口（v2.9）：「按 *.ext 清零散文件」只允许一类扩展名，
    # 且 check_deletable 这个硬闸本身永远不认盘根 —— 无论 kind 是什么。
    for g in ("*.part", "*.crdownload", "*.download"):
        if not app.drive_root_glob_ok(g):
            bad.append(f"drive_root_glob_ok({g!r}) 应为 True")
    for g in ("*", "*.*", "**", "*.part*", ".part", "*.*.part", ""):
        if app.drive_root_glob_ok(g):
            bad.append(f"drive_root_glob_ok({g!r}) 应为 False")
    if not app._is_drive_root_path("C:" + B):
        bad.append("_is_drive_root_path('C:\\\\') 应为 True")
    if app._is_drive_root_path("C:"):
        bad.append("_is_drive_root_path('C:') 应为 False（盘符相对不是盘根）")
    for kind in ("tree", "files"):
        ok, why = app.check_deletable("C:" + B, kind)
        if ok:
            bad.append(f"check_deletable('C:\\\\', {kind!r}) 竟放行（盘根硬闸被绕过）")
    return bad


def main(argv=None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    verbose = "-v" in argv or "--verbose" in argv

    cases = build_cases()
    passed, failed, skipped = 0, [], 0
    last_group = ""

    if verbose:
        print(f"{'':2s}{'分组':16s}{'路径':44s}{'默认':6s}{'显式':6s} 说明")
        print("-" * 104)

    for group, path, want_default, want_optin, desc in cases:
        if group != last_group:
            last_group = group
            if verbose:
                print(f"  -- {group} --")
        # 需要存在的路径才能测（不存在的目录会被「目标不存在」提前拒绝，
        # 那测的就不是我们想测的规则了）。期望为「拒绝」的可以不存在。
        need_exists = bool(want_default) or bool(want_optin)
        if need_exists and not os.path.exists(path):
            skipped += 1
            if verbose:
                print(f"   {'skip':52s}{'':6s}{'':6s} {desc}（本机不存在）")
            continue

        got_default, why_d = _ok(path, False)
        line = f"{'OK ' if got_default == want_default else 'X  '}{group:16s}{path[-42:]:44s}{str(got_default):6s}"
        bad = got_default != want_default
        if bad:
            failed.append(f"[默认] {group} {path} 期望 {want_default} 实际 {got_default} ({why_d})")

        got_optin = "-"
        if want_optin is not None:
            got_optin, why_o = _ok(path, True)
            line += f"{str(got_optin):6s}"
            if got_optin != want_optin:
                bad = True
                failed.append(f"[显式] {group} {path} 期望 {want_optin} 实际 {got_optin} ({why_o})")
        else:
            line += f"{'-':6s}"

        if verbose:
            print(f"{line} {desc}")
        if not bad:
            passed += 1

    # 规则单元自检（名单内容与域判定），路径型用例测不到的部分
    unit_bad = unit_failures()
    for b in unit_bad:
        failed.append("[单元] " + b)
    if verbose:
        print(f"   {'-- 规则单元自检 --':52s} {'通过' if not unit_bad else '失败'}")
        for b in unit_bad:
            print("     " + b)

    if not verbose:
        # 非 verbose 只在失败时说话，保持门禁输出干净
        for f in failed:
            print("  失败：" + f)

    print(f"护栏矩阵：通过 {passed} 项，失败 {len(failed)} 项，跳过 {skipped} 项"
          f"（另含规则单元自检 {1 if not unit_bad else 0}/{1}）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
