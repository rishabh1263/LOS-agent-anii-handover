"""
BUILD THE HANDOVER PROJECT `los_agentic/` -- agent-wise -- FROM THIS REPOSITORY (owner 2026-11-02: "ek hii zip
jisme sab proper files structure ho ... agent wise"). The repository is never changed: a new folder is written, then
zipped. Run it again after a fix here: the same project comes out with the fix.

    python scripts/build_los_agentic.py                       # build into <out> (config), check, zip
    python scripts/build_los_agentic.py --no-zip              # build only
    python scripts/build_los_agentic.py --out D:/handover     # another folder

WHAT GOES WHERE is all in scripts/los_agentic_build.yaml:
    packages    a source folder / file -> its place in the project (an agent's code, the shared platform)
    files       config, rules seeds, SQL, static pages -> their place (an agent's conf/, shared/config/)
    tests       the test files -> the tests/ folder of the agent they test
    edits       the few source lines that build a path from the repository's layout (depth, folder names)
    keep_out    never copied (secrets, uploads, caches, samples, personal files)
Every Python module name is rewritten from its repository name to its project name (app.agents.credit.bank_verification
-> agents.bank_verification), longest name first, whole names only -- in code, config and docs alike.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import sys
import zipfile
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).resolve().with_name("los_agentic_build.yaml")
TEXT = {".py", ".yaml", ".yml", ".md", ".ini", ".toml", ".txt", ".sql", ".json", ".html", ".cfg", ".example", ""}


def _dotted(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    p = p[: -len("/__init__")] if p.endswith("/__init__") else p
    return p.strip("/").replace("/", ".")


def _ignored(rel: str, patterns: list[str]) -> bool:
    parts = rel.replace("\\", "/").split("/")
    return any(fnmatch.fnmatch(rel, pat) or any(fnmatch.fnmatch(part, pat) for part in parts) for pat in patterns)


def _copy(src: Path, dst: Path, keep_out: list[str], exact: set[str] | None = None) -> list[Path]:
    """A file or a folder, without what keep_out / the item's exclude names (patterns) and without the files `exact`
    lists (paths relative to the folder). The files written."""
    written: list[Path] = []
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return [dst]
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src).as_posix()
        if f.is_dir() or _ignored(rel, keep_out) or rel in (exact or set()):
            continue
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        written.append(target)
    return written


def module_map(cfg: dict[str, Any]) -> list[tuple[str, str]]:
    """(old dotted name, new dotted name), longest first: every package / file moved, plus explicit renames."""
    pairs = {}
    for item in [*cfg["packages"], *(cfg.get("tests") or [])]:
        if item["from"].endswith(".py") or (ROOT / item["from"]).is_dir():
            pairs[_dotted(item["from"])] = _dotted(item["to"])
    # a one-word name DERIVED from a folder (`app`) is never rewritten: it is also a variable (`app.include_router`);
    # one named in `renames` is (it was written there on purpose)
    pairs = {k: v for k, v in pairs.items() if "." in k}
    pairs.update({k: v for k, v in (cfg.get("renames") or {}).items()})
    return sorted(pairs.items(), key=lambda kv: -len(kv[0]))


def rewrite(text: str, pairs: list[tuple[str, str]]) -> str:
    """Every module name (dotted) and its path form (slashed) rewritten, whole names only, in one pass."""
    table = {}
    for old, new in pairs:
        table[old] = new
        if "." in old:                                    # a one-word name has no path form of its own
            table[old.replace(".", "/")] = new.replace(".", "/")
    names = sorted(table, key=len, reverse=True)
    pattern = re.compile(r"(?<![\w.])(" + "|".join(re.escape(n) for n in names) + r")(?![\w])")
    return pattern.sub(lambda m: table[m.group(1)], text)


def _used_modules(cfg: dict[str, Any]) -> set[str]:
    """The repository modules the agents really ran (config used_modules: recorded by running their tests)."""
    path = cfg.get("used_modules")
    if not path:
        return set()
    return {line.strip() for line in (ROOT / path).read_text(encoding="utf-8").splitlines() if line.strip()}


def _unused_files(src: Path, used: set[str]) -> list[str]:
    """The .py files of a package that no agent ran (relative paths, for _copy to leave out). Package __init__ files
    of a used module, and every non-Python file (data, config), always go."""
    out = []
    for f in src.rglob("*.py"):
        dotted = _dotted(f.relative_to(ROOT).as_posix())
        if dotted in used:
            continue
        if f.name == "__init__.py" and any(u.startswith(dotted + ".") for u in used):
            continue
        out.append(f.relative_to(src).as_posix())
    return out


_PARENTS = re.compile(r"(__file__\)(?:\.resolve\(\))?\.parents\[)(\d+)(\])")


def _fix_depth(text: str, inside: int, delta: int) -> tuple[str, list[int]]:
    """Path(__file__).parents[k] in a file `inside` folders deep in an item that now sits `delta` folders shallower:
    a k reaching inside the item (k <= inside) is unchanged; one reaching above it becomes k - delta."""
    bad: list[int] = []

    def repl(m: re.Match) -> str:
        k = int(m.group(2))
        if k <= inside or not delta:
            return m.group(0)
        if k - delta < 0:
            bad.append(k)
            return m.group(0)
        return f"{m.group(1)}{k - delta}{m.group(3)}"

    return _PARENTS.sub(repl, text), bad


def build(out: Path, cfg: dict[str, Any]) -> list[str]:
    keep_out = list(cfg.get("keep_out") or [])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    written: list[Path] = []
    moved: dict[Path, tuple[int, int]] = {}             # file -> (its depth inside its item, the item's depth change)
    used = _used_modules(cfg)
    for group in ("packages", "files", "tests"):
        for item in cfg.get(group) or []:
            src = ROOT / item["from"]
            if not src.exists():
                raise SystemExit(f"{group}: {item['from']} does not exist")
            unused = set(_unused_files(src, used)) if item.get("only_used") and src.is_dir() else set()
            files = _copy(src, out / item["to"], keep_out + list(item.get("exclude") or []), unused)
            # how much shallower the item now sits (a file item: its folder)
            old_depth = len(Path(item["from"]).parts) - (0 if src.is_dir() else 1)
            new_depth = len(Path(item["to"]).parts) - (0 if src.is_dir() else 1)
            base = out / item["to"] if src.is_dir() else (out / item["to"]).parent
            for f in files:
                moved[f] = (len(f.relative_to(base).parts) - 1, old_depth - new_depth)
            written += files
    for item in cfg.get("new_files") or []:              # files the project has and the repository does not
        target = out / item["to"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
    problems: list[str] = []
    # 1. the few source lines written against the repository's layout -- edited on the repository's own text
    for rel, changes in (cfg.get("edits") or {}).items():
        f = out / rel
        if not f.is_file():
            problems.append(f"edits: {rel} was not built")
            continue
        text = f.read_text(encoding="utf-8")
        for change in changes:
            if text.count(change["old"]) != 1:
                problems.append(f"edits: {rel}: {change['old'][:60]!r} found {text.count(change['old'])} times")
                continue
            text = text.replace(change["old"], change["new"])
        f.write_text(text, encoding="utf-8")
    # 2. every module name; 3. every path a file builds from its own place (Path(__file__).parents[N])
    pairs = module_map(cfg)
    for f in written:
        if f.suffix.lower() in TEXT or f.name in {".env.example"}:
            try:
                text = f.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            new = rewrite(text, pairs)
            if f.suffix == ".py" and f in moved:
                new, bad = _fix_depth(new, *moved[f])
                problems += [f"depth: {f.relative_to(out).as_posix()}: parents[{k}] points above the project"
                             for k in bad]
            if new != text:
                f.write_text(new, encoding="utf-8")
    # each agent folder's README.md (config agent_readmes: one entry per folder)
    if cfg.get("agent_readmes"):
        for folder, a in (yaml.safe_load((ROOT / cfg["agent_readmes"]).read_text(encoding="utf-8")) or {}).items():
            if not (out / folder).is_dir():
                problems.append(f"agent_readmes: no folder {folder}")
                continue
            lines = [f"# {a['title']}", "", a["what"], "", "## Rules", "", f"In the database: {a['rules']}. "
                     "See `python scripts/rules.py list`.", "", "## Tables (its schema)", "", a["tables"], "",
                     "## Endpoints", ""] + [f"- `{e.split(' -- ')[0]}`" + (f": {e.split(' -- ')[1]}" if ' -- ' in e
                                                                          else "") for e in a["endpoints"]]
            lines += ["", "## Layout", "", "- code: this folder; `api.py` / `api/` is its API",
                      "- `conf/`: its rules seed and deployment settings", "- `tests/`: its tests "
                      "(`python -m pytest " + folder + "/tests`)", ""]
            (out / folder / "README.md").write_text("\n".join(lines), encoding="utf-8")
    # every package folder is a package
    for d in sorted({p.parent for p in out.rglob("*.py")}):
        if d != out and not (d / "__init__.py").exists() and d.name not in set(cfg.get("not_packages") or []):
            (d / "__init__.py").write_text("", encoding="utf-8")
    # nothing of the repository's names may remain in code
    left = []
    for f in out.rglob("*.py"):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.])app\.(agents|agent_db|api|store|security|core|llm|observability|mcp|knowledge|"
                         r"jev|orchestration|services|tts|approvals|config|static)\b", line):
                # at the top of a module it runs on import (an error); indented, only when that function runs
                left.append(("WARNING lazy repository import: " if line[:1].isspace() else "")
                            + f"{f.relative_to(out).as_posix()}:{n}: {line.strip()[:100]}")
    problems += [x for x in left if x.startswith("WARNING")]
    hard = [x for x in left if not x.startswith("WARNING")]
    if hard:
        problems.append(f"{len(hard)} line(s) still name a repository module:\n    " + "\n    ".join(hard[:40]))
    packages = {d.name for d in out.iterdir() if (d / "__init__.py").is_file()} | {"main"}
    problems += unresolved(out, packages)
    return problems


def unresolved(out: Path, packages: set[str]) -> list[str]:
    """Every `package.*` module imported anywhere in the project (top level or inside a function) that has no file
    there: a part of the repository the project still needs."""
    import ast

    def exists(dotted: str) -> bool:
        base = out / Path(*dotted.split("."))
        return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()

    missing: dict[str, set[str]] = {}
    for f in out.rglob("*.py"):
        rel = f.relative_to(out).as_posix()
        try:
            tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        except SyntaxError as exc:
            missing.setdefault(f"SYNTAX ERROR {exc.msg}", set()).add(rel)
            continue
        # an import at the top of a module runs when the module is imported; one inside a function only when that
        # function runs (a path the agents may never take)
        top = {id(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))}
        for block in (n for n in tree.body if isinstance(n, (ast.If, ast.Try))):
            top |= {id(n) for n in ast.walk(block) if isinstance(n, (ast.Import, ast.ImportFrom))}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                wanted = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                wanted = [node.module]
            else:
                continue
            for name in wanted:
                if name.split(".")[0] in packages and not exists(name):
                    kind = "IMPORTED AT LOAD" if id(node) in top else "imported only inside a function"
                    missing.setdefault(f"{name}  [{kind}]", set()).add(rel)
    return [f"not in the project: {m}  (used by {', '.join(sorted(u)[:3])})" for m, u in sorted(missing.items())
            if "AT LOAD" in m or "SYNTAX" in m] + \
           [f"WARNING lazy: {m}  (in {', '.join(sorted(u)[:3])})" for m, u in sorted(missing.items())
            if "AT LOAD" not in m and "SYNTAX" not in m]


def make_zip(out: Path, target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, Path(out.name) / f.relative_to(out))
                count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out")
    parser.add_argument("--no-zip", action="store_true")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    out = Path(args.out or cfg["out"]).expanduser()
    out = (out if out.is_absolute() else ROOT / out) / cfg["name"]
    problems = build(out, cfg)
    print(f"built {out}  ({sum(1 for f in out.rglob('*') if f.is_file())} files)")
    errors = [p for p in problems if not p.startswith("WARNING")]
    for p in problems:
        print(p if p.startswith("WARNING") else f"PROBLEM: {p}")
    if errors:
        return 1
    if not args.no_zip:
        target = out.parent / f"{cfg['name']}.zip"
        print(f"zip {target}  ({make_zip(out, target)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
