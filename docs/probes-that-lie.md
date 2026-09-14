# Probes that lie

A failing measurement is cheap: you see it fail and you try another. A
measurement that returns a confident wrong answer costs an hour, because you
leave and go looking somewhere else.

Every entry below actually happened and cost time. They are grouped by the shape
of the lie, because the shape repeats even when the tool changes.

## The probe measured itself

**`ps | grep "pattern"` matches your own command line.** The shell you typed it
in contains the pattern, so the grep finds itself and you count one process that
does not exist. Over ssh it matches the ssh command too.

Three counts in one night were my own grep: a "2 chunking jobs running" that was
two feeder readers, a "jobs_decoupe=1" that was the shell, and a
`grep -c "segment_time"` that returned 2 with nothing running at all.

    ps -eo pid,args --no-headers | grep "[s]egment_time" | grep -v "bash -c"

Brackets stop the literal match. Excluding `bash -c` stops the wrapper you ran
it from. Then sanity check: if the count is non-zero, print the lines and read
them, do not trust the number alone.

The same trap once matched `grep 'libx264.*segment'` against a command line that
contained `segments/*.ts`, and declared an encode running that was not.

**A `grep` whose pattern was eaten by the quoting layers.** Checking that cron's
percent escaping survived a rewrite, this returned 0:

    grep -c "unit-\\%s@<channel>"

Zero looked like "the escaping is gone, five jobs are broken". The escaping was
fine. Four layers of quoting (local shell, ssh, remote shell, heredoc) had eaten
the backslashes and the pattern no longer matched anything. When a shell pattern
has to survive ssh, do the comparison in Python on the far side and print both
counts:

    python3 -c "a=open('before').read(); b=run_crontab(); print(a.count(chr(92)+chr(37)), b.count(chr(92)+chr(37)))"

**A grep for the wrong idiom.** `grep -c "def test"` on `tests/test_medic.py`
returns 0, and 0 reads as "there are no tests". The suites here are standalone
scripts with a `check()` helper, not pytest functions. Run them, do not count
them.

## The probe measured a different thing than it claimed

**`/proc/<pid>/io` does not measure a socket.** Asked whether the pusher was
still sending, `wchar` read 30100 bytes total for a process that had been
streaming for two days. The delta over ten seconds was 0, which reads exactly
like a dead pusher. It was pushing fine.

Use `bin/live.py`, or the byte counter in `tgbot.emitting()` which filters on the
pid, not on whichever connection `ss` lists first. An earlier version of that
check took the first `bytes_sent` it found in `ss -tni` and called an active
pusher silent.

**The platform API says live when there is no picture.** The pusher holds the
RTMP session and sends the standby clip, so the channel reads live, with
viewers, while the screen is grey. `live.py` returning `EN LIGNE` is a statement
about the socket, not about the content.

The only verdict on the picture is what a viewer receives, which `/black` fetches
and measures. It answers "is the picture black", and for a long time that was
also how the standby clip was recognised: generated from `color=c=0x101014`, it
was a flat luma of 17.3, min, max and average all equal, two frames five seconds
apart identical. Real content spreads, for example min=5 max=225 avg=86.

**That shortcut died on 2026-09-14**, deliberately. The clip now carries the words
`switching vod...`, so it spreads too: measured, 11912 bright pixels, all of them
inside the centre band and none in the corners. `/black` will call it real, which
it is. Recognising the standby clip is now `ls segments/*.ts | wc -l` returning
zero, or reading the words. Both are better than a luma heuristic, and the reason
for the change is the same reason this page exists: a viewer and an operator
could not tell an empty queue from a dead stream without instrumentation.

**Reading a field without checking the HTTP status.** The first version of the
liveness probe read `livestream` straight off the response body. On 2026-09-06
the API answered 404, the key was simply absent, and the probe printed OFFLINE
for a channel that was pushing 3377 kbps. That is why `live.py` has three exits:
0 live, 1 offline and the API said so, 2 no answer. "No answer" is not
"offline", and collapsing them sends you hunting an outage that does not exist.

