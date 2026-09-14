# Failure catalogue

Every fault this system has actually had. Read the headings once so that when
one repeats you recognise it instead of re-deriving it.

Each entry has the same shape: what it looked like, what it actually was, the
measurement that told those two apart, the fix, and the control that keeps it
fixed. Where a control is missing that is said out loud, because an entry with
no control is a fault waiting to come back.

Two faults in this list have the identical error message and different causes.
That is the reason the measurement line exists.

---

## 2026-09-04 The picture was grey and the player was blamed

**Looked like** a broken player or a platform problem. The channel was live, the
pusher was connected, viewers saw a flat dark frame.

**Was** an empty `segments/` directory. `feeder.py` does
`source = segments[0] if segments else filler`, so an empty queue is not an
error, it is the standby clip, forever and silently.

**Measured by** the luma of the frame a viewer receives. The standby clip is
generated from `color=c=0x101014`, a flat luma of 17.3, so min, max and average
are all equal and two frames five seconds apart are identical. A real picture
spreads its values. One pass settles it.

**Root cause** was one line of the hardening script. It applies
`ProtectHome=read-only` plus a `ReadWritePaths` list to the prep unit, and that
list named the state, segment and incoming directories but not the video
library. `normalise_in_place()` writes its output next to the source, inside the
library, so every job died with `[Errno 30] Read-only file system` and produced
nothing.

**Fix** was to add the library to the drop-in. The installed drop-in was
corrected by hand; the script that generates it was corrected later. Anything
that regenerates a sandbox has to be checked against every path the code writes
to, not the obvious ones.

**Control**: none automatic. This is a gap. The closest thing is the five minute
quality probe, which now reports `aucun chunk sur le disque`.

---

## 2026-09-04 One hundred and forty five pusher restarts

**Looked like** an ingest problem. The stream broke into fragments, an 11h52
recording became 35 min, 36 min and then pieces, viewers went from 44 to 0.

**Was** chunks written with `-c:v copy` and no `-reset_timestamps 1`. They kept
the source timestamps, so chunk 2 started at 300s and chunk 3 at 600s. The feeder
adds its own cumulative `-output_ts_offset` on the assumption that each chunk
starts at zero. The two stack, the flv muxer rejects the result, and the pusher
exits about seven seconds after each restart.

**Measured by** the error signature. `Packet is missing PTS` and
`av_interleaved_write_frame(): Invalid argument` is a timestamp fault inside the
pipeline. An ingest cut gives `session has been invalidated` or `End of file`
instead. Those two are not the same incident and the fix for one does nothing
for the other.

**Fix** was `-reset_timestamps 1` on the segment writer.

**Control**: `tests/test_prep_consumed.py` and the chunk comparison in
`quality.py`.

Two lessons were paid for at the same time. A deployed fix does not repair a
process already running with the old code in memory: a job started at 19:55 was
still running the old path half an hour after the 20:25 deploy, while the same
command tested by hand gave the right answer. And `pkill -f <pattern>` run over
ssh matches the ssh command itself and kills your own session. Kill by pid, or
bracket the pattern.

---

## 2026-09-05 and 2026-09-14 Packets with no timestamp

The same error message as the entry above, a different cause, and nine days
between noticing it and understanding it. **Check which one you have before
touching anything.**

**Looked like** a file that plays perfectly everywhere. Stream level probing is
clean: h264, 1920x1080, 60 fps, aac 44100 stereo. The source itself probes with
zero timestamp-less packets.

**Was** an SEI unit that the platform hangs off every keyframe. Copying that
video into MPEG-TS makes the muxer emit the SEI as a packet of its own, carrying
no timestamp and no position in the file: 86 to 87 bytes, immediately behind
every packet flagged `K_`, one every 6.0 seconds, which at 60 fps is one every
360 packets. The transport stream carries them without complaint, so prep logs a
clean job and the chunk looks fine. The flv muxer at the far end refuses the
first one and exits 1. systemd restarts it onto the same chunk and it exits
again. **1298 consecutive restarts, black the whole way, and nothing anywhere
reporting a fault.**

**Measured by** counting timestamp-less packets in a chunk, not in the source:

    ffprobe -v error -select_streams v -show_entries packet=pts_time \
      -of csv=p=0 CHUNK.ts | grep -c N/A

