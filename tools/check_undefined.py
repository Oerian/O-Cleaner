# -*- coding: utf-8 -*-
"""静态检查：ocleaner 包各模块经 `_core.X` 引用的符号必须在 core 命名空间。

背景（批次 D 迁出后的真实缺陷）：模块迁出时外部引用改写为 `_core.X`，
若对应符号后来从 core 的导入/定义中被移除（如 pyflakes 误判 unused），
运行时才会以 AttributeError 暴露 —— 曾导致「保存 AI 配置」「检查更新」
两个功能 500。本检查把这类缺陷拦截在提交前。

纯标准库，零依赖。CI 快级与本地均可随时运行（<1 秒）。
"""
import ast
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def core_namespace() -> set:
    """AST 解析 core.py 的模块级绑定（def/class/赋值/导入）。"""
    src = (ROOT / "ocleaner" / "core.py").read_text(encoding="utf-8")
    ns = set()
    for node in ast.parse(src).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            ns.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    ns.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            ns.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                ns.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                ns.add(a.asname or a.name)
        elif isinstance(node, ast.If) or isinstance(node, ast.Try):
            # 条件/异常块内的顶层绑定（如 IS_WINDOWS 分支）——保守收集
            for sub in ast.walk(node):
                if isinstance(sub, ast.Assign):
                    for t in sub.targets:
                        if isinstance(t, ast.Name):
                            ns.add(t.id)
                elif isinstance(sub, ast.Import):
                    for a in sub.names:
                        ns.add(a.asname or a.name.split(".")[0])
                elif isinstance(sub, ast.ImportFrom):
                    for a in sub.names:
                        ns.add(a.asname or a.name)
    return ns


def main() -> int:
    ns = core_namespace()
    errors = []
    pkg = ROOT / "ocleaner"
    for f in sorted(pkg.glob("*.py")):
        if f.name in ("core.py", "__init__.py"):
            continue
        rel = f"ocleaner/{f.name}"
        src = f.read_text(encoding="utf-8")
        for m in sorted(set(re.findall(r"\b_core\.([A-Za-z_][A-Za-z0-9_]*)", src))
                        if (re := __import__("re")) else []):
            if m not in ns:
                errors.append(f"{rel}: _core.{m} 不在 core 命名空间"
                              f"（符号被移除或未 re-export）")
    if errors:
        print("== _core 引用完整性检查：失败 ==")
        for e in errors:
            print("  [MISS] " + e)
        return 1
    n_refs = sum(
        len(set(__import__("re").findall(
            r"\b_core\.([A-Za-z_][A-Za-z0-9_]*)",
            f.read_text(encoding="utf-8"))))
        for f in pkg.glob("*.py") if f.name not in ("core.py", "__init__.py"))
    print(f"== _core 引用完整性检查：通过（{n_refs} 个跨模块符号全部可达）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