**`du` on a list of directories you chose is not the disk.** Sizing a share, I
counted `/usr`, `/var` and one app directory as "system" and got 7 Go. The real
figure was 13 Go: /tmp, /snap and both channel roots were not in my list. The
share went up by 2 Go more than the disk had, the floor was breached, and a
channel spent twenty minutes on the standby clip.

    du -sBM /* 2>/dev/null | sort -rn | head
    du -sBM /home/<user>/* 2>/dev/null | sort -rn | head

Enumerate, then subtract. Never assemble a total from a list you wrote from
memory.

**A rate measured on one file, applied to a different kind of file.** Sizing the
duration ceiling, I measured 1.02 Go per hour from a 2.74 h video and multiplied.
That put a ten hour source at 10.2 Go, over twice what the fetching board's card
could hold, and I refused the whole long half of the catalogue on it: 141 videos
eligible out of 531, three times over, each time citing my own arithmetic back as
if it were evidence.

It was wrong by a factor of three. Long IRL streams are not short videos scaled
up: the platform serves them far thinner. The measurement was already written
down in this repository, in a comment in `quality.py`:

> A 720p30 stream at 690 kbps, which is what YouTube serves for the long IRL
> VODs in this pool, is outbid by Kick's 720p60 rung on every sample of every one
> of its eleven hours.

690 kbps is 0.31 Go per hour. An eleven hour source is 3.4 Go, not 11. The
ceiling went to twelve hours and the pool went from 141 to 531 with no pipeline
change at all, because there had never been anything in the way.

Two lessons, and the second is the one that cost the time. A rate is a property
of a kind of content, not a constant: check it on the kind you are about to
apply it to. And **before building a workaround for a limit, re-measure the
limit** — I had designed a whole streaming fetch to get around a wall that was
not there.

## The probe ran the wrong command

**Reproducing the failure with a shortcut reproduces nothing.** A library file
that killed the pusher every time exits 0, with empty stderr, under:

    ffmpeg -i FILE -c copy -f flv -y /dev/null

That result is real and it is useless: the defect is created by the chunking
step, not present in the source. It very nearly convinced me the production
check was wrong and should be relaxed. The chain has to be replayed whole:

    ffmpeg -t 60 -i FILE -c:v copy -c:a copy -f segment -segment_time 300 \
      -segment_format mpegts -reset_timestamps 1 -y /tmp/c_%05d.ts
    ffmpeg -i /tmp/c_00000.ts -c copy -f flv -y /dev/null; echo $?

Exit 1 with `Packet is missing PTS` is the fault. See
[failures.md](failures.md#2026-09-05-and-2026-09-14-packets-with-no-timestamp).

**The probe ran in the wrong container.** Testing that the SEI filter is
lossless, writing a raw h264 elementary stream produced a file ffmpeg could not
decode: `non-existing PPS 0 referenced`. That says nothing about the filter and
everything about the container, which carries no parameter sets. In MPEG-TS,
which is what the job writes, the same filter is exactly lossless: 959 decoded
frames, identical framemd5. Always probe in the container production uses.

**Simulating a function without the environment it runs with.** Running
`refill_from_library` by hand without `VODLOOP_LIBRARY` set returns 0 and looks
like an imminent outage. The service has the variable; the shell did not.

## The probe proved nothing and said so in a positive voice

**A negative result has to prove it could have gone positive.** Comparing pixels
before and after a bitstream filter, both sides came back as
`d41d8cd98f00b204`, which is the md5 of the empty string. The pipeline had
produced nothing at all and the two nothings matched. It read as "identical,
filter is lossless".

Every comparison needs its own control. The version that replaced it asserts
that 959 frames were decoded on each side, and separately runs a desaturating
filter that must produce a different fingerprint. If the control does not move,
the comparison is not measuring.

**A check that cannot fail.** A deploy script printed success unconditionally,
so it reported a handoff file updated when its anchor had not matched and
nothing had been written. Every patch script in this repo now ends with an
assertion on the anchor count, and the caller greps the file afterwards:

    assert t.count(old) == 1, "anchor matched %d times" % t.count(old)

**`py_compile` is not a load.** It checks syntax. A file missing an `import os`
compiles clean and dies on the first request. After editing, import the module
or run its entry point once.

**Exit codes swallowed by a pipe.** `cmd | head -8` then `echo $?` gives head's
status, not the command's. Two false `exit=0` readings came from that before it
was noticed. Use `${PIPESTATUS[0]}`, or capture to a file and check separately.

## The tooling in between changed the answer

**Files differ because of line endings, not content.** Comparing the repo with
the server, three files looked modified. They were identical; the server copies
had CRLF. Normalise before hashing:

    tr -d "\r" < FILE | md5sum

**`scp` can return 0 without copying.** A multi-source `scp -q` exited 0 and left
the destination untouched, which then showed an empty `git diff` and read as "the
repo already matches". Brace expansion in a remote path (`host:dir/{a,b}`) fails
outright. Copy one file at a time and hash both ends afterwards.

**`python` on Git Bash cannot open POSIX-style absolute paths** passed as string
literals (`/c/Users/...`). It fails quietly if stderr is redirected, and you get
empty hashes that look like empty files.

**A proxy may be rewriting your command.** `pytest` came back as
"No tests collected" for a suite that runs fine directly, and `git diff` and
`grep` are compacted in transit. When output looks structurally wrong rather
than factually wrong, run the tool by its direct path and compare.

## The rule under all of these

Before believing a measurement, answer two questions. What would this command
print if the thing I am testing were broken? And what would it print if the
command itself were broken? If those two answers are the same, the probe cannot
tell you anything, and you have to change the probe before you change the
system.
