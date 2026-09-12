#!/usr/bin/env python3
"""The panel's prep restart, over a real socket.
Run: python3 tests/test_dashboard_prep_restart.py

The panel cannot restart anything itself and must not try. harden-oracle.sh
gives vodloop-web NoNewPrivileges=yes, which is exactly what stops a setuid
binary elevating, so a handler that shelled out to "sudo vodloopctl" would work
on a box the hardening pass has not reached and break silently the day it does.
So the handler drops a request into state/, which is in that unit's
ReadWritePaths, and medic carries it out from cron where there is no sandbox.

Served over a loopback socket rather than by calling the handler directly,
because the thing most worth proving is the token gate, and a gate is only
proved by a request that does not carry one.
"""
import ast
import http.server
import json
import pathlib
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import common  # noqa: E402

root = pathlib.Path(tempfile.mkdtemp(prefix="dashboard-prep-"))
common.STATE = root / "state"
common.PREP_RESTART_REQUEST = common.STATE / "prep_restart_request"

import dashboard  # noqa: E402

dashboard.common.STATE = common.STATE
dashboard.common.PREP_RESTART_REQUEST = common.PREP_RESTART_REQUEST
dashboard.TOKEN = "un-jeton-pour-le-test"

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), dashboard.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"


def post(path, token=None):
    request = urllib.request.Request(base + path, data=b"{}", method="POST")
    request.add_header("Content-Type", "application/json")
    if token is not None:
        request.add_header("X-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, json.loads(answer.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


try:
    print("the gate")
    code, body = post("/api/prep-restart")
    check("no token is refused", code == 401, f"{code} {body}")
    check("and nothing was written", not common.PREP_RESTART_REQUEST.exists())

    code, body = post("/api/prep-restart", token="pas le bon jeton")
    check("a wrong token is refused", code == 401, f"{code} {body}")
    check("and still nothing was written",
          not common.PREP_RESTART_REQUEST.exists())

    print("the request")
    code, body = post("/api/prep-restart", token=dashboard.TOKEN)
    check("the right token is accepted", code == 200, f"{code} {body}")
    check("the request lands where medic looks",
          common.PREP_RESTART_REQUEST.exists())
    # medic acts on its own tick, so the panel must not claim the restart has
    # happened. Someone reading "prep relance" and seeing nothing change for
    # four minutes goes looking for a fault that is not there.
    check("and it is worded as a request, not as a restart",
          "demande" in (body.get("note") or ""), str(body))

    print("what it must never do")
    # The whole reason this is a dropped file rather than a command. Asked of
    # the text, this question answers itself wrong: the word "sudo" appears in
    # the comment explaining why there is no sudo. So it is asked of the program.
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "bin" / "dashboard.py").read_text(encoding="utf-8")
    tree = ast.parse(source)


    def spawning(parsed):
        """Every way this module could start a process, as the parser sees it."""
        found = set()
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names} & SPAWNERS
            elif isinstance(node, ast.ImportFrom) and node.module:
                found |= {node.module.split(".")[0]} & SPAWNERS
            elif isinstance(node, ast.Attribute) and node.attr in SPAWN_CALLS:
                found.add(node.attr)
        return found


    SPAWNERS = {"subprocess", "pty", "multiprocessing"}
    SPAWN_CALLS = {"system", "popen", "fork", "execv", "execve", "execvp",
                   "spawnv", "spawnl", "run", "Popen", "check_output", "call"}
    check("the panel cannot start a process at all", not spawning(tree),
          str(sorted(spawning(tree))))

    # The control. Without it the check above also passes on a spawning() that
    # has quietly stopped looking, and this file would report a panel that keeps
    # its hands clean while checking nothing.
    check("and the same question catches one that could",
          spawning(ast.parse("import subprocess\nsubprocess.run(['sudo', 'x'])\n"))
          == {"subprocess", "run"})
finally:
    server.shutdown()

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