Zero is healthy. About fifty per 300 second chunk is this fault. To see what
they are, ask for position and flags as well: the offending packet has no `pos`
at all, which is the tell that the muxer synthesised it rather than reading it.

**The trap that cost an hour**: reproducing this with
`ffmpeg -i FILE -c copy -f flv` exits 0 with empty stderr. The defect does not
exist in the source, only after the chunking step, so the shortcut reproduces
nothing and argues convincingly that the check is wrong. Replay the whole chain.

**First fix, 2026-09-05**, was to refuse such files: `prep.remux_is_safe()`
remuxes ten seconds, counts, and votes. Ten seconds is enough because the defect
is spread evenly rather than clustered. That kept the channel up and threw away
most of the supply: 32 sources across two channels were written off as
permanently unusable.

**Real fix, 2026-09-14**, is to drop the SEI, which costs nothing:

    DROP_SEI = ["-bsf:v", "filter_units=remove_types=6"]

Applied to both copy paths and to the probe, so the probe measures the command
the job actually runs. Measured on a refused file, production chain replayed, five
minutes taken from the middle of it: 50 timestamp-less packets become 0, the
pusher goes from exit 1 to exit 0, and the framemd5 over 959 decoded frames is
identical with and without, against a desaturating control that does move it. A
file that already passed still passes.

Re-encoding also fixes it, at 0.06x real time, which the box cannot pay. That is
why this was worth nine days.

**Control**: `tests/test_prep_remux_safety.py`, nineteen checks, including that
the filter takes bytes out of a chunk while leaving every decoded frame
identical. The size check is the control for the pixel check: without it, a
filter that did nothing would pass "pixels unchanged" just as happily.

**When you change what `remux_is_safe` measures, purge `state/remux.json` and
`state/unusable.json` on every channel.** Old verdicts reached under the old
probe otherwise outlive the change in silence.

---

## 2026-09-06 Grey screens that five fixes did not stop

**Looked like** a scheduling problem. Five successive fixes treated it as one: a
flat threshold, a 1.5 factor, a 4 factor, a cost model, a buffer watchdog. Each
held for a few days.

**Was** economics, not ordering. The feeder consumes at 1.0x permanently, a
remux produces at about 10x, and a full re-encode produces at 0.06 to 0.09x
measured on this box with the pusher running. A file that has to be encoded is
not slow, it is a net loss that no scheduling recovers.

**Measured by** counting what fraction of the library could be copied at all: 38
of 62 files, 61 percent, could not. Twenty five were two pixels short of the
target height, one vp9, one av1, one at the wrong resolution, and ten were the
timestamp fault above. The rotation landed on one of those six times in ten.

**The lock nobody had seen**: `refill_from_library` only fires when nothing is
pending. Thirty one un-copyable items sat pending forever, so refill could never
run, and prep chewed through them one multi-hour encode at a time. Choosing
better inside that list did not help, because every entry was expensive.

**Fix** was an invariant: the queue only ever contains files prep can copy.
`refill_from_library` filters on `remux_verdict`, `drop_unremuxable` removes the
ones blocking the queue, and offline conversion proves its output copyable before
replacing anything. The chain stays healthy even if conversion never catches up,
because an un-copyable file simply never enters the queue.

`remux_verdict` must test **the pair**, shape and copyability. Testing only the
second let in files whose copy is valid but whose shape is wrong, and prep
re-encoded them anyway, which is exactly the cost the filter exists to avoid.
Caught because the converter announced 20 files to do where measurement gave 38.

**Control**: `tests/test_prep_refill.py`, `tests/test_prep_remux_safety.py`.

---

## 2026-09-06 Twenty two good files became invisible

**Was** caching "could not measure" as a verdict. The probes were losing to a
busy box, and every loss was written down as a permanent refusal.

**Fix**: `remux_is_safe()` returns `None`, not `False`, when it could not
measure, and the caller that remembers verdicts does not write `None` down. A
probe that read no packets does not get to vote yes, and does not get to vote no
either.

**Control**: three checks in `tests/test_prep_remux_safety.py` for the
unmeasurable cases, each paired with a control where the probe did run and did
see the defect, so they cannot pass on a function that has stopped answering.

---

## 2026-09-13 An alarm spoke once and then stopped

