# vodloop

A 24/7 Kick rerun channel. It fetches archived streams from YouTube, cuts them
into chunks and pushes them to Kick over RTMPS. One ffmpeg process holds the
connection and never restarts, so nothing in the pipeline above it can interrupt
the broadcast.

Every number below was measured on the machines that run it. Where a figure is
load bearing, the measurement is given with it.

## The rule everything else follows from: never encode

The server is two vCPU and they already carry the live push. A full re-encode
runs at **0.06x real time** there. The channel consumes at 1x forever, so a
single re-encode is a deficit no scheduling recovers from.

So nothing is encoded. Videos are downloaded in exactly the shape the wire
needs, and every stage after that is a copy: `-c:v copy -c:a copy` into MPEG-TS
chunks, concatenated into a FIFO, `-c copy` into FLV. The check that this is
still true runs every five minutes and compares each chunk against the library
file it was cut from.

Three constraints force that shape, and none of them is a preference.

**RTMP/FLV carries h264 and AAC 44100, nothing else.** Measured by feeding each
codec to the real muxer:

```
av1  -> flv:  Video codec av1 not compatible with flv
vp9  -> flv:  Video codec vp9 not compatible with flv
opus -> flv:  Audio codec opus not compatible with flv
              FLV does not support sample rate 48000
h264 + aac 44100 -> flv: exit 0          (control)
```

YouTube serves AV1 or VP9 with opus by default, which is why the downloader asks
for `avc1` and `mp4a` explicitly. Without that constraint every arrival needs a
full re-encode, and for a while every arrival got one.

**Kick's ingest relays 30 or 60 fps and silently re-encodes anything else.** Fed
50, it does not refuse the stream. It rebuilds it, and the viewer sees Kick's
version. Measured on the live channel:

| library | source rung advertised | source rung actually delivered |
|---|---|---|
| 50 fps | 1920x1080@50, 3 070 272 | **1280x720** |
| 60 fps | 1920x1080@60, 4 295 657 | 1920x1080@60, decodes clean |

**The source rung has to lead the bandwidth ladder.** A player takes the fattest
variant it can afford, and Kick advertises its own 720p60 encode at a fixed
3 422 999. Any source reaching the wire under roughly 3.1 Mbps is outbid by it,
whatever its frame rate, and every player with the bandwidth picks a 720p encode
and upscales it back to 1080. That is a picture fault with no outage, no error
and no dropped frame, so nothing but an explicit check finds it.

## Two machines, and why

```
                    ORACLE (public, 2 vCPU, 45 GB)
  collector.py  ->  yt2oracle/inbox.txt
                          |  claimed on a 5 min tick
                          v
                    BOARD (RISC-V, 1 core, 15 GB SD, residential IP, behind NAT)
                    yt2oracle-inbox-pull -> urls.txt
                    yt2oracle-queue      -> one URL per tick, under a lock
                    yt2oracle            -> yt-dlp (avc1+mp4a) -> scp
                          |
                          v
  /home/ubuntu/videos/*.mp4
          |
       prep.py     -c copy -> segments/*.ts        (300 s chunks)
          |
      feeder.py    cumulative timestamp offset
          |
        FIFO
          |
      pusher.sh    ffmpeg -c copy -f flv -> Kick RTMPS
```

The board exists because **YouTube blocks the server's datacenter IP**. From
Oracle, `yt-dlp` on a video returns `Sign in to confirm you're not a bot`. From
the board's residential IP the same call works with no cookies and no account.
Any design that moves downloading back to the server is dead on arrival.

The block is on the player API, not on playlist listing. Oracle can still run
`yt-dlp --flat-playlist` against a channel, which is how the collector knows what
exists and how long each one is: 4596 durations in 70 s, and none of it costs the
board a second.

The board is behind NAT, so it always initiates. It claims the inbox with an
atomic rename, and publishes its own queue depth and free space back to the
server so the collector can see the far side.

## What decides what gets fetched

Not a file count. Three files might be twenty minutes or six hours, and the
channel does not consume files, it consumes time. Everything is decided on
**runway**: chunks already cut, plus every library file with a play left in it.

Below `MIN_RUNWAY_SECONDS` (1 h) nothing may be deleted, whatever else the rules
say, which is what makes an empty channel structurally impossible rather than
unlikely. Below four hours the collector stops taking the pool in order and
takes the shortest candidates it can measure, because what matters then is which
video is back on the shelf soonest: a 12 min video is there in three, a 12 h one
is not there for half an hour.

