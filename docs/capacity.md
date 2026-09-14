# Capacity

Disk is the resource that takes channels off the air, and it does it by a route
that looks like something else: the encoder refuses to write below a free space
floor, so a full disk presents as an empty chunk queue and a grey picture, not as
a disk error.

## The floor, and why restarting does not help

`disk_is_tight()` stops the encoder below `MIN_FREE_BYTES`, currently 4 Gio. This
is deliberate: a disk that fills completely corrupts whatever is being written,
and losing the library is worse than losing the picture for an hour.

The consequence catches people out. Below the floor, **restarting the encoder
produces nothing**, because it checks and refuses before writing. The repair
agent used to restart it three passes running against a disk at 3.8 Go free, and
the channel sat on the standby clip for twenty minutes while the repair fired
uselessly each pass. The repair agent now frees space before it restarts
anything.

## Sizing a share

Each channel has a byte budget passed through the environment. The janitor
retires files to hold the library under it, and the collector stops fetching when
it is served.

**Enumerate the whole disk. Do not assemble a total from directories you
remember.** A share was once raised on a "system" figure of 7 Go assembled from
three directory names. The real figure was 13 Go: two channel roots, the
package cache and the scratch directory were not in the list. The share went up
by more than the disk had, the floor was breached, and a channel went to the
standby clip.

    du -sBM /* 2>/dev/null | sort -rn | head
    du -sBM /home/<user>/* 2>/dev/null | sort -rn | head
    df -BM /

Then the arithmetic, from measured numbers only:

    available for libraries = total
                            - system (everything not a library and not chunks)
                            - chunks (ahead limit x channels, about 3 Go each)
                            - floor (4 Gio)
                            - margin (3 Go, for the errors above)

Split what is left between the channels. The simplest correct form, when there is
one channel left, is to compute it from the live disk rather than from a model:

    budget = current library size + (free - floor - margin)

That is measurement plus headroom, and it cannot be wrong about the parts of the
system you forgot, because they are already inside `free`.

Sanity check afterwards: the collector prints `part=X/YG libre=ZG` on every run,
and the janitor prints the target it would retire down to. If the collector says
the share is served while the disk is nearly empty, the budget is too small; if
free space is near the floor while the share has room, it is too large.

## What the guards do, and what they do not

The collector refuses when **either** the share is served or free space is near
the floor. That second condition is why an over-large budget does not fill the
disk immediately: the collector stops before it does. It stops the growth, it
cannot undo it.

The janitor holds the library under the share. **It cannot enforce anything when
every file is protected.** It keeps the newest N playable files so a channel
always has something to rotate, and if the library has fewer files than that, the
protected set is the whole library and it reports zero bytes freeable while
sitting over budget. This is correct behaviour on a healthy disk and it is
exactly how one filled: a library of nine large files, all protected, over its
share, with nothing able to bring it back.

Underneath the janitor, the repair agent has an emergency that only fires below
the floor: one library file, already broadcast, holding no chunks, never dropping
below three playable files. A grey screen now is worse than less variety later,
but an empty library is the same grey screen tomorrow.

## Scratch is not free

4.5 Go of diagnostic leftovers accumulated in the scratch directory over several
debugging sessions, one file of it 2.6 Go. That was a third of the disk pressure
in the 2026-09-14 outage, and it was created by the person debugging the disk.

The repair agent now clears it automatically: older than two hours, larger than
64 Mo, owned by the service user, and never a lock file or a probe the encoder is
holding. Those exclusions matter. A repair that deletes a lock in use is a worse
fault than the one it came to fix.

This does not excuse leaving it. Probes that write hundreds of megabytes should
clean up when they finish.

## Where the bytes actually go

A rough shape, so a listing that looks wrong is recognisable:

- **library**, by far the largest, one video is 0.4 to 3.7 Go at 1080p
- **chunks**, bounded by the ahead limit, about 3 Go per active channel
- **system**, packages, logs, swap, roughly 9 Go and stable
- **scratch**, should be near zero and is the first thing to check when it is not
- **failed downloads**, which accumulate quietly and are nobody's job to delete

File size drives rotation length far more than the budget does. At 1080p a 20 Go
share holds about seven of these videos; the same videos at 720p are roughly half
the size, so the same disk holds twice the rotation. That is a real trade between
picture quality and variety, and it is a decision for whoever owns the channel,
not a technical default.

## The measurement to take before any capacity change

    df -h /                                  # where you are
    du -sBM /* | sort -rn | head             # where it went
    <collector, dry run>                     # what the share thinks
    <janitor, dry run>                       # what it could free, and what it refuses to

Four commands. The 2026-09-14 outage came from skipping the second one.
