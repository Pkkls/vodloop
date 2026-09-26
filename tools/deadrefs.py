#!/usr/bin/env python3
"""Names the tests reach for that the code no longer defines.

Written on 2026-09-22. Deleting reward_request and video_asked did not turn the
suite red, it killed it: the run reached `bot.video_asked(...)`, raised
AttributeError, and everything after that line went unchecked. A suite that
dies is worse than one that fails, because a failure names the thing and a
death names the line it happened to reach first.

Static on purpose. It reads the modules rather than importing them, so it runs
here, with no channel, no CHAN_ROOT and no server.

    python tools/deadrefs.py            # exit 1 if a test names something gone
    python tools/deadrefs.py --witness  # prove it can go red
"""
import argparse
import ast
import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# les suites, et les repertoires ou vivent les modules qu elles importent
SUITES = [(ROOT / "tests", [ROOT / "v2" / "oracle", ROOT / "bin"])]


# Python les pose sur tout module a l execution, et aucune ne s ecrit dans la
# source, donc l analyse statique ne les voit pas. Le 2026-09-26 bot.__file__,
# lu par un harnais pour verifier quel fichier il avait charge, est ressorti en
# "n existe plus": un controle qui crie au loup sur un dunder est un controle
# qu on finit par ignorer.
DUNDERS = {"__file__", "__name__", "__doc__", "__dict__", "__path__", "__spec__",
           "__loader__", "__package__", "__builtins__", "__all__", "__version__"}


def defined(path):
    """Every name a module binds at its top level."""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    names = set(DUNDERS)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    names.update(e.id for e in target.elts if isinstance(e, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.Try, ast.If)):
            # un import sous un try ou un if compte tout autant
            for inner in ast.walk(node):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    for alias in inner.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(inner, ast.Assign):
                    for target in inner.targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
    return names


def reached(path):
    """Every module.attribute a suite reads, with the line that reads it.

    Only names the suite imports and never rebinds count. test_dashboard_js.py
    holds the whole shared helper in a local called `common` and calls
    `common.strip()` on it, which is str.strip and has nothing to do with the
    module of that name. A check that cannot tell those apart cries wolf, and a
    check that cries wolf gets ignored, which is worse than not having one.

    Shadowing is read per scope, not once for the file. test_v2.py has a helper
    `def too_long_check(bot, row)`, and counting that parameter as a rebinding
    for the whole file skipped every bot.* in the suite, which is exactly the
    run that let bot.video_asked through. A parameter hides the module inside
    its own function and nowhere else.
    """
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[0])

    def bound_here(scope):
        """Names this scope binds itself, without entering the ones inside it."""
        names = set()
        args = getattr(scope, "args", None)
        if args is not None:
            for group in (args.args, args.posonlyargs, args.kwonlyargs):
                names.update(a.arg for a in group)
            for one in (args.vararg, args.kwarg):
                if one:
                    names.add(one.arg)
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            stack.extend(ast.iter_child_nodes(node))
        return names

    out = []

    def walk(node, hidden):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                walk(child, hidden | bound_here(child))
                continue
            if (isinstance(child, ast.Attribute)
                    and isinstance(child.value, ast.Name)
                    and isinstance(child.ctx, ast.Load)
                    and child.value.id in imported
                    and child.value.id not in hidden):
                out.append((child.value.id, child.attr, child.lineno))
            walk(child, hidden)

    walk(tree, bound_here(tree))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--witness", action="store_true",
                    help="pretend one suite reads a name nothing defines")
    args = ap.parse_args()

    # Deux repertoires portent un kickfetch.py, un par architecture, et les
    # suites des deux vivent cote a cote. On prend l union: un nom qui existe
    # dans l une des deux copies n est pas un nom disparu, et un nom retire des
    # deux l est vraiment. Plus laxiste, mais sans faux positif, ce qui est la
    # seule facon qu un controle serve a quelque chose.
    modules = {}
    for _, homes in SUITES:
        for home in homes:
            for path in sorted(home.glob("*.py")):
                modules.setdefault(path.stem, set()).update(defined(path))

    gone = 0
    looked = 0
    for suite_dir, _ in SUITES:
        for suite in sorted(suite_dir.glob("*.py")):
            bad = []
            uses = reached(suite)
            if args.witness and suite.name == sorted(
                    p.name for p in suite_dir.glob("*.py"))[0]:
                uses = uses + [("chan", "une_fonction_qui_n_existe_pas", 0)]
            for module, attr, line in uses:
                if module not in modules:
                    continue
                looked += 1
                if attr not in modules[module]:
                    bad.append((module, attr, line))
            if bad:
                print("%s" % suite.relative_to(ROOT))
                for module, attr, line in sorted(set(bad), key=lambda b: b[2]):
                    print("  ligne %-5s %s.%s n existe plus" % (line, module, attr))
                    gone += 1

    print("%d acces module.attribut verifies, %d introuvable(s)" % (looked, gone))
    if args.witness:
        print("temoin: chan.une_fonction_qui_n_existe_pas DOIT apparaitre ci-dessus")
    return 1 if gone else 0


if __name__ == "__main__":
    sys.exit(main())