Above that it goes back to taking one source at a time in rotation, which keeps
the rotation varied instead of all-short.

It also tops up only to a shallow depth on the board. The board drains its queue
oldest first, so anything queued behind a backlog is a decision made hours ago
under conditions that have since changed.

## Components

| Component | Role |
|---|---|
| `bin/collector.py` | picks what to fetch, on runway and board depth. Cron, 30 min |
| `claw/yt2oracle*` | the board: claims URLs, downloads avc1+mp4a, uploads, reports |
| `bin/prep.py` | cuts library files into chunks, copying rather than encoding |
| `bin/feeder.py` | feeds chunks into the FIFO with a cumulative timestamp offset |
| `bin/pusher.sh` | the single ffmpeg that talks to Kick |
| `bin/janitor.py` | retires played files under disk pressure, on runway. Cron, 15 min |
| `bin/medic.py` | restarts push or prep when they fail, or when the panel asks. Cron, 4 min |
| `bin/quality.py` | measures source against wire, and Kick's ladder. Cron, 5 min |
| `bin/chat.py` | reads chat events, applies commands to the queue |
| `bin/chatlogic.py` | command handling and limits, no I/O, tested directly |
| `bin/allowlist.py` | the channel allowlist, managed by hand |
| `bin/dashboard.py` | control panel and stream overlay |
| `bin/tgbot.py` | Telegram reports and alerts |
| `bin/normalise.py` | the encoder. Deliberately not scheduled |

`normalise.py` is the one thing here that can encode, and it is paused. It stays
paused unless the rule at the top of this file is deliberately revisited.

## The two things that keep the stream up

**A placeholder writer holds the FIFO open.** Without it ffmpeg sees EOF the
moment one chunk ends and exits before the next starts, which ends the live
stream on Kick, creates a new VOD and drops every viewer.

```sh
sleep infinity > pipe &
```

**Chunks are remuxed with a cumulative offset, not concatenated.** A plain `cat`
makes each chunk restart its timestamps at zero, which the muxer reports as
`DTS out of order`. The offset is persisted, so restarting the feeder resumes the
timeline instead of sending timestamps backwards.

Measured when the feeder was written, over 19 junctions: 6000 of 6000 frames
delivered, 200.031 s of output for 200 s of input, no timestamp disorder,
resident memory flat at 50 MB. That figure has not been re-taken since, and the
standing check on it is `quality.py`, which would see the timeline break.

`vodloop-push` holds the connection and must never be restarted by a deployment.
Everything else lives in its own unit and can be restarted freely. The split is
not cosmetic: systemd kills a whole control group on restart, so a placeholder
writer sitting in the feeder unit would take the stream down every time the
feeder restarted. Verified live: restarting `vodloop-feed` left the pusher PID
unchanged and the channel online throughout.

"Freely" is true of `vodloop-prep` since 2026-09-12 and was not before. prep
writes chunks into `segments/` while the feeder is already eating them, and it
only consults its look-ahead limit between videos, so the item in flight can be
holding hours of runway. The orphan recovery on startup deleted all of it, and
three separate things restart that unit: `deploy/harden-oracle.sh` whenever it
lays a drop-in, `medic.py` after three dry passes, and a person picking up a fix.
It now keeps every chunk the muxer had closed and drops only the last one, which
is the only one that can be half written, because the segment muxer holds one
open at a time and never returns to an earlier one.

That is also why `vodloopctl` now has one restart verb, and only one:

```sh
sudo vodloopctl prep-restart
```

The panel has the same control, and reaches it the long way round. `vodloop-web`
is sandboxed with `NoNewPrivileges=yes` by `deploy/harden-oracle.sh`, which is
exactly what stops a setuid binary elevating, so a panel that shelled out to
`sudo` would work on a box the hardening pass had not reached yet and break
without a word the day it did. Instead the panel drops a request in `state/`,
which is in its `ReadWritePaths`, and `medic.py` — cron, no sandbox, and already
the owner of this restart, its cooldown and its announcement — carries it out on
its next pass. A request older than fifteen minutes is ignored rather than
honoured late, and nothing ever reads the file's contents, only its age.

## Monitoring

