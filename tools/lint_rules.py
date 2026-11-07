# -*- coding: utf-8 -*-
"""
规则 linter —— 发布前的安全闸门。

一条错误的识别规则会真的让用户删掉不该删的东西，而用户不会看免责声明。
所以在接受任何规则贡献之前，必须先过这一关。

检查内容：
  结构层  内置规则 ID 唯一、必填字段、类型正确、枚举合法、关键词非空且不重复
  资源包  packs/ 下的包与清单互相对得上、条目结构合法、匹配串可用
  语义层  分类是否在已知集合内、cleanable 与 risk 是否自相矛盾
  冲突层  关键词在规则之间撞车（A 的词被 B 抢走）
  路由层  用知识库自己的每个关键词去跑真实识别，验证「归属规则」能否赢
  一致性  项目文档与配置是否漂移（如 README 里的文件清单）

用法:
    python -X utf8 tools/lint_rules.py
    python -X utf8 tools/lint_rules.py --strict     # 警告也视为失败（CI 用）
    python -X utf8 tools/lint_rules.py --json       # 机器可读输出
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
HERE = TOOLS_DIR.parent
sys.path.insert(0, str(HERE))

CONFIG = HERE / "config"
FILETYPES = CONFIG / "filetypes.json"
TARGETS = CONFIG / "targets.json"
DRIVES = CONFIG / "drives.json"
PACKS = HERE / "packs"
WEB_INDEX = HERE / "web" / "index.html"

# 资源包（packs/）的校验常量。
# 资源包是可替换的外部数据，会被 UI 直接当成「这是什么、能不能删」展示，
# 所以结构和语义都要在这一关挡住，不能等运行到用户机器上再报错。
PACK_ID_RE = r"[A-Za-z0-9._-]{2,64}"
PACK_REQUIRED_META = ("id", "name", "version", "type", "description")
PACK_MAX_ENTRIES = 5000
PACK_ONLY_VALUES = {"", "dir", "file"}
# 文本字段的下限。这里刻意设得很低，只用来挡住「空壳条目」：
# 实测现有包里 effect 最短是「删除等于卸载。」、rebuild 是「不适用。」、
# keep 是「保留。」—— 这些短句本身就是准确表述，按内置规则的 15 字标准去卡
# 只会逼作者往句子里注水，反而降低信息密度。所以只对 what 设一个下限，
# 因为「这是什么」字段若短到 8 字以下，基本上等于没说。
PACK_WHAT_MIN = 8
PACK_TEXT_FIELDS = ("what", "effect", "rebuild", "keep")
# 已知包类型。新增类型不会让运行时报错（只是标签退化成「自定义」），
# 但在这里提醒一句，免得作者以为加个 type 就能在界面上分出新的组。
KNOWN_PACK_TYPES = {"games", "system", "apps", "custom"}

# app.py 里内嵌的 Windows 脚本模板（PS_* = r"""..."""）。
# 这些模板由 Python 拼字符串生成、再交给系统 shell 执行，所以语法错误
# 不会在 Python 侧暴露。此处做一条最便宜的静态检查：括号配平。
# 对应的真实事故见 lint_ps_templates() 的注释。
SCRIPT_TEMPLATE_RE = re.compile(
    r'^(?P<name>PS_[A-Z0-9_]+)\s*=\s*r?"""(?P<body>.*?)"""', re.S | re.M)

# 已知分类集合。出现新分类多半是拼写错误；确实要新增时把它加到这里。
KNOWN_CATEGORIES = {
    "游戏", "模拟器", "个人数据", "下载", "聊天软件", "云盘", "创作软件", "创作数据",
    "软件", "工具", "音乐", "视频", "音频", "文档", "开发", "AI 模型", "虚拟机",
    "系统", "缓存", "诊断", "压缩包", "镜像", "安装包", "备份", "回收站", "浏览器",
    "其它",
}
KNOWN_RISKS = {"无", "低", "中", "高"}
KNOWN_CONFIDENCE = {"high", "medium", "low"}

# 过于宽泛的关键词：会匹配到大量无关目录或文件
GENERIC_KEYWORDS = {
    "data", "file", "files", "my", "new", "old", "test", "temp", "app", "apps",
    "doc", "docs", "img", "image", "images", "user", "users", "系统", "我的",
    "新建", "工具", "文件", "数据", "备份", "下载",
}

# 「已复核、有意为之」的豁免清单。
# 目的：让 --strict 能通过，同时不逼迫作者为了过 CI 而削弱规则的表达力。
# 每一项都必须写明理由和复核日期，并给出联系方式外的责任人（PR 作者）。
# 注意：豁免只影响本文件里的规则检查，不会让引擎行为发生变化。
ACCEPTED: dict[str, dict[str, str]] = {
    "keyword:temp": {
        "reason": "temp/tmp 是最常见的临时目录命名，虽然有误匹配风险，"
                  "但引擎对 ≤4 字符的英文关键词要求整词边界匹配，已足够安全。"
                  "已有 selftest 覆盖 temp 相关用例。",
        "reviewed": "2026-09-16",
    },
    "keyword:下载": {
        "reason": "中文无词边界，但「下载」作为目录名指向明确，"
                  "且它只把项目标为「可清理」而非直接删除，用户仍需确认。",
        "reviewed": "2026-09-16",
    },
    "keyword:备份": {
        "reason": "备份目录标记为「不可清理」，误匹配的后果是少提示而非多删除，"
                  "属于安全方向的偏差。",
        "reviewed": "2026-09-16",
    },
    "keyword:工具": {
        "reason": "「工具」目录在中文环境常见，且 software-dir 为「不可清理」，"
                  "误匹配只会导致保守判断。",
        "reviewed": "2026-09-16",
    },
    "high-risk-cleanable:recycle-bin": {
        "reason": "回收站在语义上确实可清理（它就是待删文件），风险高的原因是不可恢复。"
                  "引擎的 recycle 预设与主列表都把它单独列出，并要求用户展开确认条目，"
                  "风险已被 UI 与原语共同约束。",
        "reviewed": "2026-09-16",
    },
}


def accepted(key: str) -> bool:
    if key in ACCEPTED:
        USED_ACCEPTED.add(key)
        return True
    return False


def accepted_reason(key: str) -> str:
    a = ACCEPTED.get(key, {})
    return f"已复核豁免（{a.get('reviewed', '?')}）：{a.get('reason', '')}"


def lint_accepted_list() -> None:
    """豁免清单自身也要能被检查：已不再触发的条目应当删掉。

    否则这份清单会慢慢变成「掩盖新问题」的垃圾场 —— 那才是真正的风险。
    """
    print("\n[附] 豁免清单检查")
    stale = sorted(set(ACCEPTED) - USED_ACCEPTED)
    for k in stale:
        warn(f"豁免 {k!r} 已不再触发（规则或被删、或已修正），请从 ACCEPTED 里移除")
    print(f"  登记 {len(ACCEPTED)} 条，本次生效 {len(USED_ACCEPTED)} 条，"
          f"失效 {len(stale)} 条")

# 关键词长度下限（英文），低于此值必须整词匹配才安全（引擎里已实现）
SHORT_KEYWORD_LEN = 4

REQUIRED_FIELDS = ("id", "match", "kind", "title", "category", "risk",
                   "cleanable", "confidence", "what", "advice")
ADVICE_MIN_LEN = 12
WHAT_MIN_LEN = 15

ERRORS: list[str] = []
WARNINGS: list[str] = []
NOTES: list[str] = []
USED_ACCEPTED: set[str] = set()


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def note(msg: str) -> None:
    NOTES.append(msg)


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        err(f"缺文件：{path.relative_to(HERE)}")
    except json.JSONDecodeError as e:
        err(f"JSON 解析失败 {path.relative_to(HERE)}：{e}")
    return None


# ======================================================================
#  1. 结构层
# ======================================================================
def lint_structure(all_rules: list[dict]) -> None:
    print("\n[1/8] 结构检查")
    seen_ids: dict[str, str] = {}
    for src, rules in (("folder", [r for r in all_rules if r.get("_src") == "folder"]),
                       ("file", [r for r in all_rules if r.get("_src") == "file"])):
        for i, r in enumerate(rules):
            where = f"{src}_rules[{i}] id={r.get('id', '?')}"
            for f in REQUIRED_FIELDS:
                if f not in r:
                    err(f"{where} 缺必填字段 {f}")
            rid = r.get("id")
            if rid:
                if rid in seen_ids:
                    err(f"{where} id 与 {seen_ids[rid]} 重复")
                seen_ids[rid] = where
            if not isinstance(r.get("cleanable"), bool):
                err(f"{where} cleanable 必须是布尔值（不要写字符串）")
            if r.get("kind") not in ("exact", "substring", "extension"):
                err(f"{where} kind 非法：{r.get('kind')}（应为 exact/substring/extension）")
            if r.get("risk") not in KNOWN_RISKS:
                err(f"{where} risk 非法：{r.get('risk')}（应为 {'/'.join(sorted(KNOWN_RISKS))}）")
            if r.get("confidence") not in KNOWN_CONFIDENCE:
                err(f"{where} confidence 非法：{r.get('confidence')}")
            m = r.get("match")
            if not isinstance(m, list) or not m:
                err(f"{where} match 必须是非空数组")
            else:
                if len(set(m)) != len(m):
                    dup = [k for k in set(m) if list(m).count(k) > 1]
                    warn(f"{where} match 内有重复关键词：{dup}")
                if r.get("kind") == "extension":
                    bad = [k for k in m if not str(k).startswith(".")]
                    if bad:
                        err(f"{where} kind=extension 的关键词必须以点开头：{bad}")
                if r.get("kind") == "exact" and any(
                        all(ord(c) < 128 for c in str(k)) and len(str(k)) < 3 for k in m):
                    warn(f"{where} kind=exact 出现过短关键词，有误匹配风险")
                if r.get("kind") == "substring":
                    empt = [k for k in m if not str(k).strip()]
                    if empt:
                        err(f"{where} match 含空关键词")
            if r.get("category") not in KNOWN_CATEGORIES:
                warn(f"{where} 分类 {r.get('category')!r} 不在已知集合内（拼写错误？）")
            if len(str(r.get("what") or "")) < WHAT_MIN_LEN:
                err(f"{where} what 过短（至少 {WHAT_MIN_LEN} 字，需说明这是什么）")
            if len(str(r.get("advice") or "")) < ADVICE_MIN_LEN:
                err(f"{where} advice 过短（至少 {ADVICE_MIN_LEN} 字，需说明怎么处理）")

    # 关键词在规则内自撞（同一规则里 A 是 B 的子串是允许的，跨规则才是问题）
    note(f"共检查 {len(all_rules)} 条规则、{len(seen_ids)} 个唯一 id")
    print(f"  {'通过' if not ERRORS else '发现问题'}：错误 {len(ERRORS)}，警告 {len(WARNINGS)}")


# ======================================================================
#  2. 资源包
# ======================================================================
def _pack_cat_order() -> list[str]:
    """从前端读分类排序表，用于提示「这个分类会排到最后」。"""
    try:
        txt = WEB_INDEX.read_text(encoding="utf-8")
    except OSError:
        return []
    m = re.search(r"const CAT_ORDER\s*=\s*\[(.*?)\];", txt, re.S)
    return re.findall(r'"([^"]+)"', m.group(1)) if m else []


def _pack_tokens_of(entry: dict) -> list[str]:
    m = entry.get("match")
    if not isinstance(m, dict):
        return []
    out: list[str] = []
    for k in ("any", "all"):
        v = m.get(k)
        if isinstance(v, str):
            v = [v]
        if isinstance(v, list):
            out += [str(x).strip() for x in v]
    return out


def lint_packs() -> None:
    """检查 packs/ 下的资源包与清单。

    这一关要和运行时校验（app.py 的 pack_validate）**分别实现**：
    运行时校验只挡「会让程序崩」的输入，而这里是发布前的闸门，
    还要挡「能跑但会把界面搞乱」的问题（分类排到最后、文件名与 id 不一致、
    匹配串太宽泛以至于抓错目录）。
    """
    print("\n[2/8] 资源包检查")
    n0 = len(ERRORS)

    if not PACKS.is_dir():
        # 缺目录不算错：用户可以不装任何资源包，功能退化成只有内置规则。
        warn("缺 packs/ 目录（没有资源包时功能仍可用，只是释义较少）")
        print("  跳过（无 packs/ 目录）")
        return

    index = load(PACKS / "index.json")
    listed: list[str] = []
    if index is None:
        pass
    elif not isinstance(index, dict):
        err("packs/index.json 必须是一个 JSON 对象")
    else:
        raw = index.get("packs")
        if not isinstance(raw, list) or not raw:
            err("packs/index.json 的 packs 必须是非空数组")
        else:
            # v1：文件名字符串；v2：{"file", "sha256"} 对象（仓库基建）
            listed = [str(x["file"]) if isinstance(x, dict) and x.get("file")
                      else str(x) for x in raw]
            if len(set(listed)) != len(listed):
                err("packs/index.json 的 packs 列表有重复项")

    on_disk = sorted(f.name for f in PACKS.glob("*.json")
                     if f.name.lower() != "index.json")
    # 清单与实际文件必须对得上：漏登会让「从 GitHub 检查更新」少下载一个包，
    # 多登会下载一个 404，两种情况都只在用户点按钮时才暴露。
    for name in listed:
        if name not in on_disk:
            err(f"packs/index.json 列了 {name}，但磁盘上没有这个文件")
    for name in on_disk:
        if name not in listed:
            err(f"packs/ 下的 {name} 没被 index.json 收录（检查更新时会漏掉它）")

    cat_order = _pack_cat_order()
    unknown_cats: set[str] = set()
    total_entries = 0
    for name in on_disk:
        p = PACKS / name
        d = load(p)
        if d is None:
            continue
        if not isinstance(d, dict):
            err(f"{name} 必须是 JSON 对象")
            continue
        for k in PACK_REQUIRED_META:
            if not str(d.get(k) or "").strip():
                err(f"{name} 缺字段 {k}")
        pid = str(d.get("id") or "").strip()
        if pid and not re.fullmatch(PACK_ID_RE, pid):
            err(f"{name} 的 id {pid!r} 非法（只允许字母数字点下划线连字符，2~64 字符）")
        elif pid and pid != p.stem:
            # 运行时按 id 决定「谁覆盖谁」，按文件名决定「下载哪个」，两者不一致
            # 会让「同名覆盖内置包」这件事在远端更新时失效。
            warn(f"{name} 的 id 是 {pid!r}，与文件名不一致（建议改成同一个）")
        ptype = str(d.get("type") or "")
        if ptype and ptype not in KNOWN_PACK_TYPES:
            warn(f"{name} 的 type {ptype!r} 不在已知集合"
                 f"（{'/'.join(sorted(KNOWN_PACK_TYPES))}），界面会显示成「自定义」")

        ents = d.get("entries")
        if not isinstance(ents, list) or not ents:
            err(f"{name} 的 entries 必须是非空数组")
            continue
        if len(ents) > PACK_MAX_ENTRIES:
            err(f"{name} 的 entries 过多（{len(ents)} 条，上限 {PACK_MAX_ENTRIES}）")
        total_entries += len(ents)
        seen_eids: dict[str, int] = {}
        for i, e in enumerate(ents):
            where = f"{name} entries[{i}]"
            if not isinstance(e, dict):
                err(f"{where} 不是对象")
                continue
            eid = str(e.get("id") or "").strip()
            if eid:
                if eid in seen_eids:
                    err(f"{where} 的 id {eid!r} 与 entries[{seen_eids[eid]}] 重复")
                else:
                    seen_eids[eid] = i
                where = f"{name} entries[{i}] id={eid}"
            if not str(e.get("title") or "").strip():
                err(f"{where} 缺 title（界面直接显示它，不能为空）")
            m = e.get("match")
            if not isinstance(m, dict):
                err(f"{where} 缺 match 对象")
            elif not (m.get("any") or m.get("all")):
                err(f"{where} 的 match 既没有 any 也没有 all")
            for k in ("any", "all"):
                v = m.get(k) if isinstance(m, dict) else None
                if v is not None and not isinstance(v, (str, list)):
                    err(f"{where} 的 match.{k} 必须是字符串或数组")
            toks = _pack_tokens_of(e)
            if isinstance(m, dict) and (m.get("any") or m.get("all")) and not toks:
                err(f"{where} 的 match 里没有有效关键词")
            for tok in toks:
                if not tok:
                    err(f"{where} 的 match 含空关键词")
                    continue
                if tok.startswith("re:"):
                    try:
                        re.compile(tok[3:])
                    except re.error as ex:
                        err(f"{where} 的正则 {tok!r} 编译失败：{ex}")
                    continue
                # 单段关键词（不含路径分隔符）直接拿名称做子串匹配，
                # 太泛会抓走一堆无关目录：`temp` 会命中所有临时目录。
                segs = [x for x in re.split(r"[\\/]+", tok) if x]
                if len(segs) == 1:
                    key = segs[0].lower()
                    if key in GENERIC_KEYWORDS:
                        warn(f"{where} 的关键词 {tok!r} 过于宽泛，建议写成带目录的路径")
                    elif len(key) < 3 and all(ord(c) < 128 for c in key):
                        # 只在纯 ASCII 时判「过短」：两个字母（ab）会命中一切，
                        # 而两个汉字（原神）已经足够特指，跟内置规则的口径一致。
                        warn(f"{where} 的关键词 {tok!r} 过短，有误匹配风险")
            if e.get("only") is not None and str(e.get("only")) not in PACK_ONLY_VALUES:
                err(f"{where} 的 only 非法：{e.get('only')!r}（应为 dir 或 file）")
            if e.get("risk") is not None and str(e.get("risk")) not in KNOWN_RISKS:
                err(f"{where} 的 risk 非法：{e.get('risk')!r}")
            if e.get("cleanable") is not None and not isinstance(e.get("cleanable"), bool):
                err(f"{where} 的 cleanable 必须是布尔值")
            # category 是可选的。注意这里不能用 `continue` 提前退出 ——
            # 一旦某条不相关的字段缺失就跳过本条目的其余检查，检查会静默失效
            # （这个坑已被单元测试抓到一次：不带 category 的条目会绕过文本字段检查）。
            cat = e.get("category")
            if cat is not None:
                if not str(cat).strip():
                    err(f"{where} 的 category 不能是空字符串")
                elif cat_order and str(cat) not in cat_order:
                    unknown_cats.add(str(cat))
            for fld in PACK_TEXT_FIELDS:
                if fld not in e:
                    continue
                v = e.get(fld)
                if not isinstance(v, str) or not v.strip():
                    err(f"{where} 的 {fld} 存在但为空（不想覆盖就整项省略）")
                elif fld == "what" and len(v.strip()) < PACK_WHAT_MIN:
                    warn(f"{where} 的 what 只有 {len(v.strip())} 字，"
                         f"短于 {PACK_WHAT_MIN} 字基本等于没说清这是什么")

    if unknown_cats:
        # 只是排序问题（未知分类排到最后），不影响正确性，所以只提示不判失败。
        note(f"资源包用到 {len(unknown_cats)} 个不在前端排序表里的分类："
             f"{sorted(unknown_cats)}")
    note(f"资源包 {len(on_disk)} 个、条目 {total_entries} 条")
    print(f"  包 {len(on_disk)} 个、条目 {total_entries} 条"
          f"（错误 {len(ERRORS) - n0}，警告 {len(WARNINGS)}）")


# ======================================================================
#  3. 语义自相矛盾
# ======================================================================
def lint_semantics(all_rules: list[dict]) -> None:
    print("\n[3/8] 语义一致性检查")
    n0 = len(ERRORS)
    for r in all_rules:
        rid = r.get("id")
        # 高风险 + 可清理 = 危险组合，必须显式写明保留条件
        if r.get("risk") == "高" and r.get("cleanable"):
            key = f"high-risk-cleanable:{rid}"
            if accepted(key):
                note(f"[豁免] {rid} 高风险可清理 —— {accepted_reason(key)}")
            elif not r.get("tip"):
                err(f"{rid} 标为『高风险 + 可清理』却没有 tip 说明例外情况，"
                    f"这对用户太危险")
            else:
                warn(f"{rid} 标为『高风险 + 可清理』，请确认这是有意为之"
                     f"（引擎会把它列入清理建议）。若确认无误，请在 "
                     f"lint_rules.py 的 ACCEPTED 里登记豁免并写明理由")
        # 不可清理却声称能自动重建，逻辑上不一致
        if not r.get("cleanable"):
            rb = str(r.get("what", "")) + str(r.get("advice", ""))
            if "可以清理" in rb and "不要" not in rb:
                warn(f"{rid} cleanable=false 但文案里写着『可以清理』，容易误导")
        # 建议清理的规则必须给出去向说明，否则用户不知道该不该信
        if r.get("cleanable") and not r.get("advice"):
            err(f"{rid} cleanable=true 但没有 advice")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  3. 关键词跨规则冲突
# ======================================================================
def lint_keyword_collisions(all_rules: list[dict]) -> None:
    print("\n[4/8] 关键词冲突检查")
    n0 = len(ERRORS)
    k2r: dict[str, list[dict]] = {}
    for r in all_rules:
        for k in (r.get("match") or []):
            k2r.setdefault(str(k).lower(), []).append(r)

    for k, owners in sorted(k2r.items()):
        if len(owners) == 1:
            continue
        ids = {o.get("id") for o in owners}
        if len(ids) == 1:
            continue                      # 同一规则内重复，已在结构层处理
        # 归属不同规则：只有当它们对用户的建议相反时才危险
        cleanable = {bool(o.get("cleanable")) for o in owners}
        detail = "、".join(f"{o.get('id')}(可清理={o.get('cleanable')})" for o in owners)
        if len(cleanable) > 1:
            err(f"关键词 {k!r} 被多个建议相反的规则共用：{detail}")
        else:
            warn(f"关键词 {k!r} 被多条规则共用：{detail}")

    # 宽泛关键词
    for k in sorted(k2r):
        if k not in GENERIC_KEYWORDS:
            continue
        key = f"keyword:{k}"
        if accepted(key):
            note(f"[豁免] 关键词 {k!r} 已知宽泛 —— {accepted_reason(key)}")
        else:
            warn(f"关键词 {k!r} 过于宽泛，容易误匹配（归属 "
                 f"{'、'.join(o.get('id') or '?' for o in k2r[k])}）。"
                 f"若确认必要，请在 lint_rules.py 的 ACCEPTED 里登记豁免并写明理由")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  4. 路由验证 —— 用真实识别引擎验证归属
# ======================================================================
def lint_routing(all_rules: list[dict]) -> None:
    """对每条规则的每个关键词，构造样本跑真实 identify()，验证归属规则能赢。"""
    print("\n[5/8] 路由验证（用真实引擎跑知识库自己的关键词）")
    n0 = len(ERRORS)
    try:
        import app
    except Exception as e:                # noqa: BLE001
        err(f"无法导入 app.py，跳过路由验证：{e}")
        return

    checked = 0
    ctx_skipped = 0
    stolen_dangerous: list[str] = []
    stolen_benign: list[str] = []
    # 这些规则的匹配依赖「真实文件系统上下文」，用不存在的合成路径测不出结果：
    # 浏览器目录必须真的含 User Data / Default / Cache 结构才算数，
    # 否则 D:\Chrome 这种合成路径必然不匹配，只会产生误报。
    CONTEXT_RULES = {"browser-data", "browser-profile"}
    for r in all_rules:
        is_dir = r.get("_src") == "folder"
        for k in (r.get("match") or []):
            k = str(k)
            if r.get("kind") == "extension":
                sample = "sample" + k
            elif is_dir:
                sample = k
            else:
                # 文件规则：关键词可能是文件名或扩展名
                sample = k if not k.startswith(".") else "sample" + k
            if r.get("id") in CONTEXT_RULES and is_dir:
                ctx_skipped += 1
                continue
            got = app.identify(sample, "D:\\" + sample, "dir" if is_dir else "file")
            checked += 1
            if got.get("rule_id") == r.get("id"):
                continue
            # 归属失败：判断危险程度
            if r.get("cleanable") and not got.get("cleanable"):
                stolen_benign.append(
                    f"{r.get('id')} 的关键词 {k!r} 被 {got.get('rule_id') or '未识别'} 抢走"
                    f"（本该可清理，结果变成不建议清理）")
            elif r.get("cleanable") and got.get("cleanable"):
                stolen_dangerous.append(
                    f"{r.get('id')} 的关键词 {k!r} 被 {got.get('rule_id')} 抢走"
                    f"（两条都是可清理，实际归类为 {got.get('title')}）")
            else:
                stolen_benign.append(
                    f"{r.get('id')} 的关键词 {k!r} 被 {got.get('rule_id') or '未识别'} 抢走"
                    f"（都不建议清理，影响较小）")

    # 只有「两条都建议清理但归属不同」才会让用户删错东西，算错误；
    # 其余归属失败只影响解释准确性，算警告。
    for m in stolen_dangerous:
        err(m)
    for m in stolen_benign:
        warn(m)
    print(f"  验证了 {checked} 个关键词样本（另有 {ctx_skipped} 个上下文依赖型规则跳过），"
          f"归属失败 {len(stolen_dangerous)} 危险 / {len(stolen_benign)} 轻微")
    note(f"路由验证样本数 {checked}")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  5. 与项目文档/配置的一致性
# ======================================================================
def lint_consistency() -> None:
    print("\n[6/8] 文档与配置一致性")
    n0 = len(ERRORS)
    readme = (HERE / "README.md")
    if not readme.exists():
        warn("缺 README.md")
    else:
        txt = readme.read_text(encoding="utf-8")
        for f in ("app.py", "config\\targets.json", "config\\filetypes.json",
                  "web\\index.html", "tools\\selftest.py", "tools\\lint_rules.py"):
            if Path(f).name not in txt:
                warn(f"README 未提到 {f}")

    # targets.json 检查
    t = load(TARGETS)
    if t:
        ids = [x.get("id") for x in t.get("targets", [])]
        if len(ids) != len(set(ids)):
            err("targets.json 存在重复目标 id")
        for x in t.get("targets", []):
            for f in ("id", "label", "path", "category", "risk", "kind",
                      "short", "what", "rebuild", "impact", "keep_if"):
                if not x.get(f):
                    err(f"targets.json 目标 {x.get('id')} 缺字段 {f}")
            if x.get("kind") not in ("tree", "files", "recyclebin"):
                err(f"targets.json 目标 {x.get('id')} kind 非法：{x.get('kind')}")
            if x.get("kind") == "files" and not x.get("glob"):
                err(f"targets.json 目标 {x.get('id')} kind=files 但没给 glob")
        for p in t.get("presets", []):
            sel = p.get("select", {})
            if "ids" not in sel and "risk" not in sel:
                err(f"targets.json 预设 {p.get('id')} 既没给 ids 也没给 risk")
            for want in sel.get("ids", []) or []:
                if want not in ids:
                    err(f"targets.json 预设 {p.get('id')} 引用了不存在的目标 {want}")

    d = load(DRIVES)
    if d is not None and not d.get("app", {}).get("name"):
        err("drives.json 缺 app.name")
    print(f"  错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  6. 内嵌脚本模板的语法（app.py + ps_scripts.py 的 PS_* 模板）
# ======================================================================
def lint_ps_templates() -> None:
    """检查内嵌脚本模板（app.py + ps_scripts.py）的括号是否配平。

    这一关是被一次真实事故催出来的：v2.3 给磁盘递归脚本加「按节点预算上报
    进度」时，`if (...) {` 少写了一个右花括号。模板本身能渲染、占位符也都能
    替换，Python 侧毫无异样；但脚本一交给系统 shell 就在解析阶段直接失败
    （Missing closing '}' in statement block or type definition.），
    全盘扫描因此整条不可用 —— 界面表现为「概览扫描失败」，浏览器检查里
    依赖它的三十多条断言成片跳过。

    静态检查只能数括号，正式的语法判定交给自测里那次解析器调用
    （tools/selftest.py 阶段七）。两者角度不同：这里保证「一眼可见的配平
    问题」在提交前就被挡下，而且不依赖 Windows 环境也能跑。
    """
    print("\n[7/8] 内嵌脚本模板语法（app.py + ps_scripts.py 的 PS_* 模板）")
    n0 = len(ERRORS)
    found: list[tuple[str, str]] = []
    for fname in ("app.py", "ps_scripts.py"):
        fpath = HERE / fname
        if not fpath.exists():
            if fname == "app.py":
                err("找不到 app.py，无法检查内嵌脚本模板")
            else:
                # ps_scripts.py 缺失不算单独的错：真删了模板的话，
                # 两个文件加起来 0 个模板会被下面的兜底报错拦住
                # （缺陷注入测试只在临时目录放一个假 app.py，不能误伤它）。
                print("  ps_scripts.py 不存在，跳过该文件")
            continue
        hits = SCRIPT_TEMPLATE_RE.findall(fpath.read_text(encoding="utf-8"))
        if not hits and fname == "ps_scripts.py":
            err("ps_scripts.py 里没有匹配到任何 PS_* 脚本模板"
                "（模板应集中在该文件，app.py 里不应再有）")
        elif not hits and fname == "app.py":
            print("  app.py 已无 PS_* 模板（已全部抽出至 ps_scripts.py），正常")
        found.extend((f"[{fname}] {name}", body) for name, body in hits)
    if not found:
        err("app.py + ps_scripts.py 加起来没有匹配到任何 PS_* 脚本模板"
            "（检查用的正则可能已失效）")
        return
    for name, body in found:
        for open_ch, close_ch in (("{", "}"), ("(", ")")):
            a, b = body.count(open_ch), body.count(close_ch)
            if a != b:
                err(f"{name} 的 {open_ch}{close_ch} 不配平："
                    f"{open_ch}={a} {close_ch}={b}"
                    f"（少一个 {close_ch} 会让整段脚本在解析阶段直接失败）")
    print(f"  检查了 {len(found)} 个脚本模板，错误 {len(ERRORS) - n0} 项")


# ======================================================================
#  7. 危险删除面 —— 明确列出「这条规则会让工具去删什么」
# ======================================================================
def lint_delete_surface(all_rules: list[dict]) -> None:
    print("\n[8/8] 可清理规则清单（人工复核用）")
    cl = [r for r in all_rules if r.get("cleanable")]
    print(f"  共 {len(cl)} 条规则会把项目列入清理建议：")
    for r in sorted(cl, key=lambda x: (x.get("risk") != "高", x.get("id") or "")):
        mark = "!!!" if r.get("risk") == "高" else (" ! " if r.get("risk") == "中" else "   ")
        note(f"{mark} {r.get('id'):<24} 风险{r.get('risk')}  {r.get('title')}")
    high = [r for r in cl if r.get("risk") == "高"]
    note(f"其中高风险 {len(high)} 条")
    print(f"  高风险项 {len(high)} 条（已在上面标 !!!，请逐条人工确认）")


def main() -> int:
    if os.name == "nt":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="O-Cleaner 规则 linter")
    ap.add_argument("--strict", action="store_true", help="警告也视为失败")
    ap.add_argument("--json", action="store_true", help="输出 JSON 结果")
    args = ap.parse_args()

    print("O-Cleaner 规则 linter")
    print("=" * 68)

    kb = load(FILETYPES)
    if not kb:
        print("\n无法加载知识库，终止。")
        return 1

    all_rules = []
    for r in kb.get("folder_rules", []):
        all_rules.append({**r, "_src": "folder"})
    for r in kb.get("file_rules", []):
        all_rules.append({**r, "_src": "file"})

    lint_structure(all_rules)
    lint_packs()
    lint_semantics(all_rules)
    lint_keyword_collisions(all_rules)
    lint_routing(all_rules)
    lint_consistency()
    lint_ps_templates()
    lint_delete_surface(all_rules)
    lint_accepted_list()

    print("\n" + "=" * 68)
    if ERRORS:
        print(f"错误 {len(ERRORS)} 项：")
        for m in ERRORS:
            print("  [错误] " + m)
    if WARNINGS:
        print(f"\n警告 {len(WARNINGS)} 项：")
        for m in WARNINGS:
            print("  [警告] " + m)
    if not ERRORS and not WARNINGS:
        print("全部检查通过。")

    ok = not ERRORS and not (args.strict and WARNINGS)
    print("=" * 68)
    print(f"结论：{'通过' if ok else '不通过'}"
          f"（错误 {len(ERRORS)}，警告 {len(WARNINGS)}）"
          + ("  [--strict]" if args.strict else ""))

    if args.json:
        print(json.dumps({"ok": ok, "errors": ERRORS, "warnings": WARNINGS,
                          "notes": NOTES}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
