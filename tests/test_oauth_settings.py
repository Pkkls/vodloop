#!/usr/bin/env python3
"""Settings lookup. Run: python3 tests/test_oauth_settings.py

The failure this covers made no noise. The client id and secret live in bus.env,
which systemd hands to the chat service as an EnvironmentFile, so every service
worked. oauth.py read only .env, so the same commands run by hand built an
authorisation URL with an empty client_id and exited 0. A dead link, no error.

So the load-bearing check is not "setting() returns something", it is "setting()
finds a key that exists only in bus.env". The control below reads .env alone and
asserts it does NOT find that key: without it, the first check could pass on a
lookup that never reached bus.env at all.
"""
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import common  # noqa: E402
import oauth  # noqa: E402

failures = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{'  ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    root = pathlib.Path(tmp)
    (root / ".env").write_text(
        "KICK_SLUG=cx247-cx\nSHARED=from_dot_env\n", encoding="utf-8")
    (root / "bus.env").write_text(
        "KICK_CLIENT_ID=01M1J4SA52Y0WPMTH39F1YJSAM\nSHARED=from_bus_env\n",
        encoding="utf-8")

    real_root, common.ROOT = common.ROOT, root
    for name in ("KICK_CLIENT_ID", "SHARED", "KICK_SLUG", "ABSENT_EVERYWHERE"):
        os.environ.pop(name, None)
    try:
        print("file split")
        check("a key living only in bus.env is found",
              oauth.setting("KICK_CLIENT_ID") == "01M1J4SA52Y0WPMTH39F1YJSAM")
        check("a key living only in .env is still found",
              oauth.setting("KICK_SLUG") == "cx247-cx")

        # the control: .env alone must not see the bus.env key, otherwise the
        # check above would pass without the second file ever being read
        check("reading .env alone does not see the bus.env key",
              common.env().get("KICK_CLIENT_ID") is None)
        check("reading bus.env alone does see it",
              common.env("bus.env").get("KICK_CLIENT_ID") ==
              "01M1J4SA52Y0WPMTH39F1YJSAM")

        print("precedence")
        check(".env wins over bus.env on a shared key",
              oauth.setting("SHARED") == "from_dot_env")
        os.environ["SHARED"] = "from_process_env"
        check("the process environment wins over both, as systemd relies on",
              oauth.setting("SHARED") == "from_process_env")
        os.environ.pop("SHARED", None)

        print("absent")
        check("a key in neither file returns the empty string, not None",
              oauth.setting("ABSENT_EVERYWHERE") == "")

        print("existing callers")
        check("env() with no argument still reads .env",
              common.env().get("KICK_SLUG") == "cx247-cx")
        common.ROOT = root / "does_not_exist"
        check("a missing file is empty rather than an exception", common.env() == {})
    finally:
        common.ROOT = real_root
        os.environ.pop("SHARED", None)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
