"""Static maps from the pinned sqlglot checkout in /src.

/maps/imports.json   {"tests/x.py": ["sqlglot/y.py", ...]}  files that must import
                     cleanly for the test file to import (transitive closure).
/maps/fixtures.json  {"tests/fixtures/optimizer/simplify.sql": [<test number>, ...]}
/maps/scopes.json    {"sqlglot/x.py": [["f"|"c", start, end], ...]}  f=function,
                     c=class. Lines outside every span are module level.
"""

import ast
import gzip
import json
import os
from collections import defaultdict

SRC = "/src"
OUT = "/maps"
TESTS_JSON = "/mapbuilder/tests.json"

SKIP_DIRS = {".git", "benchmarks", "pdoc", "posts", "sqlglot-integration-tests", "node_modules"}


def py_files():
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(".py"):
                yield os.path.relpath(os.path.join(root, f), SRC)


def module_of(rel):
    return rel[:-3].replace(os.sep, ".").removesuffix(".__init__")


def file_of(module):
    p = module.replace(".", "/")
    for cand in (p + ".py", p + "/__init__.py"):
        if os.path.exists(os.path.join(SRC, cand)):
            return cand
    return None


def parse(rel):
    try:
        with open(os.path.join(SRC, rel), encoding="utf-8") as fh:
            return ast.parse(fh.read(), rel)
    except (SyntaxError, ValueError):
        return None


def imports_of(tree, rel):
    pkg = os.path.dirname(rel)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                f = file_of(alias.name)
                if f:
                    out.add(f)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = os.path.dirname(base)
                base_mod = module_of(base + "/x")[:-2] if base else ""
                m = f"{base_mod}.{node.module}" if base_mod and node.module else (node.module or base_mod)
                targets = [f"{m}.{a.name}" if m else a.name for a in node.names]
            else:
                m = node.module or ""
                f = file_of(m)
                if f:
                    out.add(f)
                targets = [f"{m}.{a.name}" if m else a.name for a in node.names]
            if m:
                f = file_of(m)
                if f:
                    out.add(f)
            for t in targets:
                f = file_of(t)
                if f:
                    out.add(f)
    return out


def string_fragments(node):
    """Every string constant inside a subtree, plus f-string/binop fragments."""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.add(n.value)
    return out


