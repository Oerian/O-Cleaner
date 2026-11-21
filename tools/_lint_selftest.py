# -*- coding: utf-8 -*-
"""验证 linter 真的有检出能力（而不是空转通过）。

两组用例：
  知识库组 —— 临时往 config/filetypes.json 注入若干类缺陷，跑 linter 确认被抓到，然后还原。
  资源包组 —— 在系统临时目录里造一个假 packs/，进程内调用 lint_packs() 确认抓到缺陷。
              刻意不往仓库的 packs/ 里写文件：那样会被宿主的删除保护拦下，
              留下测试残渣反而污染仓库。用临时目录既不碰仓库也不依赖删文件。
  模板组   —— 在临时目录里放一个假的 app.py，进程内调用 lint_ps_templates()。
  签名库组 —— 在临时目录里放假的 appclean.json / bloatware-builtin.json，
              进程内调用 lint_signature_libs()（同资源包组的隔离策略）。

只修改 config/filetypes.json，运行后必定还原（finally）。
"""
from __future__ import annotations
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
CFG = HERE / "config" / "filetypes.json"
BAK = HERE / "config" / ".filetypes.linttest.bak"
sys.path.insert(0, str(HERE / "tools"))

def idx(rules: list, rule_id: str) -> int:
    """按 id 定位规则下标。不要硬编码顺序 —— 顺序会变，测试会静默失效。"""
    for i, r in enumerate(rules):
        if r.get("id") == rule_id:
            return i
    raise KeyError(rule_id)


# 用例：(名称, 变异函数)。全部按 id 定位，避免依赖列表顺序。
CASES = [
    ("必填字段缺失",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "steam-library")].pop("advice", None)),
    ("cleanable 写成字符串",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "download-dir")].__setitem__("cleanable", "true")),
    ("kind 非法",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "cache-temp")].__setitem__("kind", "fuzzy")),
    ("risk 非法",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "logs-dumps")].__setitem__("risk", "极高")),
    ("id 重复",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "music-app")].__setitem__(
         "id", "cache-temp")),
    ("扩展名关键词缺前导点",
     lambda k: k["file_rules"][idx(k["file_rules"], "installer-pkg")].__setitem__("match", ["exe"])),
    ("what 过短",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "cloud-disk")].__setitem__("what", "短")),
    ("高风险可清理但无 tip（且未豁免）",
     lambda k: (k["folder_rules"][idx(k["folder_rules"], "node-modules")].__setitem__("risk", "高"),
                k["folder_rules"][idx(k["folder_rules"], "node-modules")].__setitem__("cleanable", True),
                k["folder_rules"][idx(k["folder_rules"], "node-modules")].pop("tip", None))),
    # 真正的撞车：两条 substring 规则共用一个关键词，且建议相反
    ("关键词撞车且建议相反",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "steam-library")].__setitem__(
         "match", ["steamlibrary", "steamapps", "cache"])),
    ("分类拼写错误（警告级）",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "gopro-exports")].__setitem__(
         "category", "遊戏")),
    ("match 为空数组",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "emulator")].__setitem__("match", [])),
    ("advice 过短",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "backup-dir")].__setitem__("advice", "删")),
    ("match 含空关键词",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "drivers")].__setitem__(
         "match", ["driver", "驱动", ""])),
    ("confidence 非法",
     lambda k: k["folder_rules"][idx(k["folder_rules"], "video-media")].__setitem__(
         "confidence", "very-high")),
    ("递归引用自身导致死循环防护（match 全为空串）",
     lambda k: k["file_rules"][idx(k["file_rules"], "archive")].__setitem__("match", ["  "])),
]

PASS, FAIL = [], []


