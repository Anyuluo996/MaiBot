#!/usr/bin/env python3
"""静态检查插件 BLOCKING HookHandler 的 modified_kwargs 是否带回全部原始 kwargs。

背景（2026-10-21 prompt-guard / 2026-10-27 ov-memory 两次真实事故）：
宿主 hook_dispatcher 对 blocking 钩子的 modified_kwargs 是【整体替换 + 链式传递】，
任何订阅者返回缺键的 modified_kwargs，都会让排在它后面的钩子收到缺键载荷
（典型症状：下游钩子拿到 session=(空)、items 丢失后静默放行，零日志零报错）。

规则（对每个 mode=HookMode.BLOCKING 且返回 modified_kwargs 的处理函数）：
  1. modified_kwargs 的构造必须包含原始 **kwargs（dict(kwargs) / {**kwargs, ...}）
  2. 函数签名具名参数吸收的键（items / session_id / ...）必须显式放回
     —— 具名参数不会进入 **kwargs，dict(kwargs) 也带不回它们（ov-memory 二次踩坑）
不返回 modified_kwargs 的钩子不受限（放行不影响链条，是推荐写法）。

用法：
  python scripts/check_blocking_hook_kwargs.py [路径 ...]
  # 默认扫描 ./plugins 下全部插件；参数可为目录或单个 .py 文件
  # 发现问题退出码 1，干净退出码 0；纯 stdlib，可直接 scp 到服务器对生产插件跑

注意：静态分析按「合并函数内同名变量的全部赋值路径」从宽判定（宁漏报不误报），
无法覆盖运行时动态构造，不能完全替代人工审查。
"""

import ast
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# 表达式求值结果：
# has_kwargs: True=含原始 kwargs 展开 / False=明确不含 / None=无法静态判定
# keys: 显式覆盖的字面键集合
# has_dynamic_key: 存在非字面量下标键（如 modified[payload_key] = ...）
EvalResult = Tuple[Optional[bool], Set[str], bool]

MAX_RESOLVE_DEPTH = 5


def _is_blocking(mode_node: ast.expr) -> bool:
    """识别 mode=HookMode.BLOCKING / mode="BLOCKING" / mode=BLOCKING。"""

    if isinstance(mode_node, ast.Attribute):
        return mode_node.attr == "BLOCKING"
    if isinstance(mode_node, ast.Constant):
        return mode_node.value == "BLOCKING"
    if isinstance(mode_node, ast.Name):
        return mode_node.id == "BLOCKING"
    return False


def _parse_hook_decorator(decorator: ast.expr) -> Optional[Tuple[str, str]]:
    """从装饰器表达式解析 (事件名, blocking标记)；非 HookHandler 装饰器返回 None。"""

    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    is_hook_handler = (
        isinstance(func, ast.Name) and func.id == "HookHandler"
    ) or (
        isinstance(func, ast.Attribute) and func.attr == "HookHandler"
    )
    if not is_hook_handler:
        return None
    event_name = ""
    if decorator.args and isinstance(decorator.args[0], ast.Constant):
        event_name = str(decorator.args[0].value)
    blocking = False
    for kw in decorator.keywords:
        if kw.arg == "mode" and _is_blocking(kw.value):
            blocking = True
    return event_name, ("BLOCKING" if blocking else "OTHER")


def _named_params(node: ast.FunctionDef) -> List[str]:
    """签名中会吸收调用方传参的具名参数（排除 self / *args / **kwargs）。"""

    a = node.args
    names = [p.arg for p in (a.posonlyargs + a.args + a.kwonlyargs)]
    return [n for n in names if n != "self"]