`quality.py` writes one JSON line every five minutes and answers two questions
the rest of the system cannot.

It finds the library file each chunk came from and reports source against wire.
If the codec, size and frame rate match, the packets were copied and there is no
generation to lose. If they ever differ it prints `REENCODAGE` with both shapes,
which is the regression detector for the rule at the top of this file.

Then it reads Kick's master playlist and checks that the source rung is not
outbid by a rung carrying fewer pixels than the chunk currently going out. That
is the one fault a perfect pipeline can still produce, because it happens after
the bytes leave. The comparison is against the chunk and not against the size
Kick prints beside the source rung, for the reason under Limits: that size is
frozen at the start of the session and stops describing the picture the moment a
video of another shape comes round.

Thresholds here are deliberately thin. Absolute bitrate floors have been wrong
about this library three times: 150k of audio against YouTube's 128k, then
2.4 Mbps of video, then a 40 528 s stream YouTube serves at 690 kbps in 720p30.
Each time the floor encoded an assumption about a library that then changed. The
floors now only speak where the comparison to the source cannot, meaning the
chunk did not match its source or there is no source. Where a copy did happen,
the wire is the source and thin content is content.

## Chat commands

```
!vods                  list what can be played, paged six at a time
!play <number|words>   queue one of them, by number or by title
!vote <n>              vote for a queued item, most voted plays first
!skip                  vote to skip, several distinct people required
!next                  what is playing and what follows
!help                  list the commands
!ban <user id>         moderators only
```

`!play` takes a number or words from the list rather than a link, because the
allowlist below is what decides admissibility and the list is already filtered by
it. `!list` and `!vod` are aliases for `!vods`, `!add` for `!play`, `!v` for
`!vote`, `!queue` for `!next`. Replies are written in the channel's language, not
this one.

## Channel allowlist

The queue is closed by default. A video is prepared only if its publishing
channel is on an explicit list, managed by hand:

```sh
python3 bin/allowlist.py add https://www.youtube.com/@somechannel
python3 bin/allowlist.py list
python3 bin/allowlist.py remove UCxxxxxxxxxxxxxxxxxxxxxx
```

This is the difference between a channel that survives and one that does not.
Anyone in chat can queue anything YouTube hosts, and nothing in a title or a
transcript reliably predicts whether a video gets the Kick channel taken down.
Who published it does.

Channel ids are stored rather than handles, because a handle can be changed or
reassigned and an id cannot. The check runs at preparation time, where the
publisher is known, and a refused item is marked on the queue rather than
silently dropped. A missing, empty or malformed list allows nothing: a broken
list must take the channel off the air, never open it up.

## Hardening

Chat is the only surface strangers reach, so it is treated as hostile input.

A chat message never reaches the downloader. The video id is extracted and a
canonical URL is rebuilt from scratch, which makes other hosts, `file://`,
internal addresses, playlists and channels structurally impossible rather than
merely filtered. The rebuilt URL always starts with `https://`, so it cannot be
read as a command line flag.

Beyond that: per-user cooldown and pending cap, global queue cap, duration cap,
one vote per person per item, several distinct voters plus a cooldown for a skip,
moderator commands gated by an explicit id list, bounded state files, and a disk
floor below which preparation stops.

Titles come from YouTube, so they are hostile too. They are written with
`textContent` in both pages, never as markup, and drawn from a file with
`expansion=none` so a title containing `%{...}` or filter syntax is rendered
literally instead of evaluated.

`tests/test_chatlogic.py` is the adversarial suite for all of it, 31 checks, and
it runs against the parser directly with no I/O. The mutation check that went
with it, removing four guards one at a time and confirming each turns the suite
red, was run when the guards were written and is not re-run automatically. A
green suite written by the same hand as the code proves nothing until it has been
seen to fail.

## Setup

Requires ffmpeg with the flv muxer, Python 3 and yt-dlp on both machines.
libx264 is needed only by `normalise.py`, which is not scheduled, and a running
`libx264` anywhere is a fault by itself:

```sh
ps -eo args | grep -c "[l]ibx264"    # must be 0
```

```sh
cp systemd/*.service systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vodloop-push vodloop-feed vodloop-prep \
                      vodloop-web vodloop-chat vodloop-bus vodloop-tg
```

Configuration lives in `.env` (mode 600, never committed):

