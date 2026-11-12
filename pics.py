"""Shared drawing helpers for the bot's picture commands (/who, /wetter, /bus, /stats).

A PictoChat picture is 256 x 80 pixels in 16 colours. The DS shows columns 23-252, and the sender's name tab covers
columns 0-80 of rows 0-15. Pictures are drawn with Pillow straight in palette indices (no anti-aliasing), so what is
drawn here is exactly what the DS shows.
"""
import os
import sys
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))


def _find_host_file(name):
    """pictochat_gui.py comes from the PictoChat host repo: next to this file, in $PICTOCHAT_HOST, or ../pictochat_host."""
    for d in (HERE, os.environ.get("PICTOCHAT_HOST", ""), os.path.join(HERE, "..", "pictochat_host")):
        if d and os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    sys.exit(f"{name} not found: put the PictoChat host repo next to this one (../pictochat_host) or set PICTOCHAT_HOST")


_spec = importlib.util.spec_from_file_location("pictochat_gui", _find_host_file("pictochat_gui.py"))
gui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gui)

PW, PH = gui.PW, gui.PH
LEFT, RIGHT = gui.VIS_X0 + 2, gui.VIS_X1 - 1         # 25 .. 251: the columns used for content
TOP = gui.TAB_Y1 + 1                                 # 16: the first row below the name tab
HEAD_X = gui.TAB_X1 + 4                              # header text starts right of the name tab
PAPER, BLACK, GREY, MAGENTA, PINK, RED, ORANGE, AMBER, YELLOW, LIME, GREEN, TEAL, SKY, BLUE, NAVY, VIOLET = range(16)
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def font(px, bold=False):
    from PIL import ImageFont
    for name in (["DejaVuSans-Bold.ttf"] if bold else []) + ["DejaVuSans.ttf", "FreeSans.ttf"]:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def canvas():
    """(image, draw) for one picture; fill values are palette indices."""
    from PIL import Image, ImageDraw
    img = Image.new("P", (PW, PH), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"
    return img, d


def rows(img):
    """The finished picture as 80 rows of 256 palette indices, with the invisible parts blanked."""
    px = img.load()
    return gui.clip([[px[x, y] & 15 for x in range(PW)] for y in range(PH)])


def header(d, title, right_text=""):
    """Title in the strip right of the name tab, optional text at the right edge, and a thin rule below."""
    f = font(11, True)
    rw = int(d.textlength(right_text, font=font(10, True))) if right_text else 0
    d.text((HEAD_X, 1), fit(d, title, f, RIGHT - HEAD_X - rw - (4 if rw else 0)), fill=BLACK, font=f)
    if right_text:
        d.text((RIGHT - rw, 2), right_text, fill=BLACK, font=font(10, True))
    d.line((gui.TAB_X1 + 1, TOP - 1, gui.VIS_X1, TOP - 1), fill=GREY)


def chip(d, x, y, text, bg, fg, f, min_w=0, h=11):
    """A filled, rounded label. Returns the x just right of it."""
    w = max(min_w, int(d.textlength(text, font=f)) + 7)
    d.rounded_rectangle((x, y, x + w - 1, y + h - 1), 2, fill=bg)
    d.text((x + (w - d.textlength(text, font=f)) / 2, y + (h - f.size) // 2 - 1), text, fill=fg, font=f)
    return x + w


def fit(d, text, f, width):
    """`text`, shortened with an ellipsis so that it is at most `width` pixels wide."""
    if d.textlength(text, font=f) <= width:
        return text
    while len(text) > 1 and d.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text.rstrip(" ,.-&") + "…"


def right_text(d, x_right, y, text, fill, f):
    d.text((x_right - d.textlength(text, font=f), y), text, fill=fill, font=f)


def nearest_colour(rgb, exclude=(PAPER,)):
    """The palette index closest to an RGB colour."""
    return min((i for i in gui.PALETTE if i not in exclude),
               key=lambda i: sum((a - b) ** 2 for a, b in zip(gui.PALETTE[i], rgb)))


def ago(seconds):
    s = int(seconds)
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min" % (s // 60)
    return "%d h %02d" % (s // 3600, s % 3600 // 60) if s < 86400 else "%d d" % (s // 86400)


def save_png(rows_, path, scale=3):
    from PIL import Image
    img = Image.new("RGB", (PW, PH))
    px = img.load()
    for y, r in enumerate(rows_):
        for x, v in enumerate(r):
            px[x, y] = gui.PALETTE[v]
    img.resize((PW * scale, PH * scale), Image.NEAREST).save(path)