def main():
    data = json.load(open(TESTS_JSON))
    tests = data["tests"]

    graph = {}
    trees = {}
    for rel in py_files():
        tree = parse(rel)
        if tree is None:
            continue
        trees[rel] = tree
        graph[rel] = {f for f in imports_of(tree, rel) if f != rel}

    def closure(rel):
        seen = set()
        stack = [rel]
        while stack:
            cur = stack.pop()
            for nxt in graph.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    imports_map = {rel: sorted(closure(rel)) for rel in sorted(trees)}

    scopes = {}
    for rel, tree in trees.items():
        if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            continue
        spans = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body_start = node.body[0].lineno if node.body else node.lineno
                spans.append(["f", node.lineno, getattr(node, "end_lineno", node.lineno), body_start])
            elif isinstance(node, ast.Lambda):
                # lambda bodies run at call time, not at import
                spans.append(["f", node.lineno, getattr(node, "end_lineno", node.lineno), node.lineno])
            elif isinstance(node, ast.ClassDef):
                spans.append(["c", node.lineno, getattr(node, "end_lineno", node.lineno), node.lineno])
            elif isinstance(node, ast.expr):
                s = getattr(node, "lineno", 0)
                e = getattr(node, "end_lineno", s)
                if s and e and e > s:
                    spans.append(["e", s, e, s])  # multi-line expression
        if spans:
            scopes[rel] = sorted(spans, key=lambda s: (s[1], -s[2]))

    # ---- reference map: names/strings assigned at module or class level
    # -> lines anywhere in the tree that mention them. For a changed
    # constant, the tests that can notice are the ones covering its readers.
    targets = set()
    for rel, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.Module)):
                for sub in node.body if isinstance(node, ast.ClassDef) else node.body:
                    if isinstance(sub, ast.Assign):
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                targets.add(t.id)
                        if isinstance(sub.value, ast.Dict):
                            for k in sub.value.keys:
                                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                                    targets.add(k.value)
    refs = defaultdict(lambda: defaultdict(list))
    for rel, tree in trees.items():
        if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            continue
        for node in ast.walk(tree):
            hit = None
            if isinstance(node, ast.Name) and node.id in targets:
                hit = node.id
            elif isinstance(node, ast.Attribute) and node.attr in targets:
                hit = node.attr  # dialect.WEEK_OFFSET reads
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in targets:
                hit = node.value
            if hit:
                refs[hit][rel].append(node.lineno)
    refs_json = {k: {f: sorted(set(v)) for f, v in m.items()} for k, m in refs.items()}

    # ---- value-flow map: a changed literal value is noticed only where the
    # value is read; plain handoffs (assignments, keyword arguments) just
    # carry it to another name. obs: name -> lines that READ it;
    # flows: name -> names that receive its value.
    obs = defaultdict(lambda: defaultdict(list))
    flows = defaultdict(set)

    def chain_names(node, acc):
        """Names in a pure value chain: no calls, indexing or operators."""
        if isinstance(node, ast.Name):
            acc.add(node.id)
            return True
        if isinstance(node, ast.Attribute):
            acc.add(node.attr)
            return chain_names(node.value, acc)
        if isinstance(node, ast.Constant):
            return True
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            return all(chain_names(e, acc) for e in node.elts)
        if isinstance(node, ast.Dict):
            return (all(chain_names(k, acc) for k in node.keys if k is not None) and
                    all(chain_names(v, acc) for v in node.values))
        return False

    def chain_positions(node, acc):
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Name, ast.Attribute)):
                acc.add((sub.lineno, sub.col_offset))
        return acc

    def simple_target(node, acc):
        if isinstance(node, ast.Name):
            acc.add(node.id)
            return True
        if isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                acc.add(node.attr)
                return True
            return False
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(simple_target(e, acc) for e in node.elts)
        return False

    for rel, tree in trees.items():
        if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            continue
        transparent = set()  # (lineno, col) of occurrences that only carry a value
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                vnames = set()
                tnames = set()
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                ok_targets = all(simple_target(t, tnames) for t in targets)
                if node.value is None:
                    for t in targets:
                        chain_positions(t, transparent)
                elif chain_names(node.value, vnames) and ok_targets:
                    for v in vnames:
                        flows[v] |= tnames
                    chain_positions(node.value, transparent)
                    for t in targets:
                        chain_positions(t, transparent)
            elif isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg is None:
                        continue
                    vnames = set()
                    if chain_names(kw.value, vnames):
                        for v in vnames:
                            flows[v].add(kw.arg)
                        chain_positions(kw.value, transparent)
        for node in ast.walk(tree):
            hit = None
            if isinstance(node, ast.Name):
                hit = node.id
            elif isinstance(node, ast.Attribute):
                hit = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                hit = node.value
            if hit and (node.lineno, node.col_offset) not in transparent:
                obs[hit][rel].append(node.lineno)

    obs_json = {k: {f: sorted(set(v)) for f, v in m.items()} for k, m in obs.items()}
    flows_json = {k: sorted(v) for k, v in flows.items()}

    # ---- fixture -> tests
    fixture_root = os.path.join(SRC, "tests", "fixtures")
    fixture_files = []
    for root, _dirs, files in os.walk(fixture_root):
        for f in sorted(files):
            fixture_files.append(os.path.relpath(os.path.join(root, f), fixture_root))

    fixtures_map = defaultdict(set)
    for t in tests:
        rel = t["id"].split("::")[0]
        tree = trees.get(rel)
        if tree is None:
            continue
        # module-level / class-level simple string assignments (e.g. DIR_TPCH)
        consts = defaultdict(set)
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                consts[n.targets[0].id] |= string_fragments(n.value)
        # strings behind Names used in a test's enclosing class (setup code)
        class_frags = defaultdict(set)
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for sub in ast.walk(n):
                    if isinstance(sub, ast.Name) and sub.id in consts:
                        class_frags[n.name] |= consts[sub.id]
        parts = t["id"].split("::")[1:]
        if len(parts) > 1 and parts[0] in class_frags:
            pass  # added below after node resolution
        node = tree
        for part in parts:
            found = None
            for n in ast.walk(node):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == part:
                    found = n
                    break
            node = found
            if node is None:
                break
        if node is None:
            node = tree
        strings = string_fragments(node)
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id in consts:
                strings |= consts[n.id]
        if len(parts) > 1:
            strings |= class_frags.get(parts[0], set())
        strings = {s for s in strings if len(s) >= 3}
        for fx in fixture_files:
            hit = False
            base = fx.rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            for s in strings:
                norm = s.split("fixtures/", 1)[1] if "fixtures/" in s else s.lstrip("/")
                if fx == norm or fx == norm.rstrip("/") + "/" + base:
                    hit = True
                elif "/" not in norm and norm == stem and fx == f"optimizer/{norm}.sql":
                    hit = True  # check_file("simplify") -> optimizer/simplify.sql
                elif "/" not in norm and norm == base:
                    hit = True
                elif norm.endswith("/") and fx.startswith(norm):
                    hit = True
                if hit:
                    break
            if hit:
                fixtures_map[fx].add(t["number"])

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "imports.json"), "w") as fh:
        json.dump(imports_map, fh, separators=(",", ":"))
    with open(os.path.join(OUT, "fixtures.json"), "w") as fh:
        json.dump({k: sorted(v) for k, v in sorted(fixtures_map.items())}, fh, separators=(",", ":"))
    with open(os.path.join(OUT, "scopes.json"), "w") as fh:
        json.dump(scopes, fh, separators=(",", ":"))
    with open(os.path.join(OUT, "refs.json"), "w") as fh:
        json.dump(refs_json, fh, separators=(",", ":"))
    with gzip.open(os.path.join(OUT, "obs.json.gz"), "wt") as fh:
        json.dump(obs_json, fh, separators=(",", ":"))
    with open(os.path.join(OUT, "flows.json"), "w") as fh:
        json.dump(flows_json, fh, separators=(",", ":"))
    for name in ("imports.json", "fixtures.json", "scopes.json", "refs.json", "flows.json"):
        print(name, os.path.getsize(os.path.join(OUT, name)), "bytes")
    print("obs.json.gz", os.path.getsize(os.path.join(OUT, "obs.json.gz")), "bytes")
    mapped = sum(1 for fx in fixture_files if fixtures_map.get(fx))
    print(f"fixture files mapped: {mapped} / {len(fixture_files)}")
    unmapped = [fx for fx in fixture_files if not fixtures_map.get(fx)]
    print("unmapped:", unmapped[:30])


if __name__ == "__main__":
    main()