**Looked like** silence meaning health.

**Was** edge triggering. `health()` said something only when a key entered its
alarm set, so a standing fault produced exactly one message. A channel's state
file held one alarm from the previous night. One message, in a chat shared by two
channels, is one message nobody finds again.

**Fix**: a standing fault repeats every `REMIND_SECONDS`, currently three hours,
saying how long it has stood.

**Control**: `tests/test_tgbot.py`.

Related, same day: nothing owned the chain as a whole. Every component logged its
own refusal locally and published nothing: the board's failure count sat unread
in a status file, the collector's "no room" went to its own log, the HTTP
failures went to the board's log. All of it was there and none of it was watched.
`newest_arrival()` is the single assertion that covers the lot: a channel eats 24
hours of video a day, so gaining none for six hours is broken whatever the cause.

---

## 2026-09-14 Ranged fetch always fails

**Looked like** an intermittent network problem: HTTP 403 from the CDN, ten times
in a row.

**Was** `--download-sections`. It hands the byte range to ffmpeg, which fetches
it without the downloader's own headers, and the CDN refuses every time. It is
not intermittent and no retry policy helps.

**Fix**: do not use it. To bound file size, bound the video's duration up front
with a maximum-seconds filter so only whole fetchable videos are queued.

**Control**: none automatic. The guard is that the option is not used anywhere;
if you reach for it, this entry is why it is not there.

---

## 2026-09-14 Skipping a video costs the broadcast

**Looked like** a clean operation: the skip is written to a state file and the
feeder acts on it.

**Was** the feeder restarting, which drops the RTMP session:
`The specified session has been invalidated for some reason`. The pusher
restarts, the platform closes the recording and opens a new one, and the channel
reads offline for about forty seconds before coming back on its own.

**Rule**: do not skip casually. If a video must change, queue the replacement and
let the current one finish. If the operator wants it now, tell them the stream
will blink and the recording will be split in two, then do it.

To put a chosen library file on air next: add a pending queue item with one vote,
because playback order sorts on vote count and then on arrival time; stop prep
while writing the queue file, since writing it under a running prep races; wait
until that item's chunks exist on disk; and only then skip. Skipping before the
chunks exist leaves the feeder nothing to read and puts the standby clip on air,
which is the thing you were trying to avoid.

---

## 2026-09-14 Twenty minutes on the standby clip with a repair loop running

**Looked like** the repair agent working: it detected the empty buffer and
restarted prep, three passes running, exactly as designed.

**Was** a disk below the floor. `disk_is_tight()` stops prep before it writes a
single chunk, so the restart could not produce anything, the buffer stayed empty,
and the repair fired again each pass. **The restart was never the missing piece.
Space was.**

**Measured by** the five minute quality samples, which had recorded
`buffer_s: 0` with 3.5 to 3.8 Go free against a 4 Gio floor, every five minutes,
for twenty minutes. Detection and paging both worked. Only acting was missing.

**Three things filled the disk**, in order of blame:

1. A share raised on arithmetic that counted a hand-picked list of directories as
   "system" and was 6 Go short. See [capacity.md](capacity.md).
2. 4.5 Go of diagnostic scratch accumulated in /tmp across sessions, one file of
   it 2.6 Go. Left there by whoever was debugging, which was me.
3. The janitor cannot enforce a share when every file is protected. With nine
   files and a protection floor of the newest N it reported `liberable: 0.00G`
   while the library sat over budget. Correct on a healthy disk, and exactly what
   let this one fill.

**Fix**: the repair agent checks the floor ahead of the restart and gives space
back cheapest first. Diagnostic scratch under /tmp goes first because it costs
the channel nothing: older than two hours, larger than 64 Mo, owned by us, never
a lock file and never a probe the encoder is holding. Only when there is none
does it give up a library file, and only one that has already been on air, holds
no chunks, and is not among the last three, because an empty library is the same
grey screen tomorrow.

**Control**: ten checks in `tests/test_medic.py`. Each refusal is tested against
a control that does get selected, so they cannot pass on a function that returns
nothing.

---

## 2026-09-14 Every alarm named the wrong channel

**Was** a hardcoded channel name at the top of the status text. Harmless while
there was one channel. From the day there were two, every status and every alarm
carried the wrong name, including during an outage.

