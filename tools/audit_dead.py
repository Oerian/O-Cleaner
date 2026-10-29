# 死代码审计（只读，照 HANDOFF §46 流程：先审计→人工核实→才删）。
# 用法: python tools/audit_dead.py   已知限制: 重复类选择器不排除 @media 覆盖，需人工确认。
# 只读死代码审计 v3：裸词保守判定（宁漏报不误删）
import re, collections

h = open("web/index.html", encoding="utf-8").read()
styles = re.findall(r"<style[^>]*>(.*?)</style>", h, re.S)
scripts = re.findall(r"<script[^>]*>(.*?)</script>", h, re.S)
st = "\n".join(styles)
sc = "\n".join(scripts)
rest = h
for blk in styles:
    rest = rest.replace(blk, "", 1)
for blk in scripts:
    rest = rest.replace(blk, "", 1)

print("=== 1. CSS：裸词在 rest 与 sc 中都不出现 ===")
defined = set(re.findall(r"\.([A-Za-z_][-\w]*)", st))
css_dead = [c for c in sorted(defined) if c not in rest and c not in sc]
print("[CSS 未引用]", css_dead, f"共 {len(css_dead)} 条")

print("=== 3. DOM id：裸词在 sc/st 中都不出现 ===")
ids_dead = [i for i in re.findall(r'\bid="([^"$]+)"', rest)
            if i not in sc and f"#{i}" not in st]
print("[DOM 未引用]", ids_dead, f"共 {len(ids_dead)} 条")

print("=== 4. 重复类选择器（上下文） ===")
cnt = collections.Counter(re.findall(r"^\s*\.([A-Za-z_][-\w]*)\s*[,{]", st, re.M))
for k, v in cnt.items():
    if v > 1:
        print("·", k)
        for m in re.finditer(r"^[ \t]*\." + re.escape(k) + r"\s*[,{].*$", st, re.M):
            print("   ", m.group(0).strip()[:110])

print("=== 7. i18n：t() 使用的键必须有 EN 词条（否则英文界面回退中文） ===")
m = re.search(r"I18N_EN\s*=\s*\{", sc)
i = m.end() - 1; depth = 0
for j, ch in enumerate(sc[i:], start=i):
    if ch == "{": depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0: break
en_keys = set(re.findall(r'"([a-z]+\.[\w.]+)":', sc[i:j]))
used = set(re.findall(r't\("(\w+\.[\w.]+)"', sc))
used |= set(re.findall(r'data-i18n(?:-html|-ph|-title)?="(\w+\.[\w.]+)"', rest))
# 动态拼接键：报告但不判死（人工确认）
dyn = re.findall(r't\("([\w.]+)\s*\+', sc)
# 已知间接使用（t(变量) 形式，键名以字面量出现在同文件其它调用里，
# 如 showCleanResult 的 head("cr.onceHead", ...)）：
INDIRECT = {"cr.onceHead", "cr.regenHead", "cr.unkHead"}
missing = sorted(used - en_keys - INDIRECT)
print("t() 使用但 EN 缺失:", missing if missing else "无", f"(使用 {len(used)} 键 / 词典 {len(en_keys)} 键)")
if INDIRECT - en_keys:
    print("[白名单键在 EN 词典缺失]:", sorted(INDIRECT - en_keys))
if dyn: print("[动态拼接键，需人工核对]:", sorted(set(dyn)))

print("=== 8. CSS 变量：未定义的使用 / 未使用的定义 ===")
var_def = set(re.findall(r"--([\w-]+)\s*:", st))
var_use = set(re.findall(r"var\(--([\w-]+)", st + sc))
undef = sorted(u for u in var_use - var_def)
unused = sorted(d for d in var_def - var_use)
print("使用了但未定义:", undef if undef else "无")
print("定义了但未使用:", unused if unused else "无")
