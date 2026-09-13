#!/usr/bin/env python3
"""Channel artwork for Kick: one avatar, one banner, same look as the rest.

    python3 tools/brand.py <channel> [outdir]

The palette and the type are taken from assets/offline-banner.png, which is
what a viewer sees when the channel is down, and from prep.py, which draws the
title over the video in DejaVu Sans Bold. A channel whose banner, standby card
and on-air overlay disagree looks like three channels.

Sizes are what Kick asks for, doubled so the artwork stays sharp on a dense
screen: the banner floor is 1200x134 and the avatar has to sit between 128 and
3000 square. The avatar is drawn for the circle Kick crops it to, and for the
40 pixels it becomes next to a chat message, which is why the mark is a ring
and three digits and not a wordmark nobody could read.
"""
import pathlib
import sys

from PIL import Image, ImageDraw, ImageFont

BG = (12, 15, 22)
GREEN = (83, 252, 24)
WHITE = (255, 255, 255)
GREY = (139, 146, 160)
DIM = (26, 32, 48)

FONTS = pathlib.Path(__file__).resolve().parent / "fonts"
# matplotlib ships the same DejaVu the server draws titles with, so the artwork
# and the on-air overlay are the same typeface without shipping a font here
try:
    import matplotlib
    FONTS = pathlib.Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
except ImportError:
    pass
BOLD = str(FONTS / "DejaVuSans-Bold.ttf")
MONO = str(FONTS / "DejaVuSansMono-Bold.ttf")

SCALE = 3  # drawn this much larger, then reduced: round shapes need it


def split_name(channel):
    """A channel like abc247 reads as a name and a number. Both get their own
    weight in the artwork, and a name without a trailing number keeps all of
    it."""
    head = channel.rstrip("0123456789")
    return head or channel, channel[len(head):]


def tracked(draw, xy, text, font, fill, tracking):
    """Text with letters spaced out, which Pillow will not do on its own.

    Returns the width, so a caller can centre what it just drew or put
    something after it.
    """
    x, y = xy
    for ch in text:
        if draw is not None:
            draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking
    return x - xy[0] - tracking


def width_of(draw, text, font, tracking):
    x = 0
    for ch in text:
        x += draw.textlength(ch, font=font) + tracking
    return x - tracking