**Fix**: use the label the rest of the file already uses to say who is speaking,
falling back to the old string when none is set.

**Control**: `tests/test_tgbot.py` exercises both the labelled and unlabelled
cases.

---

## 2026-09-14 Half the library was the wrong resolution and nothing said so

**Looked like** the sources only offering 720p for some streams. Four of ten
library files were 1280x720 while the channel is configured for 1080p, and every
one of them passed the shape check, so nothing anywhere reported a problem.

**Was** the downloader's own size cap. The format selector asks for
`avc1` at `height<=1080` **and** `filesize_approx` under the cap, then falls
through to a `height<=720` branch, then 480, then 360. A long stream's 1080p
rendition exceeds the cap, the first branch matches nothing, and the second one
quietly succeeds. There is no warning: a fallback that works is not an error.

**Measured by** asking the source what it actually offers, for a file that had
arrived at 720p:

    yt-dlp -F <url> | grep avc1

    311  1280x720   60  ~4.36GiB  3799k  m3u8
    298  1280x720   60   2.61GiB  2277k  https
    312  1920x1080  60  ~6.90GiB  6015k  m3u8
    299  1920x1080  60   4.62GiB  4029k  https

The 1080p rendition existed and was 4.62 GiB against a cap of 4 Go, which is
3.73 GiB. Then the same selector run at both caps, which is the control that
turns a plausible story into a cause:

    cap 4000000000  ->  298+140  720p   2.96 GB
    cap 5500000000  ->  299+140  1080p  5.12 GB

**Fix**: raise the cap. These sources run about 1.7 Go per hour at 1080p60, so
5500M covers everything up to the three hour ceiling the collector already
enforces, and the lower branches stay in place so a heavier source degrades
rather than fetching nothing.

**The cap is not arbitrary and is not about taste.** The fetching board has a 15
Go card, and the downloader writes video and audio separately then merges, so
peak usage is about twice the final size. 5500M peaks near 11 Go against 13 Go
free. Raising it further needs a larger card, not a larger number. That is the
whole reason a cap exists at all, and anyone raising it should check the card
first.

**And fixing the files does not fix the wire.** The platform decides its rung
ladder when the session opens and keeps that decision for the whole session. A
session opened while a 720p video was on air tops out at the platform's own 720p
encode and never gains a source rung, however many 1080p chunks are pushed into
it afterwards. Measured 2026-09-12, and again on 2026-09-14 after the library had
been made entirely 1080p: the samples still read `rung_height: 720` against
`width: 1920` on the pushed chunk.

Restarting the pusher is not enough either. The ingest holds the session across a
reconnect, so the timestamp the API reports does not change and the ladder does
not move. Only letting the session actually expire works: stop the pusher, poll
until the API reports offline, then start it. Measured 2026-09-14: the session
closed after about two minutes, and the whole outage was 2 min 14 s with a new
recording opened at the end of it.

Whether that is worth doing is the channel owner's call, not a technical one. It
costs a real outage and splits the recording, and on this channel the answer was
that 720p60 is fine. The monitoring threshold was then set to 720 rather than
1080, so it stays quiet about an accepted condition and still speaks if the
ladder ever drops below it. **An alarm for something the owner has accepted is
noise, and noise is how the useful alarm gets missed.**

**Control**: none automatic, and this is a gap worth naming. Nothing asserts that
what arrives matches the height the channel asked for. The check is one ffprobe
per arrival, and until it exists the failure mode is silent by construction.

---

## Standing hazards that have not bitten yet

**Chunks written into `segments/` without a matching queue item are deleted
within seconds.** `reap()` owns that directory and removes anything it does not
recognise, comparing the numeric prefix against the ids of items marked preparing
or ready. An external tool that writes chunks is doomed however good it is. To
feed the queue, add an item.

**A queue can claim `ready` for items with no chunks on disk.** Observed twice.
Do not trust the status field, list the directory.

**The presentation timestamp wraps.** A channel's accumulated offset was measured
at 430959 seconds against a 33 bit PTS limit of 95443.7 seconds. It looked
exactly like the cause of an outage and it was not: reset to zero, the pusher
died identically. It remains an undemonstrated time bomb rather than a known
fault, and it is recorded here so the next person does not spend an evening on it
as if it were new.