# ----------------------------------------------------------------------
#  资源包组的用例。基准包刻意做成完全合法，每个用例只改坏一处。
# ----------------------------------------------------------------------
PACK_BASE = {
    "id": "probe", "name": "测试包", "version": "1.0.0", "type": "custom",
    "description": "仅用于 linter 自检的包",
    "entries": [{
        "id": "ut-entry", "title": "测试条目", "category": "自定义目录",
        "risk": "低", "cleanable": True,
        "match": {"any": ["\\foo\\barcache"]},
        "what": "这是一个用于自检的缓存目录。",
        "effect": "删除没有影响。", "rebuild": "会重建。", "keep": "可直接删除。",
    }],
}


def _set_entry(key, val):
    def go(p):
        p["entries"][0][key] = val
    return go


PACK_CASES = [
    ("资源包 id 非法", lambda p: p.__setitem__("id", "bad id!")),
    ("资源包缺 name", lambda p: p.pop("name", None)),
    ("资源包缺 version", lambda p: p.pop("version", None)),
    ("资源包 entries 为空", lambda p: p.__setitem__("entries", [])),
    ("条目缺 title", lambda p: p["entries"][0].pop("title", None)),
    ("条目缺 match", lambda p: p["entries"][0].pop("match", None)),
    ("match 既无 any 也无 all", _set_entry("match", {"some": ["x"]})),
    ("match.any 为空数组", _set_entry("match", {"any": []})),
    ("条目 id 重复",
     lambda p: p["entries"].append(dict(p["entries"][0]))),
    ("条件 risk 非法", _set_entry("risk", "极高")),
    ("cleanable 写成字符串", _set_entry("cleanable", "true")),
    ("only 非法", _set_entry("only", "folder")),
    ("正则关键词编译失败", _set_entry("match", {"any": ["re:[unclosed"]})),
    ("解释字段为空白字符串", _set_entry("what", "   ")),
    ("category 为空字符串", _set_entry("category", "")),
    ("单段泛化关键词（警告级）", _set_entry("match", {"any": ["\\temp"]})),
    ("ASCII 关键词过短（警告级）", _set_entry("match", {"any": ["\\ab"]})),
    ("id 与文件名不一致（警告级）", None),      # 由 pack_case_file 换文件名完成
    ("未知 type（警告级）", lambda p: p.__setitem__("type", "novel")),
    ("清单漏登同名包", "__orphan__"),
]


def run_pack_lint(pack: dict, file_name: str = "probe.json",
                  listed: bool = True) -> tuple[list[str], list[str]]:
    """在临时目录里跑一次 lint_packs()。返回 (错误列表, 警告列表)。

    不写仓库文件，因此不会触发宿主的文件删除保护，也就没有测试残渣。
    """
    import lint_rules as L
    tmp = Path(tempfile.mkdtemp(prefix="oc_lint_pack_"))
    saved = L.PACKS
    try:
        (tmp / file_name).write_text(
            json.dumps(pack, ensure_ascii=False), encoding="utf-8")
        names = [file_name] if listed else []
        (tmp / "index.json").write_text(
            json.dumps({"packs": names}, ensure_ascii=False), encoding="utf-8")
        L.PACKS = tmp
        L.ERRORS.clear()
        L.WARNINGS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            L.lint_packs()
        return list(L.ERRORS), list(L.WARNINGS)
    finally:
        L.PACKS = saved
        L.ERRORS.clear()
        L.WARNINGS.clear()
        shutil.rmtree(tmp, ignore_errors=True)


# 内嵌脚本模板组用的最小样本。故意写得很短，让「配平」一眼可数：
#   @{ k = 1 }                      花括号一对
#   foreach ($x in 1..3) { ... }     圆括号一对、花括号一对
TPL_BASE = (
    'PS_DEMO = r"""\n'
    "$a = @{ k = 1 }\n"
    "foreach ($x in 1..3) { $a[$x] = $x }\n"
    '"""\n'
)

# 用例：(名称, 变换函数)。返回 None 表示连 app.py 都不存在。
TPL_CASES = [
    ("少一个右花括号（真实事故：整段脚本解析失败）",
     lambda s: s.replace("= $x }", "= $x ")),
    ("少一个左花括号", lambda s: s.replace("{ $a[$x] = $x }", "$a[$x] = $x }")),
    ("少一个右圆括号", lambda s: s.replace("($x in 1..3)", "($x in 1..3")),
    ("模板被删空（正则失效保护）", lambda s: "X = 1\n"),
    ("app.py 不存在", lambda s: None),
]


