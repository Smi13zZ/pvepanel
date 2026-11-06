#!/usr/bin/env python3
"""check_structure.py - catch the class of bug that py_compile cannot.

py_compile only proves a file parses. The bugs that actually reached a device
in this project were all syntactically perfect:

  * a method body indented out of its class, so the class lost the method and
    the call site raised AttributeError at runtime;
  * a self.foo read that is never assigned anywhere on that class;
  * a name used but never imported or defined.

So this walks the AST and checks those three things per module. It is
deliberately conservative: it reports what it can prove is wrong, plus a short
"review" list of things it cannot decide.
"""
import ast
import builtins
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "src", "fn-pvepanel", "app", "server")

BUILTINS = set(dir(builtins)) | {
    "self", "cls", "__name__", "__file__", "__doc__", "__package__",
    "__builtins__", "__spec__", "__loader__", "True", "False", "None",
}


class ClassInfo:
    def __init__(self, node, path):
        self.node = node
        self.path = path
        self.name = node.name
        self.methods = {}
        self.attr_assign = set()
        self.attr_read = {}
        self.bases = [ast.unparse(b) for b in node.bases]


def collect(tree, path):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            ci = ClassInfo(node, path)
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    ci.methods[item.name] = item
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                        and sub.value.id == "self":
                    if isinstance(sub.ctx, ast.Store):
                        ci.attr_assign.add(sub.attr)
                    else:
                        ci.attr_read.setdefault(sub.attr, sub.lineno)
            for key in ("__init__",):
                fn = ci.methods.get(key)
                if fn is None:
                    continue
            out.append(ci)
    return out


def module_level_names(tree):
    """Names defined at module scope: functions, classes, assignments, imports."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            names |= module_level_names(ast.Module(body=node.body, type_ignores=[]))
            for h in getattr(node, "handlers", []):
                names |= module_level_names(ast.Module(body=h.body, type_ignores=[]))
            names |= module_level_names(ast.Module(body=getattr(node, "orelse", []),
                                                  type_ignores=[]))
            names |= module_level_names(ast.Module(body=getattr(node, "finalbody", []),
                                                  type_ignores=[]))
    return names


def check_file(path, module_names_seen):
    problems = []
    review = []
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as exc:
        return ["%s:%s syntax error: %s" % (path, exc.lineno, exc.msg)], []

    rel = os.path.relpath(path, ROOT)
    module_names = module_level_names(tree) | BUILTINS
    classes = collect(tree, path)
    by_name = {c.name: c for c in classes}

    # 1) a method defined at module level but named like it belongs to a class
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            parent_hint = any(node.name in c.methods for c in classes)
            if parent_hint:
                problems.append("%s:%d %r is defined at module level AND as a method "
                                "- indentation probably slipped" % (rel, node.lineno, node.name))

    # 2) self.X read on a class that never assigns X anywhere
    for ci in classes:
        assigned = set(ci.attr_assign)
        # class-level annotations / assignments also count
        for item in ci.node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                assigned.add(item.target.id)
            elif isinstance(item, ast.Assign):
                for t in item.targets:
                    if isinstance(t, ast.Name):
                        assigned.add(t.id)
        inherited = set()
        for base in ci.bases:
            base = base.split(".")[-1]
            if base in by_name:
                b = by_name[base]
                inherited |= b.attr_assign
                for item in b.node.body:
                    if isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name):
                                inherited.add(t.id)
        # A class deriving from something outside this package (BaseHTTPRequest-
        # Handler sets self.path/rfile/wfile, etc.) can legally read attributes
        # this tool cannot see assigned. Only audit classes whose every base is
        # resolvable here.
        external_base = any(b.split(".")[-1] not in by_name for b in ci.bases)
        if external_base:
            continue
        for attr, line in ci.attr_read.items():
            if attr.startswith("__"):
                continue
            if attr in assigned or attr in inherited:
                continue
            # a method name used as self.method() is fine
            if attr in ci.methods:
                continue
            if any(attr in b.methods for b in by_name.values()):
                inherited_via_method = True
            else:
                inherited_via_method = False
            if inherited_via_method:
                continue
            problems.append("%s:%d %s.%s is read but never assigned on that class"
                            % (rel, line, ci.name, attr))

    # 3) names used inside functions that are not module-level, params, locals,
    #    comprehension targets, or builtins
    def locals_of(fn):
        out = set()
        for a in fn.args.args + fn.args.kwonlyargs + fn.args.posonlyargs:
            out.add(a.arg)
        if fn.args.vararg:
            out.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            out.add(fn.args.kwarg.arg)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                out.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                out.add(node.name)
            elif isinstance(node, ast.Import):
                for al in node.names:
                    out.add((al.asname or al.name).split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for al in node.names:
                    out.add(al.asname or al.name)
            elif isinstance(node, (ast.comprehension,)):
                pass
            elif isinstance(node, ast.arg):
                out.add(node.arg)
            elif isinstance(node, ast.Global):
                out |= set(node.names)
            elif isinstance(node, ast.Nonlocal):
                out |= set(node.names)
        for node in ast.walk(fn):
            if isinstance(node, ast.comprehension):
                if isinstance(node.target, ast.Name):
                    out.add(node.target.id)
                elif isinstance(node.target, ast.Tuple):
                    for e in node.target.elts:
                        if isinstance(e, ast.Name):
                            out.add(e.id)
        return out

    # Walk functions with their enclosing scope chain. A nested closure may
    # legitimately use any name bound in a function that contains it
    # (`latest` inside on_progress() is defined in start()); flagging that would
    # bury the real findings in noise.
    def walk_fn(fn, enclosing):
        local = locals_of(fn)
        visible = enclosing | local
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in visible or sub.id in module_names:
                    continue
                problems.append("%s:%d %r used in %s() but never defined"
                                % (rel, sub.lineno, sub.id, fn.name))
        for inner in ast.walk(fn):
            if inner is fn:
                continue
            if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk_fn(inner, visible)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            walk_fn(node, set())
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # methods cannot see each other's locals, only module scope
                    walk_fn(item, set())

    # 4) things worth a human eye but not provable
    if "except:" in src:
        review.append("%s: bare 'except:' present" % rel)

    return problems, review


def main():
    files = []
    for dirpath, dirnames, filenames in os.walk(SERVER):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if fn.endswith(".py"):
                files.append(os.path.join(dirpath, fn))
    files.sort()

    all_problems = []
    all_review = []
    for path in files:
        p, r = check_file(path, set())
        all_problems += p
        all_review += r

    print("checked %d modules" % len(files))
    for f in all_problems:
        print("  FAIL  " + f)
    for f in all_review:
        print("  NOTE  " + f)
    print("")
    print("problems: %d" % len(all_problems))
    print("RESULT: " + ("FAIL" if all_problems else "PASS"))
    return 1 if all_problems else 0


if __name__ == "__main__":
    sys.exit(main())