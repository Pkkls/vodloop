#!/usr/bin/env python3
"""Watch a local folder and hand anything that lands in it to the server.

The machine running this keeps nothing: a file is copied up, checked, then
deleted locally. It is a relay, not a library.

    python relay.py                 watch the default folder
    python relay.py --once          one pass, then exit
    python relay.py --dir D:\\clips  watch somewhere else

Files are uploaded under a temporary ".part" name and renamed once the transfer
is complete, so the server never sees a half-copied file: its own ingest only
picks up known video extensions.
"""
import argparse
import pathlib
import subprocess
import sys
import time

HOST = "ubuntu@89.168.60.67"
KEY = str(pathlib.Path.home() / ".ssh" / "ssh-key-2026-05-07.key")
REMOTE = "/home/ubuntu/vodloop/incoming"
DEFAULT_DIR = pathlib.Path.home() / "Downloads" / "vodloop-out"
SUFFIXES = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".m4v", ".avi"}
SETTLE_SECONDS = 20
POLL_SECONDS = 15


def run(args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def remote_size(name):
    out = run(["ssh", "-i", KEY, "-o", "BatchMode=yes", HOST,
               f"stat -c %s '{REMOTE}/{name}' 2>/dev/null || echo -1"], timeout=60)
    try:
        return int(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return -1


def send(path):
    """Upload one file and remove it locally once the server confirms its size."""
    local_size = path.stat().st_size
    part = path.name + ".part"
    print(f"  envoi   {path.name} ({local_size / 1e6:.0f} Mo)", flush=True)

    up = run(["scp", "-q", "-i", KEY, "-o", "BatchMode=yes",
              str(path), f"{HOST}:{REMOTE}/{part}"], timeout=7200)
    if up.returncode != 0:
        print(f"  ECHEC   {path.name}: {up.stderr.strip()[:160]}", flush=True)
        return False

    # compare sizes before renaming: a truncated upload must not become playable
    if remote_size(part) != local_size:
        print(f"  ECHEC   {path.name}: taille distante differente", flush=True)
        run(["ssh", "-i", KEY, "-o", "BatchMode=yes", HOST, f"rm -f '{REMOTE}/{part}'"])
        return False

    mv = run(["ssh", "-i", KEY, "-o", "BatchMode=yes", HOST,
              f"mv '{REMOTE}/{part}' '{REMOTE}/{path.name}'"], timeout=120)
    if mv.returncode != 0:
        print(f"  ECHEC   {path.name}: renommage impossible", flush=True)
        return False

    path.unlink(missing_ok=True)
    print(f"  ok      {path.name} envoye et efface localement", flush=True)
    return True


def pass_once(folder):
    sent = 0
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        # a file still being written must not be uploaded half-finished
        if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
            continue
        if send(path):
            sent += 1
    return sent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    folder = pathlib.Path(args.dir)
    folder.mkdir(parents=True, exist_ok=True)
    print(f"relai actif sur {folder}", flush=True)
    print(f"destination {HOST}:{REMOTE}", flush=True)

    while True:
        try:
            pass_once(folder)
        except Exception as problem:  # a bad file must not stop the relay
            print(f"  erreur de passe: {type(problem).__name__}: {problem}", flush=True)
        if args.once:
            return 0
        time.sleep(POLL_SECONDS)


def demo():
    """Self-check: only settled video files are picked, in name order."""
    import tempfile
    folder = pathlib.Path(tempfile.mkdtemp())
    old = time.time() - 600
    for name in ("b.mp4", "a.mkv", "notes.txt", "fresh.mp4"):
        (folder / name).write_bytes(b"x")
    import os
    for name in ("b.mp4", "a.mkv", "notes.txt"):
        os.utime(folder / name, (old, old))

    picked = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if time.time() - path.stat().st_mtime < SETTLE_SECONDS:
            continue
        picked.append(path.name)

    assert picked == ["a.mkv", "b.mp4"], picked  # .txt skipped, fresh.mp4 too young
    print("self-check OK")


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        sys.exit(main())
