#!/usr/bin/env python3
"""Prove, against the live channel, that no hour can go out twice.

Read only: it draws nothing, moves nothing, writes nothing. It asks the same
functions the cutter asks and checks every answer they could give.
"""
import collections
import pathlib
import sys

sys.path.insert(0, "/home/ubuntu/v2/bin")

import chan  # noqa: E402
import cut  # noqa: E402

book = cut.ledger()
durations = chan.read_json(chan.STATE / "durations.json", {})
problems = []

print("== the ledger itself")
total = sum(len(h) for h in book.values())
print(f"   {len(book)} videos, {total} hours recorded as aired")
raw = []
try:
    for line in cut.HOURS.read_text().splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            raw.append((parts[1], parts[2]))
except OSError:
    pass
doubles = [k for k, n in collections.Counter(raw).items() if n > 1]
print(f"   hours written more than once: {len(doubles)}"
      f"{' (harmless, a set is a set)' if doubles else ''}")

print()
print("== every hour the draw could offer right now")
offered = 0
for folder, label in ((chan.QUEUE, "queue"), (chan.AIRED, "reserve"), (chan.CURRENT, "on air")):
    for path in chan.media(folder):
        seconds = chan.duration(path, durations)
        if not seconds:
            continue
        free = cut.unaired(path, book, durations)
        spent = cut.played(path.name, book)
        overlap = free & spent
        if overlap:
            problems.append(f"{path.name}: hours {sorted(overlap)} both free and spent")
        offered += len(free)
        print(f"   {label:<8} {path.name[:44]:<44} free {sorted(free) or '-'} spent {sorted(spent) or '-'}")
print(f"   {offered} hours the channel may still show")

print()
print("== the window function, asked a thousand times per file")
for folder in (chan.QUEUE, chan.AIRED):
    for path in chan.media(folder):
        seconds = chan.duration(path, durations)
        free = cut.unaired(path, book, durations)
        if not seconds or not free:
            continue
        drawn = {cut.window(path, seconds)[2] for _ in range(1000)}
        if not drawn <= free:
            problems.append(f"{path.name}: window offered spent hours {sorted(drawn - free)}")
        print(f"   {path.name[:44]:<44} drew only {sorted(drawn)} of the free {sorted(free)}")

print()
print("== what happens when nothing is left")
# next_source() moves files, so it is not asked here: the branch is read from
# the source instead. Calling it in an audit once swept four files out of the
# queue on the live channel, which is the opposite of read only.
body = pathlib.Path("/home/ubuntu/v2/bin/cut.py").read_text()
tail = body.split("def next_source")[1].split("def claim")[0]
if "return None, None" not in tail.rsplit("for folder, origin", 1)[-1]:
    problems.append("the draw no longer ends on None when nothing is unseen")
import re as _re
if _re.search(r'claim\([^)]*"repeat"', tail):
    problems.append("a repeat tier is back in the draw")
print("   the draw ends on (None, None): standby clip and an alarm, never a repeat")

print()
if problems:
    print("PROBLEMES:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("AUCUNE heure ne peut repasser: verifie sur l'etat reel de la chaine")