```
KICK_INGEST=rtmps://<host>:443/app
KICK_STREAM_KEY=<key>
KICK_SLUG=<channel slug>
VODLOOP_MODS=<comma separated user ids>
```

The dashboard reads `VODLOOP_TOKEN` from `web.env` and listens on loopback only.
`deploy/nginx-vodloop.conf` is the reverse proxy in front of it; run
`certbot --nginx` against that host to add the certificate.

The board's four scripts go in `/usr/bin` and are driven entirely by cron:

```
*/5  * * * *  yt2oracle-inbox-pull ; yt2oracle-queue ; yt2oracle-status-push
```

`yt2oracle-queue` serialises the board with a lock that records its owner's pid,
and `/proc` answers whether that owner is alive. An age based lock does not work
here: the threshold was set when the longest run took six minutes, the duration
cap later went from three hours to twenty four, and a download half an hour into
an eleven hour video was then declared orphaned while it was still running. At
one tick every five minutes that is six concurrent downloads in half an hour on
one core and one SD card.

## Chat wiring

Chat arrives through kickbus, which verifies Kick's RSA signature on every
webhook and republishes the events locally as SSE. It is a separate Go project
and **its source is not in this repository**: only the built binary is deployed,
at `bin/kickbus`, which `vodloop-bus.service` runs. kickbus and the dashboard
both listen on loopback, and only the webhook path and the panel are proxied.

```
Kick -> https://<host>/kick/webhook -> kickbus :8787 -> /events -> chat.py
```

The webhook path carries no dashboard token on purpose. Authenticity there is the
signature, not a shared secret, and Kick has no way to send one.

To connect it, create an app at kick.com/settings/developer, point its webhook at
`https://<host>/kick/webhook`, subscribe to `chat.message.sent`, and put the
credentials in `bus.env`. Then subscribe the broadcaster:

```sh
bin/kickbus -subscribe -broadcaster <user id>
```

The id is sent as an integer, not a string, or the subscription is rejected.

## Limits

Only publicly reachable videos are handled. A video behind a sign-in check is
recorded as an error on its queue item, and there is deliberately no support for
supplying an account session.

**Resolution is mixed on air, and Kick's label for it is not.** A 720p-only video
airs at 720p rather than being upscaled, which is the deliberate choice. The FLV
muxer accepts the change and the ingest carries it. What the ingest does not do
is follow it: it reads the resolution out of the sequence header when the RTMP
session opens and advertises that for the whole live. Observed 2026-09-12, the
source rung is still announced `1920x1080@60` hours after 720p files started
going through it.

The label belongs to the session, so only a new session can correct it, and a new
session is an ended stream, a closed VOD and no viewers. It is therefore left
alone, and two things follow from leaving it alone.

`quality.py` measures the ladder against the chunk on the wire rather than
against the label. Taken from the label, every 720p VOD in this pool reads as a
1080p source outbid by a smaller rung, which is a standing alert every six hours
for the eleven hours one of them lasts, about pixels nobody is losing — and it
would be raised by the one check that catches a real picture fault.

And the feeder opens a session the pusher has just started with the 1080p60
standby clip, so the label the channel is then stuck with is the best shape the
library holds rather than whatever chunk happened to be at the head of the queue.
This one is a decision, not a measurement: it is twenty seconds of standby clip
spent at the only moment there are no viewers to spend it on. It lands rather
than races because `vodloop-feed` is `BindsTo=` the pusher, so a pusher restart
takes the feeder with it and the feeder meets the new session at the top of its
loop; were it ever mid-chunk instead, the label would be what it would have been
anyway.

**The board's card is the ceiling on duration, not the server.** yt-dlp needs the
video track, the audio track and the merged output on the card at once, so the
finished file stays under about a third of 15 GB, and a hard 4 GB cap aborts
before the bytes land. What that allows depends entirely on the source: the long
IRL streams this channel replays run 690 kbps in 720p30, so 4 GB is 12.9 hours of
them. Across the 4570 videos currently in the pool, 19 739 hours in total, the
longest is 12.0 hours and none exceeds what already fits.

## Tests

```sh
for t in tests/test_*.py; do VODLOOP_ROOT=$(mktemp -d) python3 "$t"; done
```

Several tests do not isolate `VODLOOP_ROOT` on their own, so run them against a
throwaway root or they write into live state.