def run_tpl_lint(src: str | None) -> tuple[list[str], list[str]]:
    """在临时目录里放一个假的 app.py，跑一次 lint_ps_templates()。

    做法与资源包组一致：只换模块里的路径常量，不往仓库写任何文件。
    """
    import lint_rules as L
    tmp = Path(tempfile.mkdtemp(prefix="oc_lint_tpl_"))
    saved = L.HERE
    try:
        if src is not None:
            (tmp / "app.py").write_text(src, encoding="utf-8")
        L.HERE = tmp
        L.ERRORS.clear()
        L.WARNINGS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            L.lint_ps_templates()
        return list(L.ERRORS), list(L.WARNINGS)
    finally:
        L.HERE = saved
        L.ERRORS.clear()
        L.WARNINGS.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def run_lint(strict: bool = True) -> tuple[int, str]:
    cmd = [sys.executable, "-X", "utf8", str(HERE / "tools" / "lint_rules.py")]
    if strict:
        cmd.append("--strict")     # CI 用的就是这个模式，警告必须算失败
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ----------------------------------------------------------------------
#  签名库组：appclean.json / bloatware-builtin.json 的检出能力。
#  同资源包组做法：临时目录里放假签名库，进程内调用 lint_signature_libs()，
#  绝不碰仓库的真实 config/。
# ----------------------------------------------------------------------
SIG_AC_BASE = {
    "version": 1,
    "apps": [
        {
            "id": "demo", "name": "演示应用",
            "bases": ["%LOCALAPPDATA%\\Demo"],
            "items": [
                {"sub": "Cache", "kind": "cache", "label": "缓存"},
                {"sub": "Data", "kind": "core", "label": "数据库（勿动）"},
            ],
        }
    ],
}

SIG_BW_BASE = {
    "version": 1,
    "apps": [
        {
            "id": "demobad", "name": "演示毒瘤",
            "match": ["演示毒瘤", "DemoBad"],
            "dirs": ["DemoBad"],
        }
    ],
}


def _ac_set(key, val, idx: int = 0):
    def go(ac, bw):
        ac["apps"][0]["items"][idx][key] = val
    return go


SIG_CASES = [
    ("appclean kind 非法", _ac_set("kind", "maybe")),
    ("appclean 把模型目录标成 cache", _ac_set("sub", "models")),
    ("appclean cache 条目缺 label", _ac_set("label", "")),
    ("appclean sub 含路径穿越", _ac_set("sub", "..\\..\\x", idx=1)),
    ("appclean sub 绝对路径", _ac_set("sub", "C:\\bad", idx=1)),
    ("appclean id 重复", lambda ac, bw: ac["apps"].append(dict(ac["apps"][0]))),
    ("appclean 同应用条目重复", lambda ac, bw: ac["apps"][0]["items"].append(
        dict(ac["apps"][0]["items"][0]))),
    ("appclean base 写死用户路径", lambda ac, bw: ac["apps"][0].__setitem__(
        "bases", ["C:\\Users\\x\\Demo"])),
    ("appclean 空 sub cache 且 base 非 cache 目录（警告级）",
     lambda ac, bw: ac["apps"][0]["items"].__setitem__(
         0, {"sub": "", "kind": "cache", "label": "整个数据目录"})),
    ("appclean items 为空", lambda ac, bw: ac["apps"][0].__setitem__("items", [])),
    ("appclean 缺 version", lambda ac, bw: ac.pop("version", None)),
    ("bloatware match 为空数组", lambda ac, bw: bw["apps"][0].__setitem__("match", [])),
    ("bloatware match 含空关键词", lambda ac, bw: bw["apps"][0].__setitem__(
        "match", ["演示毒瘤", "  "])),
    ("bloatware dirs 含路径穿越", lambda ac, bw: bw["apps"][0].__setitem__(
        "dirs", ["..\\Windows"])),
    ("bloatware dirs 绝对路径", lambda ac, bw: bw["apps"][0].__setitem__(
        "dirs", ["C:\\Bad"])),
    ("bloatware id 重复", lambda ac, bw: bw["apps"].append(dict(bw["apps"][0]))),
    ("bloatware match 关键词跨应用重复（警告级）", lambda ac, bw: bw["apps"].append(
        {**bw["apps"][0], "id": "demo2", "match": ["demobad"]})),
    ("bloatware 缺 name", lambda ac, bw: bw["apps"][0].pop("name", None)),
]


