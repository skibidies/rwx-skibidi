"""Generate single-hunk mutants of the pinned tree, in the shapes the sample
patches use, so the selector can be checked against the real distribution.

Each mutant is written as evaldata/patches/<name>.patch; the truth comes from
eval/run_one.py in the container.
"""
import ast
import difflib
import sys
from pathlib import Path

BASE = Path(sys.argv[1] if len(sys.argv) > 1 else "../mutbase")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "../evaldata/patches")
HOT = ("parser.py", "generator.py", "tokens.py", "dialects/presto.py",
       "dialects/snowflake.py", "dialects/bigquery.py", "dialects/duckdb.py",
       "dialects/tsql.py", "dialects/mysql.py", "dialects/postgres.py",
       "optimizer/qualify_columns.py", "optimizer/eliminate_joins.py",
       "expressions/core.py", "expressions/ddl.py", "typing/hive.py")


def sources():
    for rel in HOT:
        p = BASE / "sqlglot" / rel
        if p.exists():
            yield rel, p.read_text(), ast.parse(p.read_text())


def docstrings(tree):
    """ids of Constant nodes that are docstrings."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                out.add(id(body[0].value))
    return out


def where(node):
    return f"{node.lineno}:{node.col_offset}"


def edit(text, node, new, keep_end=True):
    """Replace node's source text with `new`, byte-safe."""
    lines = text.encode().splitlines(keepends=True)
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += len(line)
    a = starts[node.lineno - 1] + node.col_offset
    if keep_end and node.end_lineno:
        b = starts[node.end_lineno - 1] + node.end_col_offset
    else:
        b = a + len(node.value.encode() if hasattr(node, "value") else b"")
    out = text.encode()[:a] + new.encode() + text.encode()[b:]
    return out.decode()


def line_edit(text, lineno, new_line):
    lines = text.splitlines(keepends=True)
    lines[lineno - 1] = new_line + "\n"
    return "".join(lines)


def find(kind, rel, text, tree):
    docs = docstrings(tree)
    for node in ast.walk(tree):
        if kind == "kwbool":
            if isinstance(node, ast.keyword) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, bool):
                return node.value, str(not node.value.value)
        elif kind == "classflag":
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, ast.Assign) and isinstance(st.value, ast.Dict):
                        for v in st.value.values:
                            if isinstance(v, ast.Constant) and isinstance(v.value, bool):
                                return v, str(not v.value)
        elif kind == "classint":
            if isinstance(node, ast.ClassDef):
                for st in node.body:
                    if isinstance(st, ast.Assign) and isinstance(st.value, ast.Constant) \
                            and type(st.value.value) is int:
                        return st.value, str(st.value.value + 1)
        elif kind == "cmpop":
            if isinstance(node, ast.Compare) and len(node.ops) == 1 \
                    and isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
                line = text.splitlines()[node.lineno - 1]
                frag = line[node.col_offset:node.end_col_offset]
                if frag.count("==") == 1 and "!=" not in frag:
                    return node, frag.replace("==", "!=")
                if frag.count("!=") == 1 and "==" not in frag:
                    return node, frag.replace("!=", "==")
        elif kind == "boolop":
            if isinstance(node, ast.BoolOp):
                line = text.splitlines()[node.lineno - 1]
                frag = line[node.col_offset:node.end_col_offset]
                word = " and " if isinstance(node.op, ast.And) else " or "
                other = " or " if isinstance(node.op, ast.And) else " and "
                if word in frag:
                    return node, frag.replace(word, other, 1)
        elif kind == "argidx":
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "seq_get" and len(node.args) >= 2 \
                    and isinstance(node.args[1], ast.Constant) \
                    and node.args[1].value in (0, 1):
                return node.args[1], str(1 - node.args[1].value)
        elif kind == "strlit":
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and len(node.value) > 3 \
                    and node.end_lineno == node.lineno and node.col_offset > 4 \
                    and node.lineno > 1:
                return node, chr(34) + node.value + "x" + chr(34)
        elif kind == "dkey":
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                            and len(k.value) > 3:
                        return k, chr(34) + k.value + "x" + chr(34)
        elif kind == "localbool":
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, bool):
                for fn in ast.walk(tree):
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno) \
                            and fn not in (tree,):
                        if node.lineno > fn.lineno:
                            return node.value, str(not node.value.value)
    return None


def mkpatch(rel, old, new, name):
    rel = f"sqlglot/{rel}"
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", n=3)
    body = "".join(diff)
    if not body:
        return False
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.patch").write_text(f"diff --git a/{rel} b/{rel}\n{body}")
    print(f"{name}: {rel} {body.splitlines()[2]}")
    return True


def main():
    kinds = ["kwbool", "classflag", "classint", "cmpop", "boolop", "argidx",
             "strlit", "dkey", "localbool"]
    made = 0
    files = list(sources())
    for idx, kind in enumerate(kinds):
        order = files[idx % len(files):] + files[:idx % len(files)]
        for rel, text, tree in order:
            hit = find(kind, rel, text, tree)
            if not hit:
                continue
            node, new = hit
            if "\n" in new:
                continue
            if kind in ("kwbool", "classflag", "classint", "strlit", "dkey", "argidx"):
                new_text = edit(text, node, new)
            else:
                new_text = edit(text, node, new)
            if mkpatch(rel, text, new_text, f"m{idx:02d}{kind}"):
                made += 1
                break
    # a comment-only change must select nothing
    for rel, text, tree in list(sources())[3:]:
        line = text.splitlines()[0]
        idx = next(i for i, ln in enumerate(text.splitlines()) if ln.startswith("import "))
        new_text = "".join(
            (ln + "\n") + ("# blast radius: inert comment\n" if i == idx else "")
            for i, ln in enumerate(text.splitlines()))
        if mkpatch(rel, text, new_text, "m90comment"):
            made += 1
            break
    print(f"\n{made} mutants -> {OUT}")


if __name__ == "__main__":
    main()
