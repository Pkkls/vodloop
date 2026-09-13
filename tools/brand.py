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


def avatar(channel, size=1024):
    """A sunrise, drawn to the edges because Kick crops it to a circle.

    No letters in it. The channel name is shown beside the avatar everywhere it
    appears, so spending the mark on repeating it buys nothing, and at forty
    pixels a word is a smudge while a warm band with a bright dot is still a
    warm band with a bright dot.
    """
    s = size * SCALE
    # High, so the black below it stays a base and does not become half the
    # mark: at forty pixels the useful part is the band, and a horizon at the
    # middle left it two pixels tall.
    horizon = 0.660

    sky = Image.fromarray(np.repeat(ramp([
        (0.000, NIGHT), (0.260, (12, 20, 50)), (0.440, DEEP),
        (0.560, BLUE_HOUR), (0.614, DAWN), (horizon, GLOW),
    ], s), s, axis=1))

    im = sky.copy()
    d = ImageDraw.Draw(im)

    # the sun, sitting in the horizon rather than above it: the land is drawn
    # over its lower third a few lines down
    r = int(s * 0.190)
    cx, cy = int(s * 0.430), int(s * 0.600)
    # the halo first, added rather than pasted, so it lights the sky instead of
    # sitting on it as a grey disc
    glow = Image.new("RGB", (s, s), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - r * 2.2, cy - r * 2.2, cx + r * 2.2, cy + r * 2.2],
                                 fill=(96, 44, 20))
    im = ImageChops.add(im, glow.filter(ImageFilter.GaussianBlur(s * 0.07)))
    d = ImageDraw.Draw(im)
    # the disc itself is not one flat colour: it cools towards the top and
    # burns where it meets the haze, which is what keeps it from reading as a
    # sticker
    body = Image.fromarray(np.repeat(ramp([
        (0.0, (255, 241, 214)), (0.6, NOON), (1.0, (255, 178, 104)),
    ], 2 * r), 2 * r, axis=1))
    disc = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(disc).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
    im.paste(body, (cx - r, cy - r), disc)

    land = Image.fromarray(np.repeat(ramp([
        (0.0, (14, 12, 20)), (0.25, LAND), (1.0, (3, 4, 8)),
    ], s - int(s * horizon)), s, axis=1))
    im.paste(land, (0, int(s * horizon)))
    # the line itself, thin and bright, where the light stops
    d.rectangle([0, int(s * horizon) - int(s * 0.004), s, int(s * horizon)],
                fill=(255, 176, 110))

    tape(im, max(2, int(s * 0.019)), 20)

    # a vignette, so the disc still reads as a disc once Kick rounds it off
    vg = Image.new("L", (s, s), 0)
    ImageDraw.Draw(vg).ellipse([int(-s * 0.18)] * 2 + [int(s * 1.18)] * 2, fill=255)
    vg = vg.filter(ImageFilter.GaussianBlur(s * 0.06))
    im = Image.composite(im, Image.new("RGB", (s, s), (2, 3, 7)), vg)

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

    # two and a half days across the strip, starting before dawn
    cycle = [(0.00, NIGHT), (0.17, DEEP), (0.25, DAWN), (0.34, NOON),
             (0.56, NOON), (0.68, DUSK), (0.78, DEEP), (1.00, NIGHT)]
    days = 2.5
    n = int(w * days)
    row = ramp(cycle, int(w / days) + 1, vertical=False)[0]
    strip = np.concatenate([row] * (int(days) + 1), axis=0)[:w]
    top, bot = int(h * 0.50), int(h * 0.795)
    im.paste(Image.fromarray(np.repeat(strip.reshape(1, w, 3), bot - top, axis=0)),
             (0, top))
    # crop() hands back a copy, so the lines have to be pasted back or they are
    # drawn on an image nobody keeps
    im.paste(tape(im.crop((0, top, w, bot)), max(2, int(h * 0.05)), 30), (0, top))

    # hours under it, small, so the strip reads as a clock and not as a ribbon
    hours = ImageFont.truetype(MONO, int(h * 0.072))
    track = h * 0.03
    per = w / days
    for k in range(int(days * 4) + 1):
        x = k * per / 4
        if x > w - h * 0.1:
            break
        label = "%02d" % ((k % 4) * 6)
        d.rectangle([x, bot, x + max(1, int(w * 0.0008)), bot + int(h * 0.045)],
                    fill=(70, 62, 80))
        tracked(d, (x + h * 0.03, bot + int(h * 0.075)), label, hours, MUTED, track)

    # the playhead: a line and a head, the only bright vertical thing here
    px = int(w * 0.615)
    d.rectangle([px, top - int(h * 0.06), px + max(2, int(w * 0.0016)), bot + int(h * 0.05)],
                fill=WARM)
    head = int(h * 0.055)
    d.polygon([(px - head, top - int(h * 0.06)), (px + head, top - int(h * 0.06)),
               (px, top + int(h * 0.02))], fill=WARM)

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