def avatar(channel, size=1024):
    """A ring, and the number inside it.

    Kick crops this to a circle and shows it at 40 pixels beside every chat
    message. So there is one shape and one word in it: a replay ring, because
    the whole channel is a loop, and the number, because that is the part of
    the name that survives being small.
    """
    s = size * SCALE
    im = Image.new("RGB", (s, s), BG)
    d = ImageDraw.Draw(im)
    name, number = split_name(channel)

    # the ring, left open at the top right for the arrow head
    import math
    r = int(s * 0.375)
    w = int(s * 0.052)
    box = (s // 2 - r, s // 2 - r, s // 2 + r, s // 2 + r)
    gap_at = -32
    # started a few degrees behind the head so the stroke runs under its base:
    # butted exactly against it, the flat cap leaves a nick in the ring
    d.arc(box, start=gap_at - 5, end=298, fill=GREEN, width=w)

    # The head sits ON the ring and points along it, not away from it. Built
    # from the radius it belongs to: tangent for the direction it travels,
    # radius for its thickness. Pointed outwards instead, it reads as a
    # triangle someone dropped next to a circle.
    a = math.radians(gap_at)
    u = (math.cos(a), math.sin(a))            # outwards
    t = (math.sin(a), -math.cos(a))           # along the ring, towards the gap
    cx, cy = s / 2 + r * u[0], s / 2 + r * u[1]
    reach, half = w * 1.55, w * 1.15
    back = w * 0.45
    d.polygon([(cx + t[0] * reach, cy + t[1] * reach),
               (cx - t[0] * back + u[0] * half, cy - t[1] * back + u[1] * half),
               (cx - t[0] * back - u[0] * half, cy - t[1] * back - u[1] * half)],
              fill=GREEN)

    # the number, as large as fits between the ring's inner edges
    digits = number or name[:3].upper()
    want = int(r * 1.32)
    font = ImageFont.truetype(BOLD, want)
    while d.textlength(digits, font=font) > r * 1.35:
        want = int(want * 0.96)
        font = ImageFont.truetype(BOLD, want)
    bb = d.textbbox((0, 0), digits, font=font)
    d.text(((s - (bb[2] - bb[0])) / 2 - bb[0],
            (s - (bb[3] - bb[1])) / 2 - bb[1] - s * 0.045), digits,
           font=font, fill=WHITE)

    # the name under it, small enough to read as a texture when the avatar is
    # forty pixels wide and as a word when it is not
    if number:
        small = ImageFont.truetype(MONO, int(s * 0.062))
        label = name.upper()
        track = s * 0.022
        wide = width_of(d, label, small, track)
        tracked(d, ((s - wide) / 2, s * 0.615), label, small, GREEN, track)

    return im.resize((size, size), Image.LANCZOS)


def banner(channel, width=2400, height=268):
    """The strip across the top of the channel page.

    Kick's floor is 1200x134 and the shape is nearly nine to one, so this is a
    line of type and nothing else. It says what the channel is in three words
    and then says what it is not, because a rerun channel that does not say so
    is a channel pretending to be live.
    """
    w, h = width * SCALE, height * SCALE
    im = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(im)
    name, number = split_name(channel)

    # A tape running under the type, filling a strip that is nine times wider
    # than it is tall and would otherwise be a hole in the middle. Ticks, with
    # a taller one every sixth, because what this channel actually is, is a
    # timeline that never stops. Dim on purpose: it is a texture, not a second
    # headline.
    pad = int(w * 0.042)
    step = int(w * 0.0105)
    for n, tx in enumerate(range(pad, w - pad, step)):
        tall = (n % 6 == 0)
        half = int(h * (0.085 if tall else 0.045))
        d.rectangle([tx, int(h * 0.5) - half, tx + max(1, int(w * 0.0011)),
                     int(h * 0.5) + half], fill=DIM)

    # the same left rule the standby card uses
    rule = int(h * 0.012)
    d.rectangle([pad, int(h * 0.20), pad + rule, int(h * 0.80)], fill=GREEN)

    x = pad + rule + int(w * 0.016)
    title = ImageFont.truetype(BOLD, int(h * 0.30))
    bb = d.textbbox((0, 0), name, font=title)
    top = int(h * 0.22) - bb[1]
    d.text((x, top), name, font=title, fill=WHITE)
    if number:
        d.text((x + d.textlength(name, font=title), top), number,
               font=title, fill=GREEN)

    sub = ImageFont.truetype(BOLD, int(h * 0.155))
    d.text((x, int(h * 0.575)), "VODs, around the clock", font=sub, fill=GREY)

    # what it is not, kept to the right so it reads as a footnote rather than a
    # headline, and in the mono the rest of the channel uses for machine talk
    note = ImageFont.truetype(MONO, int(h * 0.085))
    track = h * 0.028
    for n, line in enumerate(("FAN-RUN REBROADCAST", "NOTHING HERE IS LIVE")):
        wide = width_of(d, line, note, track)
        tracked(d, (w - pad - wide, int(h * (0.34 + n * 0.22))), line, note,
                GREY if n == 0 else GREEN, track)

    return im.resize((width, height), Image.LANCZOS)


def main(argv):
    if not argv:
        print(__doc__.strip())
        return 2
    channel = argv[0]
    out = pathlib.Path(argv[1] if len(argv) > 1 else f"assets/{channel}")
    out.mkdir(parents=True, exist_ok=True)
    for name, im in (("avatar.png", avatar(channel)),
                     ("banner.png", banner(channel))):
        path = out / name
        im.save(path, "PNG", optimize=True)
        print(f"{path.resolve()}  {im.size[0]}x{im.size[1]}  "
              f"{path.stat().st_size / 1024:.0f} ko")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
