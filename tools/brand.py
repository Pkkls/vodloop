#!/usr/bin/env python3
"""Channel artwork for Kick: one avatar, one banner.

    python3 tools/brand.py <channel> [outdir]

What this channel actually airs is long IRL streams, most of them five hours
and up, whole days abroad. So the artwork is about a day passing, not about a
loop: the first draft was a green replay arrow around the digits 247, which is
what every channel with 247 in its name already looks like.

The avatar is a sunrise at the horizon, drawn edge to edge because Kick crops
it to a circle, and readable as a warm band with a bright dot at the forty
pixels it becomes beside a chat message. The banner is a twenty four hour
timeline with a playhead on it, which is the only shape that makes sense of a
strip nine times wider than it is tall and is also, literally, what the channel
is.

Type is Bahnschrift Bold Condensed, the DIN that signage and departure boards
are set in, because the subject is travel and because DejaVu is what everything
else already looks like.

Sizes are what Kick asks for, doubled so they stay sharp: the banner floor is
1200x134 and the avatar has to sit between 128 and 3000 square.
"""
import pathlib
import sys

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

# A day, in the order it happens. Not a palette picked to look technical.
NIGHT = (7, 11, 24)
DEEP = (20, 34, 78)
BLUE_HOUR = (58, 42, 82)
DAWN = (232, 112, 58)
GLOW = (255, 194, 122)
NOON = (255, 222, 175)
DUSK = (255, 94, 58)
LAND = (6, 8, 14)
WARM = (246, 239, 228)
MUTED = (150, 140, 158)

WIN = pathlib.Path("C:/Windows/Fonts")
DISPLAY = str(WIN / "bahnschrift.ttf")
DISPLAY_FACE = "Bold Condensed"
try:
    import matplotlib
    _MPL = pathlib.Path(matplotlib.__file__).parent / "mpl-data" / "fonts" / "ttf"
    MONO = str(_MPL / "DejaVuSansMono-Bold.ttf")
    FALLBACK = str(_MPL / "DejaVuSans-Bold.ttf")
except ImportError:  # pragma: no cover
    MONO = FALLBACK = str(WIN / "consolab.ttf")

SCALE = 3  # drawn this much larger, then reduced: gradients and arcs need it


def display(size):
    """The signage face, at its condensed bold instance.

    Bahnschrift is one variable font and loads at Regular, which is too light
    to be a wordmark. A machine without it falls back rather than failing.
    """
    try:
        font = ImageFont.truetype(DISPLAY, size)
        font.set_variation_by_name(DISPLAY_FACE)
        return font
    except (OSError, AttributeError):
        return ImageFont.truetype(FALLBACK, size)


def split_name(channel):
    """A channel like abc247 reads as a name and a number, and the two are set
    differently. A name with no trailing number keeps all of it."""
    head = channel.rstrip("0123456789")
    return head or channel, channel[len(head):]


def ramp(stops, n, vertical=True):
    """A gradient of n pixels from (position, colour) stops.

    Built as an array and stretched, because a python loop over nine million
    pixels takes longer than the rest of this file put together.
    """
    pos = np.array([p for p, _ in stops], dtype=float)
    cols = np.array([c for _, c in stops], dtype=float)
    t = np.linspace(0.0, 1.0, n)
    out = np.stack([np.interp(t, pos, cols[:, k]) for k in range(3)], axis=1)
    out = out.astype(np.uint8)
    return out.reshape(n, 1, 3) if vertical else out.reshape(1, n, 3)


