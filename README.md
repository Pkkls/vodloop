# vodloop

A 24/7 Kick channel that plays a queue of YouTube videos, with the queue driven
by chat. One ffmpeg process holds the connection to Kick and never restarts, so
adding, skipping or reordering never interrupts the broadcast.

## How it works

```
chat -> kickbus (signed webhooks) -> chat.py -> queue.json
                                                   |
                              prep.py: yt-dlp | ffmpeg -> uniform 720p30 chunks
                                                   |
                              feeder.py -> FIFO -> pusher.sh -> RTMPS -> Kick
```

Five processes, each with its own lifecycle:

| Component | Role |
|---|---|
| `bin/chat.py` | reads chat events, applies commands to the queue |
| `bin/chatlogic.py` | all command handling and limits, no I/O, tested directly |
| `bin/prep.py` | streams a URL through ffmpeg into uniform MPEG-TS chunks |
| `bin/feeder.py` | feeds chunks into the FIFO with a cumulative timestamp offset |
| `bin/pusher.sh` | the single ffmpeg that talks to Kick |
| `bin/dashboard.py` | control panel and stream overlay |

## The two things that make it work

**A placeholder writer holds the FIFO open.** Without it, ffmpeg sees EOF the
moment one chunk ends and exits before the next starts, which ends the live
stream on Kick, creates a new VOD and drops every viewer.

```sh
sleep infinity > pipe &
```

**Chunks are remuxed with a cumulative offset, not concatenated.** A plain `cat`
makes each chunk restart its timestamps at zero, which the muxer reports as
"DTS out of order". The offset is persisted, so restarting the feeder resumes
the timeline instead of sending timestamps backwards.

Measured over 19 junctions: 6000 of 6000 frames delivered, 200.031s of output
for 200s of input, no timestamp disorder, resident memory flat at 50 MB.

## systemd layout

`vodloop-push` holds the connection and must never be restarted by a deployment.
Everything else lives in separate units and can be restarted freely. This split
is not cosmetic: systemd kills a whole control group on restart, so a placeholder
writer sitting in the feeder unit would take the stream down on every feeder
restart.

Verified live: restarting `vodloop-feed` left the pusher PID unchanged and the
channel online throughout.

## Chat commands

```
!play <youtube link>   add a video to the queue
!vote <n>              vote for a queued item, most voted plays first
!skip                  vote to skip, several distinct people required
!help                  list the commands
!ban <user id>         moderators only
```

## Hardening

Chat is the only surface strangers can reach, so it is treated as hostile input.

A chat message never reaches the downloader. The video id is extracted and a
canonical URL is rebuilt from scratch, which makes other hosts, `file://`,
internal addresses, playlists and channels structurally impossible rather than
merely filtered. The rebuilt URL always starts with `https://`, so it cannot be
read as a command-line flag.

Beyond that: per-user cooldown and pending cap, global queue cap, duration cap,
one vote per person per item, several distinct voters plus a cooldown for a
skip, moderator commands gated by an explicit id list, bounded state files, and
a disk floor below which preparation stops.

Titles come from YouTube, so they are hostile too. They are written with
`textContent` in both pages, never as markup, and drawn from a file with
`expansion=none` so a title containing `%{...}` or filter syntax is rendered
literally instead of evaluated.

Results:

```
adversarial tests                      15/15
mutation check, 4 guards removed       4/4 turn red
hostile chat stream                    112 messages, 1 legitimate item kept
burst load                             5000 messages in 0.20s
```

The mutation check matters as much as the suite: a green suite written by the
same hand as the code proves nothing until it has been seen to fail.

## Setup

Requires ffmpeg with libx264, libfreetype and the flv muxer, plus Python 3 and
yt-dlp.

```sh
cp systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vodloop-push vodloop-feed vodloop-prep vodloop-web vodloop-chat
```

Configuration lives in `.env` (mode 600, never committed):

```
KICK_INGEST=rtmps://<host>:443/app
KICK_STREAM_KEY=<key>
KICK_SLUG=<channel slug>
VODLOOP_MODS=<comma separated user ids>
```

The dashboard reads `VODLOOP_TOKEN` from `web.env` and listens on loopback only.
Put a reverse proxy with TLS in front of it before exposing it.

## Limits

Only publicly reachable videos are handled. A video behind a sign-in check is
recorded as an error on its queue item, and there is deliberately no support for
supplying an account session.

Encoding is the tight resource. On two vCPUs, normalising 1080p60 to 720p30 runs
at 0.99x real time, so 30fps sources leave comfortable headroom and 60fps sources
do not. Preparation runs ahead of playback and yields CPU to the pusher.

## Tests

```sh
python3 tests/test_chatlogic.py
```
