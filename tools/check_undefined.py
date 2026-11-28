# -*- coding: utf-8 -*-
"""静态检查：
1) ocleaner 包各模块经 `_core.X` 引用的符号必须在 core 命名空间；
2) AST 作用域检查 ocleaner/*.py 内是否存在未定义的裸名（F821）。

纯标准库，零依赖。CI 快级与本地均可随时运行（<1 秒）。
"""
import ast
import builtins
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_BUILTINS = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__cached__", "__loader__", "__builtins__", "__all__", "__annotations__",
    "Ellipsis",
}


def _target_names(node, out: set) -> None:
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            _target_names(elt, out)
    elif isinstance(node, ast.Starred):
        _target_names(node.value, out)


def _match_pat_names(pat, out: set) -> None:
    name = getattr(pat, "name", None)
    if isinstance(name, str) and name:
        out.add(name)
    rest = getattr(pat, "rest", None)
    if isinstance(rest, str) and rest:
        out.add(rest)
    for field in ("patterns", "kwd_patterns"):
        for sub in getattr(pat, field, None) or ():
            _match_pat_names(sub, out)
    if hasattr(pat, "pattern") and pat.pattern is not None:
        _match_pat_names(pat.pattern, out)


def _collect_local_bindings(stmts: list, is_module: bool = False) -> tuple[set, set]:
    """收集当前作用域内的显式绑定名及 global/nonlocal 声明名。"""
    bound: set[str] = set()
    nonlocals: set[str] = set()

    def walk_body(node) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            return
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp,
                             ast.DictComp, ast.GeneratorExp)):
            return
        if isinstance(node, ast.Global):
            nonlocals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            nonlocals.update(node.names)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                _target_names(t, bound)
        elif isinstance(node, ast.AugAssign):
            _target_names(node.target, bound)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None or isinstance(node.target, ast.Name):
                _target_names(node.target, bound)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            _target_names(node.target, bound)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    _target_names(item.optional_vars, bound)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, ast.NamedExpr):
            bound.add(node.target.id)
        elif hasattr(ast, "Match") and isinstance(node, ast.Match):
            for c in node.cases:
                _match_pat_names(c.pattern, bound)
        for ch in ast.iter_child_nodes(node):
            if not (is_module and isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))):
                if not isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                       ast.Lambda, ast.ListComp, ast.SetComp,
                                       ast.DictComp, ast.GeneratorExp)):
                    walk_body(ch)
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bound.add(ch.name)

    for s in stmts:
        walk_body(s)
    return bound - nonlocals, nonlocals


def check_ast_f821(src: str, rel: str) -> list[str]:
    """检查单个 Python 文件中所有未定义的裸名 Load 引用。"""
    tree = ast.parse(src, filename=rel)
    future_ann = any(
        isinstance(n, ast.ImportFrom) and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )
    errs: list[str] = []

    def check_expr(node, env: set, defer_ann: bool = False) -> None:
        if node is None:
            return
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load) and node.id not in env:
                errs.append(f"{rel}:{node.lineno}: 未定义名称 '{node.id}' (F821)")
            return
        if isinstance(node, ast.Lambda):
            pnames = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            if node.args.vararg:
                pnames.add(node.args.vararg.arg)
            if node.args.kwarg:
                pnames.add(node.args.kwarg.arg)
            for d in (*node.args.defaults, *(x for x in node.args.kw_defaults if x is not None)):
                check_expr(d, env)
            check_expr(node.body, env | pnames)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            c_env = set(env)
            for idx, gen in enumerate(node.generators):
                check_expr(gen.iter, env if idx == 0 else c_env)
                _target_names(gen.target, c_env)
                for cond in gen.ifs:
                    check_expr(cond, c_env)
            if isinstance(node, ast.DictComp):
                check_expr(node.key, c_env)
                check_expr(node.value, c_env)
            else:
                check_expr(node.elt, c_env)
            return
        if isinstance(node, ast.NamedExpr):
            check_expr(node.value, env)
            return
        for field, val in ast.iter_fields(node):
            if defer_ann and field == "annotation":
                continue
            if isinstance(val, ast.AST):
                check_expr(val, env, defer_ann)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, ast.AST):
                        check_expr(item, env, defer_ann)

    def check_stmts(stmts: list, outer_env: set, is_module: bool = False) -> None:
        bound, _ = _collect_local_bindings(stmts, is_module=is_module)
        cur_env = outer_env | bound
        for n in stmts:
            check_stmt(n, outer_env, cur_env)

    def check_stmt(node, outer_env: set, cur_env: set) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                check_expr(dec, cur_env)
            for d in (*node.args.defaults, *(x for x in node.args.kw_defaults if x is not None)):
                check_expr(d, cur_env)
            if not future_ann:
                for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                    check_expr(a.annotation, cur_env)
                for opt_a in (node.args.vararg, node.args.kwarg):
                    if opt_a is not None:
                        check_expr(opt_a.annotation, cur_env)
                check_expr(node.returns, cur_env)
            pnames = {a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)}
            if node.args.vararg:
                pnames.add(node.args.vararg.arg)
            if node.args.kwarg:
                pnames.add(node.args.kwarg.arg)
            check_stmts(node.body, cur_env | pnames)
            return
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                check_expr(dec, cur_env)
            for b in (*node.bases, *node.keywords):
                check_expr(b, cur_env)
            check_stmts(node.body, cur_env)
            return
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
            return
        # 语句体（含 if/for/while/with/try）共享当前 cur_env，递归检查内部语句与表达式
        for field, val in ast.iter_fields(node):
            if future_ann and field == "annotation":
                continue
            if isinstance(val, ast.AST):
                if isinstance(val, ast.stmt):
                    check_stmt(val, outer_env, cur_env)
                else:
                    check_expr(val, cur_env, future_ann)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, ast.stmt):
                        check_stmt(item, outer_env, cur_env)
                    elif isinstance(item, ast.AST):
                        check_expr(item, cur_env, future_ann)

    check_stmts(tree.body, _BUILTINS, is_module=True)
    return errs


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
        rel = f"ocleaner/{f.name}"
        src = f.read_text(encoding="utf-8")
        errors.extend(check_ast_f821(src, rel))
        if f.name in ("core.py", "__init__.py"):
            continue
        for m in sorted(set(re.findall(r"\b_core\.([A-Za-z_][A-Za-z0-9_]*)", src))
                        if (re := __import__("re")) else []):
            if m not in ns:
                errors.append(f"{rel}: _core.{m} 不在 core 命名空间"
                              f"（符号被移除或未 re-export）")
    if errors:
        print("== _core 引用完整性 + AST 作用域检查：失败 ==")
        for e in errors:
            print("  [MISS] " + e)
        return 1
    n_refs = sum(
        len(set(__import__("re").findall(
            r"\b_core\.([A-Za-z_][A-Za-z0-9_]*)",
            f.read_text(encoding="utf-8"))))
        for f in pkg.glob("*.py") if f.name not in ("core.py", "__init__.py"))
    print(f"== _core 引用完整性 + AST F821 检查：通过（{n_refs} 个跨模块符号全部可达）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