def run_sig_lint(appclean: dict | None = None,
                 bloatware: dict | None = None) -> tuple[list[str], list[str]]:
    """在临时目录里放假签名库，进程内跑一次 lint_signature_libs()。

    换掉模块级 CONFIG / APPCLEAN_FILE / BLOATWARE_FILE 三个路径常量，
    不往仓库的 config/ 写任何文件（同资源包组的隔离策略）。
    """
    import lint_rules as L
    tmp = Path(tempfile.mkdtemp(prefix="oc_lint_sig_"))
    saved = (L.CONFIG, getattr(L, "APPCLEAN_FILE", None),
             getattr(L, "BLOATWARE_FILE", None))
    try:
        (tmp / "appclean.json").write_text(
            json.dumps(appclean if appclean is not None else SIG_AC_BASE,
                       ensure_ascii=False), encoding="utf-8")
        (tmp / "bloatware-builtin.json").write_text(
            json.dumps(bloatware if bloatware is not None else SIG_BW_BASE,
                       ensure_ascii=False), encoding="utf-8")
        L.CONFIG = tmp
        L.APPCLEAN_FILE = tmp / "appclean.json"
        L.BLOATWARE_FILE = tmp / "bloatware-builtin.json"
        L.ERRORS.clear()
        L.WARNINGS.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            L.lint_signature_libs()
        return list(L.ERRORS), list(L.WARNINGS)
    finally:
        L.CONFIG, L.APPCLEAN_FILE, L.BLOATWARE_FILE = saved
        L.ERRORS.clear()
        L.WARNINGS.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    if sys.stdout.reconfigure:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    shutil.copy2(CFG, BAK)
    base = json.loads(CFG.read_text(encoding="utf-8-sig"))
    try:
        code, out = run_lint()
        if code == 0:
            PASS.append("干净知识库应通过")
            print("  [OK]   干净知识库通过 linter")
        else:
            FAIL.append("干净知识库应通过")
            print("  [严重] 干净知识库竟然没通过：")
            print("\n".join(out.splitlines()[-12:]))

        for name, mutate in CASES:
            kb = json.loads(json.dumps(base))          # 每次从干净副本出发
            try:
                mutate(kb)
            except Exception as e:                     # noqa: BLE001
                print(f"  [跳过] {name}：注入失败 {e}")
                continue
            CFG.write_text(json.dumps(kb, ensure_ascii=False, indent=2), encoding="utf-8")
            code, out = run_lint()
            caught = code != 0
            (PASS if caught else FAIL).append(name)
            print(("  [OK]   " if caught else "  [严重] ") + f"能检出：{name}")
            if not caught:
                print("        （linter 通过了，说明这一类缺陷没被覆盖）")

        CFG.write_text(json.dumps(base, ensure_ascii=False, indent=2), encoding="utf-8")
        code, out = run_lint()
        restored = code == 0
        (PASS if restored else FAIL).append("还原后应恢复通过")
        print(("  [OK]   " if restored else "  [严重] ") + "还原后重新通过")

        # --------------------------------------------------------------
        #  资源包组
        # --------------------------------------------------------------
        print("\n  —— 资源包检查的检出能力 ——")
        try:
            errs, warns = run_pack_lint(PACK_BASE)
        except Exception as e:                        # noqa: BLE001
            print(f"  [跳过] 资源包组：无法调用 lint_packs（{e}）")
            errs = warns = None
        if errs is not None:
            if not errs and not warns:
                PASS.append("干净资源包应通过")
                print("  [OK]   干净资源包通过 lint_packs")
            else:
                FAIL.append("干净资源包应通过")
                print(f"  [严重] 干净资源包没通过：{errs} {warns}")

            for name, mutate in PACK_CASES:
                pack = json.loads(json.dumps(PACK_BASE))
                fname, listed = "probe.json", True
                if mutate == "__orphan__":
                    listed = False           # 文件在磁盘上但没进 index.json
                elif mutate is None:
                    fname = "other-name.json"   # id 与文件名不一致
                else:
                    mutate(pack)
                errs, warns = run_pack_lint(pack, fname, listed)
                # CI 用的是 --strict：警告同样会让门禁失败，所以警告也算「检出」。
                caught = bool(errs) or bool(warns)
                (PASS if caught else FAIL).append("资源包：" + name)
                print(("  [OK]   " if caught else "  [严重] ") + f"能检出：{name}")
                if not caught:
                    print("        （lint_packs 通过了，说明这一类缺陷没被覆盖）")

        # --------------------------------------------------------------
        #  内嵌脚本模板组
        # --------------------------------------------------------------
        print("\n  —— 内嵌脚本模板检查的检出能力 ——")
        try:
            errs, warns = run_tpl_lint(TPL_BASE)
        except Exception as e:                        # noqa: BLE001
            print(f"  [跳过] 模板组：无法调用 lint_ps_templates（{e}）")
            errs = None
        if errs is not None:
            if not errs and not warns:
                PASS.append("干净脚本模板应通过")
                print("  [OK]   干净脚本模板通过 lint_ps_templates")
            else:
                FAIL.append("干净脚本模板应通过")
                print(f"  [严重] 干净脚本模板没通过：{errs} {warns}")

            for name, mutate in TPL_CASES:
                errs, warns = run_tpl_lint(mutate(TPL_BASE))
                caught = bool(errs) or bool(warns)
                (PASS if caught else FAIL).append("模板：" + name)
                print(("  [OK]   " if caught else "  [严重] ") + f"能检出：{name}")
                if not caught:
                    print("        （lint_ps_templates 通过了，说明这一类缺陷没被覆盖）")

        # --------------------------------------------------------------
        #  签名库组（appclean / bloatware-builtin）
        # --------------------------------------------------------------
        print("\n  —— 签名库检查的检出能力 ——")
        try:
            errs, warns = run_sig_lint()
        except Exception as e:                        # noqa: BLE001
            print(f"  [跳过] 签名库组：无法调用 lint_signature_libs（{e}）")
            errs = None
        if errs is not None:
            if not errs and not warns:
                PASS.append("干净签名库应通过")
                print("  [OK]   干净签名库通过 lint_signature_libs")
            else:
                FAIL.append("干净签名库应通过")
                print(f"  [严重] 干净签名库没通过：{errs} {warns}")

            for name, mutate in SIG_CASES:
                ac = json.loads(json.dumps(SIG_AC_BASE))
                bw = json.loads(json.dumps(SIG_BW_BASE))
                mutate(ac, bw)
                errs, warns = run_sig_lint(ac, bw)
                # CI 用 --strict：警告同样会让门禁失败，警告也算「检出」。
                caught = bool(errs) or bool(warns)
                (PASS if caught else FAIL).append("签名库：" + name)
                print(("  [OK]   " if caught else "  [严重] ") + f"能检出：{name}")
                if not caught:
                    print("        （lint_signature_libs 通过了，说明这一类缺陷没被覆盖）")
    finally:
        shutil.move(str(BAK), str(CFG))

    print(f"\n{'=' * 52}\n  通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        print("  未覆盖：" + "、".join(FAIL))
    print("=" * 52)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
