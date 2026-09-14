# Operating

What you are allowed to do to a running channel, what each action costs, and the
two escaping rules that have silently broken things.

## What a restart costs

Not all of these units are equal. Three of them are cheap and two are not.

| Unit | Restarting it costs | Safe to do |
|---|---|---|
| pusher | **The broadcast.** The RTMP session ends, the platform closes the recording and opens a new one, viewers are dropped. | Only when it is genuinely dead |
| feeder | The RTMP session, indirectly. Its timeline offset is persisted so it can restart, but the session usually drops with it. | Rarely, and expect a blink |
| prep | **The job in progress.** Whatever it was chunking or encoding is thrown away and starts over. | Only when nothing is in flight |
| chat, bus | Nothing on the wire | Freely |
| telegram bot | Nothing on the wire | Freely |

**The pusher must never be restarted to "fix" something upstream.** It is one
ffmpeg holding one connection, and everything above it is designed so that it
never has to move. A placeholder writer holds the FIFO open permanently for
exactly this reason: without it ffmpeg would see end of file the moment one chunk
finished and exit before the next one started.

**Before restarting prep, check nothing is in flight:**

    ps -eo pid,etime,args --no-headers | grep "[s]egment_time" | grep -v "bash -c"

Empty means there is nothing to lose. Three restarts in one night at four in the
morning each destroyed an hour of encoding, and the service looked stuck the
whole time while it was in fact advancing.

**Restarting prep does not fix a full disk.** It refuses to write below the free
space floor, so the restart produces nothing and the buffer stays empty. Free
space first. This is the 2026-09-14 outage in one sentence.

## Changing what is on air

The queue, not the filesystem. **Chunks written into `segments/` by hand are
deleted within seconds**: the reaper owns that directory and removes anything
whose numeric prefix does not match an item marked preparing or ready.

To put a specific library file on next:

1. Check nothing is being chunked, then stop prep. Writing the queue file under a
   running prep races with it.
2. Append an item with `status: pending` and one entry in `votes`. Playback order
   sorts on vote count first, then arrival time, so one vote puts it at the head.
3. Start prep and **wait until that item's chunks exist on disk**.
4. Only then skip.

Skipping before the chunks exist leaves the feeder nothing to read and puts the
standby clip on air, which is the thing you were trying to avoid.

**A skip costs about forty seconds of broadcast.** The feeder restarts, the RTMP
session drops with it, the recording is split in two. It recovers on its own. Say
so before doing it rather than after.

## Long sources are cut into parts

Most of what these channels are fed runs six to twelve hours. Aired whole, one of
them is most of a day on the same evening, and a viewer who comes back twice sees
the same stream both times. `bin/slice.py` cuts anything long into even parts of
at most fifty minutes, named `<title>-<id>.pNNofMM.mkv`, and everything
downstream treats a part as an ordinary library file.

Two settings make it work as intended, and the second is easy to forget:

- `VODLOOP_SLICE_SECONDS`, the ceiling per part, 3000 by default. Parts are cut
  even rather than at a flat ceiling with a stub at the end: a 2.56 h source
  becomes four parts of 38 minutes, not three of 50 and one of 3.
- `VODLOOP_SPREAD_PARTS` on the **prep unit**, not in the cron line, because the
  draw runs inside prep. Without it a video's parts play back to back and the
  channel airs one whole evening in a row, which is the thing the cutting was
  supposed to prevent. Measured on the live library: without it, up to four parts
  of the same video run consecutively; with it, never two.

The cut is a copy, roughly ten times real time, and it refuses more than it does:
never a file that is on air or holding chunks, never a delete of the original
until every part it produced has been probed and measured against it, and never a
job the disk cannot hold. If any part comes out in a shape prep cannot copy, the
parts are removed and the original is kept.

The reason this happens after the download rather than during it is in
[failures.md](failures.md#2026-09-14-ranged-fetch-always-fails): asking the
downloader for a byte range gets the request refused every time, so a long video
has to arrive whole whatever else is true.

## Pausing a channel

Stopping the units is not enough. Two things will fight the pause.

**The repair agent** restarts prep after three dry passes, every four minutes,
forever, and each restart refills the disk you may have just cleared. **The
collector** keeps fetching for a channel that is not broadcasting.

So a real pause is three steps:

1. Stop in order: chat, feeder, prep, pusher. Pusher last, so the stream ends
   cleanly rather than being cut mid-chunk.
2. `systemctl disable` each one, or a reboot brings the channel back.
3. Comment out that channel's cron lines. Comment, with a dated marker, do not
   delete: you will want them back, and a commented line documents itself.

Keep a copy of the crontab first. Restoring from a backup is minutes; rebuilding
one from memory is an evening.

If the alarm bot watches the paused channel by default, repoint it, or it will
page forever about a channel that is off on purpose.

## Deploying

There is no deployment tool. Files are copied and verified by hand, which is
tedious and has caught partial copies more than once.

    scp <one file> <host>:<path>        # one at a time, multi-source has returned 0 without copying
    tr -d "\r" < FILE | md5sum          # compare both ends, normalised

Line endings will differ between a checkout and the server. Compare normalised or
you will chase three files that are identical.

**After copying a Python file, load it, do not just compile it.** `py_compile`
checks syntax; a file missing an import compiles clean and dies on the first
request. Import the module or run its entry point once.

**A running process keeps the old code.** Deploying does not fix a job that
started before the deploy. Check `ps` before concluding the fix did not work.

## Escaping, twice bitten

**In cron, `%` means newline.** An unescaped percent in a command turns the rest
of the line into stdin. Any format string, any `%s`, must be written `\%`. A
crontab rewrite that loses those backslashes breaks every job that has one, and
the breakage is silent.

When rewriting a crontab programmatically, assert the count survives:

    assert after.count(chr(92) + chr(37)) == before.count(chr(92) + chr(37))
    assert len(after.splitlines()) == len(before.splitlines())

Then read it back and compare, because a shell pattern that has to survive
several quoting layers may itself be what failed. See
[probes-that-lie.md](probes-that-lie.md).

**In a systemd unit file, `%` introduces a specifier.** The same format string
must be written `%%s` there. Instance templates use `%i` deliberately, which is
why this is easy to get wrong: both spellings appear in the same file and mean
opposite things.

## Editing files over ssh

Heredocs through several shells eat one level of backslashes, so a regular
expression written that way arrives wrong and matches nothing. Write the patch to
a file locally, copy it, and run it there.

Every patch script asserts before it writes:

    assert t.count(old) == 1, "anchor matched %d times" % t.count(old)

A patch script that cannot fail once reported a file updated when nothing had
been written.

## Conventions this repo keeps

Comments and commit messages in English. No channel or creator name hardcoded
anywhere in the source; everything is passed through the environment, one
variable per thing, so the same tree serves every channel. Configuration lives in
cron lines and unit files, which is the largest remaining source of avoidable
mistakes and the cleanup most worth doing next.
