# Diagnosis

Follow a section top to bottom. Each step is a question with a command and a
reading, ordered so the cheap questions that eliminate the most come first. This
is written to be used at four in the morning without thinking.

Throughout, `$ROOT` is the channel root (the directory holding `segments/`,
`state/` and the FIFO) and `$LIB` is its video library. Both are set per channel
through the environment; a command run without them silently answers about the
wrong channel, which is its own trap.

---

## The picture is grey

The most common report, and the one most often misattributed to the player.

**1. Is it the standby clip?** Do not reason about this, measure the frame a
viewer receives.

    /black

**Since 2026-09-14 the standby clip says so in words**: it carries the text
`switching vod...` centred on the same dark field. If you can read that, the
queue is empty and the rest of this section applies. That replaced a flat field
whose only tell was its luma, which meant the difference between "nothing to
play" and "dead stream" could only be settled by measuring.

The older tell still works on a channel whose clip has not been regenerated: a
flat field reads min, max and average all equal at about 17, with two frames five
seconds apart identical. A real picture spreads, for example min=5 max=225
avg=86. Note that the clip with text also spreads, so **luma alone no longer
separates the standby clip from real content**. The unambiguous check is the
chunk count below.

If it is real content, the fault is in the viewer's player or their session, not
here. Tell them to reload.