def _iter_function_body(node: ast.FunctionDef) -> List[ast.stmt]:
    """遍历函数体语句（不进入嵌套函数/lambda，避免把内层作用域算进来）。"""

    collected: List[ast.stmt] = []

    def walk(stmts: List[ast.stmt]) -> None:
        for stmt in stmts:
            collected.append(stmt)
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    continue
                if isinstance(child, ast.stmt):
                    walk([child])
                elif isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.With)):
                    walk([c for c in ast.iter_child_nodes(child) if isinstance(c, ast.stmt)])

    walk(node.body)
    return collected


class _FuncScope:
    """函数内变量赋值与下标赋值的收集结果。"""

    def __init__(self, kwarg_name: str) -> None:
        # 注意：**kwargs 在 ast 里是 arguments.kwarg（vararg 是 *args）
        self.kwarg_name = kwarg_name
        self.assigns: Dict[str, List[ast.expr]] = {}
        self.subscript_keys: Dict[str, Set[str]] = {}
        self.dynamic_subscript: Dict[str, bool] = {}


def _collect_scope(node: ast.FunctionDef) -> _FuncScope:
    scope = _FuncScope(kwarg_name=node.args.kwarg.arg if node.args.kwarg else "")
    for stmt in _iter_function_body(node):
        targets: List[Tuple[str, ast.expr]] = []
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    targets.append((t.id, stmt.value))
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, ast.Name):
                targets.append((stmt.target.id, stmt.value))
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            # x |= {...} 之类按未知处理，不强解
            scope.assigns.setdefault(stmt.target.id, []).append(None)
        for name, value in targets:
            scope.assigns.setdefault(name, []).append(value)
        # 下标赋值 x["k"] = v / x[var] = v
        sub_targets: List[Tuple[str, ast.expr]] = []
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                    sub_targets.append((t.value.id, t.slice))
        for name, slice_expr in sub_targets:
            if isinstance(slice_expr, ast.Constant) and isinstance(slice_expr.value, str):
                scope.subscript_keys.setdefault(name, set()).add(slice_expr.value)
            else:
                scope.dynamic_subscript[name] = True
    return scope


def _merge(results: List[EvalResult]) -> EvalResult:
    """合并多个赋值路径：kwargs 展开任一路径成立即成立，未知优先于明确不含。"""

    has = False
    unknown = False
    keys: Set[str] = set()
    dyn = False
    for h, k, d in results:
        if h is True:
            has = True
        elif h is None:
            unknown = True
        keys |= k
        dyn = dyn or d
    if has:
        merged: Optional[bool] = True
    elif unknown:
        merged = None
    else:
        merged = False
    return merged, keys, dyn


def _eval_expr(node: Optional[ast.expr], scope: _FuncScope, depth: int = 0) -> EvalResult:
    """求表达式对「原始 kwargs」的覆盖情况，未知构造返回 (None, ...)。"""

    if node is None or depth > MAX_RESOLVE_DEPTH:
        return (None, set(), False)
    if isinstance(node, ast.Dict):
        has: Optional[bool] = False
        keys: Set[str] = set()
        dyn = False
        parts: List[EvalResult] = []
        for k, v in zip(node.keys, node.values):
            if k is None:  # **spread
                parts.append(_eval_expr(v, scope, depth + 1))
            elif isinstance(k, ast.Constant) and isinstance(k.value, str):
                keys.add(k.value)
        if not parts:
            return (False, keys, dyn)
        merged = _merge(parts)
        return (merged[0], merged[1] | keys, merged[2] or dyn)
    if isinstance(node, ast.Name):
        if scope.kwarg_name and node.id == scope.kwarg_name:
            return (True, set(), False)
        if node.id in scope.assigns:
            results = [_eval_expr(v, scope, depth + 1) for v in scope.assigns[node.id]]
            merged = _merge(results)
            keys = merged[1] | scope.subscript_keys.get(node.id, set())
            dyn = merged[2] or scope.dynamic_subscript.get(node.id, False)
            return (merged[0], keys, dyn)
        return (None, set(), False)
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id == "dict":
            parts: List[EvalResult] = []
            for arg in node.args:
                parts.append(_eval_expr(arg, scope, depth + 1))
            for kw in node.keywords:
                if kw.arg is None:  # dict(**kwargs)
                    parts.append(_eval_expr(kw.value, scope, depth + 1))
            if parts:
                return _merge(parts)
        return (None, set(), False)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _merge([_eval_expr(node.left, scope, depth + 1), _eval_expr(node.right, scope, depth + 1)])
    return (None, set(), False)