def tape(im, spacing, strength):
    """Thin dark lines across the picture.

    Everything here is a recording of a day that already happened, and this is
    the cheapest way to say so without drawing a tape or a play button.
    """
    lines = Image.new("L", im.size, 0)
    d = ImageDraw.Draw(lines)
    for y in range(0, im.size[1], spacing):
        d.line([(0, y), (im.size[0], y)], fill=strength, width=max(1, spacing // 6))
    im.paste(Image.new("RGB", im.size, (0, 0, 0)), (0, 0), lines)
    return im


def tracked(d, xy, text, font, fill, tracking):
    """Letter-spaced text, which Pillow will not do on its own."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += d.textlength(ch, font=font) + tracking
    return x - xy[0] - tracking


def width_of(d, text, font, tracking):
    return sum(d.textlength(c, font=font) + tracking for c in text) - tracking


def hour_colour(hour):
    """The colour of an hour of the day, 0 to 24.

    One ramp, used by both surfaces: the banner is this unrolled and the avatar
    is the same thing bent into a circle. Two marks built from one idea read as
    one channel; two marks built from two ideas read as two.
    """
    cycle = [(0.00, NIGHT), (0.17, DEEP), (0.26, DAWN), (0.36, NOON),
             (0.58, NOON), (0.70, DUSK), (0.80, DEEP), (1.00, NIGHT)]
    row = ramp(cycle, 24 * 8, vertical=False)[0]
    return tuple(int(v) for v in row[min(int(hour / 24 * 24 * 8), 24 * 8 - 1)])


def avatar(channel, size=1024, now_hour=14.5):
    """A day as a dial: twenty four blocks of an hour, and a mark on the one
    that is playing.

    The draft before this was a sunrise with scanlines over it. It was pretty
    and it was stock: the same picture sits behind half the lo-fi channels on
    the internet and said nothing about this one. Blocks say something. They
    say the day is the unit here, which is exactly what a channel airing five
    to fifteen hour streams is about.

    Drawn edge to edge because Kick crops it to a circle. At the forty pixels
    it becomes beside a chat message the blocks blur into one warm-to-dark ring
    with a bright nick in it, which is still nobody else's avatar.
    """
    s = size * SCALE
    im = Image.new("RGB", (s, s), (6, 8, 16))
    d = ImageDraw.Draw(im)

    outer = int(s * 0.412)
    thick = int(s * 0.140)
    box = [s // 2 - outer, s // 2 - outer, s // 2 + outer, s // 2 + outer]
    gap = 1.6  # degrees, so the blocks read as blocks and not as a gradient

    # midnight at the top, noon at the bottom, clockwise. PIL measures from
    # three o'clock, hence the quarter turn.
    for hour in range(24):
        a0 = hour * 15 - 90 + gap / 2
        d.arc(box, start=a0, end=a0 + 15 - gap, fill=hour_colour(hour + 0.5),
              width=thick)

    # No glow in the middle. A blurred warm ellipse in there read as a stain on
    # the lens rather than as light, and the dial is stronger as a clean ring
    # around a hole.

    # The playhead sits in the dark margin OUTSIDE the ring, not across it.
    # Drawn through the blocks it was a white thread nobody could see, and
    # colouring the current block white fails too: land it on a pale afternoon
    # hour and it disappears into its neighbours. The margin is dark all the way
    # round, so a warm mark there reads whatever hour it points at.
    import math
    a = math.radians(now_hour * 15 - 90)
    side = math.radians(4.6)
    tip, base = outer + s * 0.012, outer + s * 0.064
    d.polygon([(s / 2 + tip * math.cos(a), s / 2 + tip * math.sin(a)),
               (s / 2 + base * math.cos(a - side), s / 2 + base * math.sin(a - side)),
               (s / 2 + base * math.cos(a + side), s / 2 + base * math.sin(a + side))],
              fill=WARM)

    tape(im, max(2, int(s * 0.024)), 9)
    return im.resize((size, size), Image.LANCZOS)


def banner(channel, width=2400, height=268):
    """A day, twice over, with a playhead on it.

    Nine to one is the shape of a timeline and of nothing else, so that is what
    this is: the colour along the strip is the hour, the ticks are what hour,
    and the playhead is where the channel happens to be. It never reaches the
    end because there is not one.
    """
    w, h = width * SCALE, height * SCALE
    name, number = split_name(channel)
    im = Image.new("RGB", (w, h), NIGHT)
    d = ImageDraw.Draw(im)

    # Two and a half days, an hour to a block. Blocks rather than a smooth
    # ramp, and the same twenty four the avatar is made of: a gradient is
    # decoration and you read nothing off it, while blocks are a timetable and
    # you can count them. Unrolled here, rolled up there, one idea twice.
    days = 2.5
    per = w / days
    block = per / 24
    gap = max(1, int(block * 0.11))
    top, bot = int(h * 0.50), int(h * 0.795)
    k = 0
    while k * block < w:
        x = k * block
        d.rectangle([int(x), top, int(x + block) - gap, bot],
                    fill=hour_colour(k % 24 + 0.5))
        k += 1

    # hours under it, small, so the strip reads as a clock and not as a ribbon
    hours = ImageFont.truetype(MONO, int(h * 0.072))
    track = h * 0.03
    for k in range(int(days * 4) + 1):
        x = k * per / 4
        if x > w - h * 0.1:
            break
        label = "%02d" % ((k % 4) * 6)
        d.rectangle([x, bot, x + max(1, int(w * 0.0008)), bot + int(h * 0.045)],
                    fill=(70, 62, 80))
        tracked(d, (x + h * 0.03, bot + int(h * 0.075)), label, hours, MUTED, track)

    # The playhead marks the hour by ringing its block, not by drawing a line
    # through it: run across a pale afternoon the line vanished, which is the
    # same contrast problem the avatar's marker had. The head stays above, in
    # the dark, where it is legible whatever colour the hour is.
    which = int(w * 0.615 / block)
    bx = which * block
    head = int(h * 0.055)
    d.rectangle([int(bx), top, int(bx + block) - gap, bot],
                outline=WARM, width=max(2, int(h * 0.022)))
    mid = int(bx + (block - gap) / 2)
    d.polygon([(mid - head, top - int(h * 0.10)), (mid + head, top - int(h * 0.10)),
               (mid, top - int(h * 0.015))], fill=WARM)

    pad = int(w * 0.038)
    title = display(int(h * 0.36))
    bb = d.textbbox((0, 0), name, font=title)
    d.text((pad, int(h * 0.055) - bb[1]), name, font=title, fill=WARM)
    if number:
        d.text((pad + d.textlength(name, font=title), int(h * 0.055) - bb[1]),
               number, font=title, fill=DAWN)

    # Set against the wordmark rather than floating above it: the first line
    # sits on its baseline and the second under it, so the top half reads as one
    # row and not as two things that happen to share a strip.
    note = ImageFont.truetype(MONO, int(h * 0.082))
    small = ImageFont.truetype(MONO, int(h * 0.068))
    for k, (line, font, fill) in enumerate((
            ("SOMEBODY ELSE'S DAYS, ON A LOOP", note, WARM),
            ("FAN-RUN REBROADCAST. NOTHING HERE IS LIVE.", small, MUTED))):
        wide = width_of(d, line, font, track)
        tracked(d, (w - pad - wide, int(h * (0.13 + k * 0.155))), line, font,
                fill, track)

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