**2. If it is the standby clip, the chunk queue is empty.** Confirm:

    ls $ROOT/segments/*.ts | wc -l

Zero means the feeder has nothing and is doing the only thing it can.

**3. Why is it empty? Ask the disk first.** This is the question that was skipped
on 2026-09-14 and cost twenty minutes.

    df -h /

Below about 4 Gio free, the encoder refuses to write anything at all, and
restarting it changes nothing. Go to [capacity.md](capacity.md) and free space.
Nothing else in this section matters until that is done.

**4. Disk is fine. Is the encoder running, and is it actually working?**

    systemctl is-active <prep unit>
    ps -eo pid,etime,args --no-headers | grep "[s]egment_time" | grep -v "bash -c"

No segment job and a full queue means it has produced everything the current
item needs and is waiting, which is normal. No segment job and an empty queue
means it refused; read its journal.

**5. Is there anything it is allowed to prepare?**

    python3 -c "import sys;sys.path.insert(0,'<bin>');import prep,glob;\
      [print(p, prep.remux_verdict(p)) for p in sorted(glob.glob('$LIB/*.mkv'))]"

All `False` means the library is full of files that would need a re-encode, and
the queue is by design empty rather than expensive. See
[failures.md](failures.md#2026-09-06-grey-screens-that-five-fixes-did-not-stop).

**Do not restart the encoder to unstick it.** Read
[operating.md](operating.md#what-a-restart-costs) first.

---

## The pusher restarts forever

**1. Read the actual error.** There are two faults with the same symptom and
different causes, and the message separates them.

    journalctl -u <push unit> -n 40 --no-pager

- `Packet is missing PTS` and `av_interleaved_write_frame(): Invalid argument`
  is a timestamp fault inside the pipeline. Continue below.
- `session has been invalidated` or `End of file` is the ingest dropping the
  connection from its side. That is not this fault. It happens after a feeder
  restart, and it resolves itself in under a minute.

**2. For a timestamp fault, count the bad packets in the chunk it died on.**

    ffprobe -v error -select_streams v -show_entries packet=pts_time \
      -of csv=p=0 $ROOT/segments/<the chunk>.ts | grep -c N/A

Zero means the chunk is clean and you are looking at the wrong thing. Around
fifty per 300 second chunk is the SEI fault:
[failures.md](failures.md#2026-09-05-and-2026-09-14-packets-with-no-timestamp).

**3. Check the chunk writer still resets timestamps.** The other cause of the
same message is a segment written without `-reset_timestamps 1`, whose
timestamps then stack with the feeder's own offset.

    grep -n "reset_timestamps" <bin>/prep.py

**4. Confirm the fix in the running process, not in the file.** A deployed patch
does not change a job already running with the old code in memory. Check `ps`
before concluding a fix did not work.

---

## The buffer is empty

Order matters here, because the expensive answer is last.

    df -h /                                   # 1. the floor, always first
    ls $ROOT/segments/*.ts | wc -l            # 2. how bad
    systemctl is-active <prep unit>           # 3. is it even up
    journalctl -u <prep unit> -n 20 --no-pager

Then: is there an item to work on at all?

    python3 -c "import json,collections;q=json.load(open('$ROOT/state/queue.json'));\
      print(collections.Counter(i['status'] for i in q['items']))"

`ready` with no chunks on disk has been observed twice. Do not trust the status
field, list the directory.

A queue holding only `played` entries means refill has not fired. Refill only
runs when nothing is pending, so one stuck pending item blocks it indefinitely.

A thin buffer is not always a fault. The encoder prepares one item at a time and
stops at the ahead limit, so a short video legitimately leaves ten minutes on
disk until it is played and the next one is queued. Watch for two minutes before
acting: a recovery from 2 chunks to 29 in two minutes is the normal shape.

---

## Nothing new arrives

The channel eats 24 hours of video a day. Gaining none for six hours is broken
whatever the cause, and that single assertion covers every failure in the supply
chain at once.

**1. How long has it been?**

    python3 -c "import sys;sys.path.insert(0,'<bin>');import tgbot;\
      print(tgbot.newest_arrival()/3600, 'hours')"

**2. Is the fetching machine alive and talking?** It reports into a status file
on the server, since the server cannot reach it.

    cat <inbox dir>/status.json

`at` older than about fifteen minutes means it has stopped reporting. `queue`
tells you whether it has work. Compare `done` against `failed`, but remember both
are cumulative over the life of the project and include eras with known-broken
options, so a bad ratio is not by itself current evidence.

**3. Is the server queueing anything for it?**

    <collector, dry run>

`part servie ou disque trop juste` means the share is full or the disk is tight,
and the fix is in [capacity.md](capacity.md), not in the fetcher.
`la part ne laisse pas la place d un fichier de plus` means the share has less
headroom than one file needs.

**4. Is the inbox being drained?** Watch the file for one cycle of the fetcher's
schedule. Emptying means it pulled. Not emptying while it reports fresh means the
pull is broken.

**5. Only then look at the fetching machine itself.** It is a single core board.
Grepping a large log on it while it is downloading starves its ssh daemon and
you will lose the connection and slow the very thing you are trying to fix. Keep
commands short, prefer reading the status file from the server.

---

## A file will not play, but it probes clean

This is its own trap and has its own entry:
[failures.md](failures.md#2026-09-05-and-2026-09-14-packets-with-no-timestamp).

The short version: stream level probing tells you nothing about this fault, the
source is genuinely clean, and the shortcut reproduction
(`ffmpeg -i FILE -c copy -f flv`) exits 0 and will convince you the check is
wrong. Replay the real chain, chunk included.

---

## A short is playing and I did not ask for one

Two things put a short on the wire, and they look identical from the outside.

    R=/home/ubuntu/v2/nanatty247
    ls $R/chunks/*.ts | wc -l           # nothing waiting: it is filling a gap
    journalctl -u vodloop-v2-feed@nanatty247 --since "20 min ago" | grep short

`short intercale` means the chat voted for a video the board has to fetch, and
the short covers the wait. It goes out between two chunks and the hour on air
resumes straight after it.

`rien a envoyer, short` means the buffer is empty. The short is not the problem,
it is what the channel shows instead of the loading card while the problem lasts.
Go to [the buffer is empty](#the-buffer-is-empty). The watchdog still counts an
empty buffer as an empty buffer, so an alarm has already been raised.

`short ecarte` means one was refused before it reached the wire: bigger than the
session, or a file ffprobe could not read. Both are worth a look, since
`shorts.py` builds them all at the channel's own ceiling and neither should
happen.

What it never means is that the channel is repeating itself. A short is written
into `state/onair.json` as a wait, so nothing is spent in the hour ledger and
`!now` says a short is playing rather than naming a video that is not on.

## The wire is not at the height the settings say

`MAXH` decides what is fetched. It does not decide what goes out, and there is no
setting that does: Kick fixes a session's ladder on the first picture it sees and
holds it until the session closes. So the two can disagree for days, both of them
correct.

    cd /home/ubuntu/v2/bin && CHAN_ROOT=$R python3 ceiling.py

That prints one of three answers: already at the ceiling, blocked by N units
above it, or that the flip can happen. It runs itself every twenty minutes with
`--apply` and does the flip the moment nothing above the new ceiling is left to
show, which costs one pusher restart, about six seconds off air, and is
announced on Telegram.

Do not do the flip by hand while a file taller than the new ceiling still holds
unaired time. That chunk exceeds the session, the session reopens on a clip it
exceeds again, and the loop repeats every ten minutes for ever. The waiting is
the whole point of the tool.

---

## Before you report a cause

Two questions, every time.

What would my command have printed if the system were healthy? If the answer is
"the same thing", the probe did not measure anything. See
[probes-that-lie.md](probes-that-lie.md).

Did I see the fix working in the running system, or did I see a file change and a
green test? Those are different claims and only one of them is the job.