def _returns_with_modified_kwargs(node: ast.FunctionDef) -> List[Tuple[int, ast.expr]]:
    """返回 [(行号, modified_kwargs 表达式)]；不含 modified_kwargs 的 return 忽略。"""

    out: List[Tuple[int, ast.expr]] = []
    for stmt in _iter_function_body(node):
        if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict):
            for k, v in zip(stmt.value.keys, stmt.value.values):
                if (
                    isinstance(k, ast.Constant)
                    and k.value == "modified_kwargs"
                    and v is not None
                ):
                    out.append((stmt.lineno, v))
    return out


def check_file(path: Path) -> List[str]:
    """检查单个文件，返回人类可读的问题列表（空列表=通过）。"""

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        return [f"{path}: WARN: 解析失败，跳过检查（{exc}）"]

    problems: List[str] = []
    hooks_total = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            parsed = _parse_hook_decorator(decorator)
            if parsed is None:
                continue
            event_name, mode = parsed
            if mode != "BLOCKING":
                continue
            hooks_total += 1
            returns = _returns_with_modified_kwargs(node)
            if not returns:
                continue  # 不动 kwargs 的 blocking 钩子是推荐写法
            named = _named_params(node)
            scope = _collect_scope(node)
            for lineno, expr in returns:
                has_kwargs, keys, dyn = _eval_expr(expr, scope)
                label = f"{path}:{lineno} [{node.name} @ {event_name or '?'}]"
                if has_kwargs is False:
                    problems.append(
                        f"{label}\n"
                        f"  ERROR: modified_kwargs 构造不含原始 **kwargs（宿主为整体替换，"
                        f"缺键将毒化链上后续钩子）"
                    )
                elif has_kwargs is None:
                    problems.append(
                        f"{label}\n"
                        f"  WARN: 无法静态确认 modified_kwargs 含原始 **kwargs，请人工核对"
                    )
                missing = [n for n in named if n not in keys]
                if missing:
                    severity = "WARN" if dyn else "ERROR"
                    problems.append(
                        f"{label}\n"
                        f"  {severity}: 具名参数 {missing} 被签名吸收、不会进入 **kwargs，"
                        f"必须显式放回 modified_kwargs（如 {{\"items\": items, \"session_id\": session_id, **kwargs}}）"
                    )
    if hooks_total == 0:
        return []
    return problems


def collect_files(paths: List[str]) -> List[Path]:
    files: List[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(
                f for f in p.rglob("*.py")
                if "__pycache__" not in f.parts and ".git" not in f.parts
            )
    return sorted(set(files))


def main(argv: List[str]) -> int:
    targets = argv[1:] or ["plugins"]
    files = collect_files(targets)
    if not files:
        print("未找到待检查的 .py 文件")
        return 2
    all_problems: List[str] = []
    checked = 0
    for f in files:
        problems = check_file(f)
        if problems and any("ERROR" in p or "WARN" in p for p in problems):
            all_problems.extend(problems)
        checked += 1
    print(f"已检查 {checked} 个文件")
    if all_problems:
        errors = sum(1 for p in all_problems if "ERROR" in p)
        warns = sum(1 for p in all_problems if "WARN" in p)
        for p in all_problems:
            print(f"\n{p}")
        print(f"\n结论: {errors} 个 ERROR / {warns} 个 WARN")
        return 1 if errors else 0
    print("结论: 全部 BLOCKING 钩子的 modified_kwargs 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
