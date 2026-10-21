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
    D. 显式可选也不能放行的：系统目录。删了系统就坏了，
       不该由用户的一次误点决定。

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

        # ---------- D. 显式可选也必须拒绝 ----------
        ("D 显式也拒绝", j("C:", "Windows"), False, False, "Windows"),
        ("D 显式也拒绝", j("C:", "Program Files"), False, False, "Program Files"),
        ("D 显式也拒绝", str(HERE), False, False, "仓库根"),
        ("D 显式也拒绝", j("C:", "Windows", "System32"), False, False, "System32"),

        # ---------- E. 8.3 短名不得绕过 ----------
        ("E 8.3 短名", j("C:", "PROGRA~1"), False, False, "PROGRA~1（应展开为 Program Files）"),
    ]
    return cases


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

    if not verbose:
        # 非 verbose 只在失败时说话，保持门禁输出干净
        for f in failed:
            print("  失败：" + f)

    print(f"护栏矩阵：通过 {passed} 项，失败 {len(failed)} 项，跳过 {skipped} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
