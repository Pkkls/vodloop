#!/usr/bin/env python3
"""The dashboard's JavaScript has to parse. Run:

    python3 tests/test_dashboard_js.py

163 of dashboard.py's 356 lines are JavaScript living inside Python strings.
Python never looks at them, no test touched them, and a stray bracket reaches
the browser as a blank panel with the error in a console nobody has open. It
parses today, measured 2026-09-05; nothing was keeping it that way.

This is not a test of what the panel does. It is the one gate that says the
thing it serves is syntactically a program, which is the cheapest check that
would have caught a typo before it shipped.

The pages are checked one at a time, not concatenated: each declares its own
`tok`, so joining them invents a redeclaration that does not exist in either.
"""
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "bin" / "dashboard.py"

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


NODE = shutil.which("node") or shutil.which("nodejs")
if NODE is None:
    # Skipping would leave this file green while checking nothing, which is the
    # state it was written to end.
    print("FAIL  node introuvable, la porte ne peut rien verifier")
    sys.exit(1)


def common_js(src):
    """The helper spliced into every page. Missing it would quietly shrink what
    is checked rather than fail, so the caller asserts it was found."""
    found = re.search(r'COMMON_JS\s*=\s*"""(.*?)"""', src, re.S)
    return found.group(1) if found else None


def script_blocks(src, common):
    """One entry per <script> the dashboard serves.

    Matched on a line that is exactly the tag, because the module docstring
    mentions <script> while explaining why titles are escaped, and a looser
    pattern starts there and swallows prose into the JavaScript.
    """
    lines = src.splitlines()
    out, start = [], None
    for n, line in enumerate(lines):
        if line.strip() == "<script>":
            start = n + 1
        elif "</script>" in line and start is not None:
            out.append("\n".join(lines[start:n]).replace("__COMMON__", common))
            start = None
    return out


def parses(js, tmp, name):
    path = pathlib.Path(tmp) / name
    path.write_text(js, encoding="utf-8")
    done = subprocess.run([NODE, "--check", str(path)],
                          capture_output=True, text=True, timeout=60)
    return done.returncode == 0, done.stderr.strip().splitlines()


source = DASHBOARD.read_text(encoding="utf-8")
common = common_js(source)
blocks = script_blocks(source, common or "")

print("what there is to check")
check("the shared helper is found, not silently skipped",
      common is not None and common.strip() != "")
# Both the panel and the overlay. A count that drifts means the extractor stopped
# matching the file, and a gate that finds nothing must say so rather than pass.
check("both pages are found", len(blocks) == 2, f"{len(blocks)} bloc(s)")
check("every page carries real code",
      all(b.count("\n") > 5 for b in blocks),
      str([b.count("\n") for b in blocks]))
check("the placeholder is substituted, not checked as-is",
      not any("__COMMON__" in b for b in blocks))

with tempfile.TemporaryDirectory() as tmp:
    print("node --check, one page at a time")
    for n, block in enumerate(blocks):
        ok, err = parses(block, tmp, f"page{n}.js")
        check(f"page {n} parses", ok, err[0] if err else "")

    print("the same checker on a page with a bracket removed")
    # The control. Without it every check above would also pass on a parses()
    # that had stopped running node and returned True, and this file would go on
    # reporting a healthy dashboard while checking nothing at all.
    broken = blocks[0].replace("function health(s) {", "function health(s) {{", 1)
    ok, _ = parses(broken, tmp, "broken.js")
    check("a broken page is rejected", ok is False)
    check("and the break was actually introduced", broken != blocks[0])

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
