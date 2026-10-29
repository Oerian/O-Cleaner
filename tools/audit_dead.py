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
