#!/usr/bin/env python3
"""Is the channel actually on air? Run: python3 bin/live.py

    exit 0  live
    exit 1  offline, and the API said so
    exit 2  no answer: the API refused, which is not the same thing

That third case is the whole reason this file exists. The first version read
`livestream` straight off the response body, so on 2026-09-06 a 404 came back,
the key was simply absent, and it printed "HORS LIGNE" for a channel that was
pushing 3377 kbps at that exact moment. It sent me looking for an outage that
was not there.

The second signal is here for the same reason. When the API will not answer,
whether bytes are leaving the pusher still can, and it is the more direct
question anyway: Kick's opinion is downstream of the socket.
"""
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import common


def api():
    try:
        from curl_cffi import requests
    except ImportError:
        return 2, "curl_cffi absent"
    try:
        r = requests.get(
            f"https://kick.com/api/v2/channels/{common.channel_slug()}",
                         impersonate="chrome", timeout=25)
    except Exception as exc:                                  # noqa: BLE001
        return 2, f"{type(exc).__name__}"
    if r.status_code != 200:
        return 2, f"http {r.status_code}"
    stream = (r.json() or {}).get("livestream")
    if not stream:
        return 1, "livestream=null sur une reponse 200"
    return 0, (f"viewers={stream.get('viewer_count')} "
               f"depuis {stream.get('start_time')}")


def pusher_bytes(seconds=10):
    """Bytes the pusher put on the wire, or None if it cannot be measured.

    None is not zero. A missing process and an unreadable counter both mean
    "no measurement", and calling either of them silence is how the first
    version of this got it wrong.
    """
    try:
        pid = subprocess.run(["pgrep", "-f", "ffmpeg.*rtmps"],
                             capture_output=True, text=True).stdout.split()
        if not pid:
            return None
        want = f"pid={pid[0]},"

        def sent():
            """bytes_sent for the pusher's own socket.

            ss prints the socket on one line and its counters on the next, so
            the pid has to be matched on the header and the number read from
            the line after it. Taking the first bytes_sent in the output
            instead read whichever connection happened to be listed first and
            reported a live pusher as silent.
            """
            out = subprocess.run(["ss", "-tnip"], capture_output=True, text=True).stdout
            lines = out.splitlines()
            for n, line in enumerate(lines):
                if want not in line:
                    continue
                for follow in lines[n:n + 3]:
                    for token in follow.split():
                        if token.startswith("bytes_sent:"):
                            return int(token.split(":")[1])
            return None
        first = sent()
        if first is None:
            return None
        time.sleep(seconds)
        second = sent()
        return None if second is None else max(0, second - first)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


code, detail = api()
print({0: "EN LIGNE", 1: "HORS LIGNE", 2: "INDETERMINE"}[code], "-", detail)
if code == 2:
    moved = pusher_bytes()
    if moved is None:
        print("  et le debit du pusher n'a pas pu etre mesure non plus")
    else:
        kbps = moved * 8 / 10 / 1000
        print(f"  le pusher a emis {moved} octets en 10s ({kbps:.0f} kbps)")
        if kbps > 500:
            print("  -> il emet, la chaine est presque surement en ligne")
            code = 0
        else:
            print("  -> rien ne sort du pusher")
            code = 1
sys.exit(code)
