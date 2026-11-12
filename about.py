"""/about for the PictoChat bot: a title picture for the room, with the versions of the host and the bot.

The picture is drawn here, pixel by pixel, in the DS's 16-colour palette: a handheld with two screens on the left,
radio waves in rainbow colours, and the title with the versions on the right.

    python3 about.py preview OUTDIR      # render the picture as a PNG file
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

RAINBOW = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 3, 4]      # palette indices, red round to pink
BLACK, GREY = 1, 2
URL = "github.com/LNYmachines"


def _font(px, bold=False):
    from PIL import ImageFont
    for name in (["DejaVuSans-Bold.ttf"] if bold else []) + ["DejaVuSans.ttf", "FreeSans.ttf"]:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _uptime(seconds):
    s = int(seconds)
    if s >= 86400:
        return "%d d %d h" % (s // 86400, s % 86400 // 3600)
    return "%d h %02d min" % (s // 3600, s % 3600 // 60) if s >= 3600 else "%d min" % (s // 60)


def lines(state, bot_version, bot_commit):
    """The text lines of the picture: (text, colour index)."""
    st = state or {}
    if st.get("version"):
        host = "host %s%s" % (st["version"], " · " + st["commit"] if st.get("commit") else "")
        kind = {"python": "Python", "rust": "Rust"}.get(st.get("impl"), "")
        run = " · ".join(p for p in (kind, "up " + _uptime(st["uptime"]) if st.get("uptime") is not None else "") if p)
    else:
        host, run = "host: version unknown", ""
    bot = "bot %s%s" % (bot_version, " · " + bot_commit if bot_commit else "")
    return [(host, BLACK), (bot, BLACK), (run, 13), (URL, 14)]


def picture(state=None, bot_version="", bot_commit=""):
    """80 x 256 PictoChat picture (rows of palette indices)."""
    from PIL import Image, ImageDraw
    img = Image.new("P", (gui.PW, gui.PH), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"                                    # no anti-aliasing: every pixel is a palette index

    # --- handheld with two screens, open, seen from the front (below the name tab)
    x0, x1 = 28, 76
    d.rounded_rectangle((x0, 19, x1, 46), 3, fill=GREY, outline=BLACK)          # lid
    d.rectangle((x0 + 8, 23, x1 - 8, 42), fill=0, outline=BLACK)                # upper screen
    d.rectangle((x0 + 3, 47, x1 - 3, 48), fill=BLACK)                           # hinge
    d.rounded_rectangle((x0, 49, x1, 75), 3, fill=GREY, outline=BLACK)          # base
    d.rectangle((x0 + 13, 53, x1 - 13, 71), fill=0, outline=BLACK)              # lower screen
    d.rectangle((x0 + 4, 60, x0 + 10, 62), fill=BLACK)                          # d-pad
    d.rectangle((x0 + 6, 58, x0 + 8, 64), fill=BLACK)
    for bx, by in ((x1 - 9, 58), (x1 - 5, 62), (x1 - 9, 66)):                   # buttons
        d.ellipse((bx, by, bx + 2, by + 2), fill=BLACK)
    # upper screen: three chat lines; lower screen: a scribble in rainbow colours
    for i, (w, c) in enumerate(((22, 13), (15, 5), (19, 10))):
        d.line((x0 + 11, 27 + 5 * i, x0 + 11 + w, 27 + 5 * i), fill=c)
    pts = [(x0 + 16, 67), (x0 + 19, 58), (x0 + 22, 66), (x0 + 25, 57), (x0 + 28, 66), (x0 + 31, 58), (x0 + 33, 65)]
    for i in range(len(pts) - 1):
        d.line((pts[i], pts[i + 1]), fill=RAINBOW[(i * 2) % len(RAINBOW)], width=1)

    # --- radio waves leaving the handheld
    for i, r in enumerate((7, 13, 19, 25)):
        d.arc((x1 + 3 - r, 47 - r, x1 + 3 + r, 47 + r), -42, 42, fill=RAINBOW[i * 3], width=2)

    # --- title, rainbow rule, text lines
    tx = 112
    d.text((tx, 0), "PictoChat", fill=BLACK, font=_font(15, True))
    seg = (gui.VIS_X1 - 1 - tx) / len(RAINBOW)
    for i, c in enumerate(RAINBOW):
        d.rectangle((tx + round(i * seg), 19, tx + round((i + 1) * seg) - 1, 20), fill=c)
    y = 23
    for text, colour in lines(state, bot_version, bot_commit):
        if not text:
            continue
        f = _font(10)
        while d.textlength(text, font=f) > gui.VIS_X1 - 2 - tx and len(text) > 1:
            text = text[:-2] + "…"
        d.text((tx, y), text, fill=colour, font=f)
        y += 13

    # --- rainbow along the bottom edge
    seg = (gui.VIS_X1 - gui.VIS_X0 + 1) / (len(RAINBOW) * 2)
    for i in range(len(RAINBOW) * 2):
        d.rectangle((gui.VIS_X0 + round(i * seg), 78, gui.VIS_X0 + round((i + 1) * seg) - 1, 79),
                    fill=RAINBOW[i % len(RAINBOW)])

    px = img.load()
    return gui.clip([[px[x, y] & 15 for x in range(gui.PW)] for y in range(gui.PH)])


def send_about(state, bot_version, bot_commit, send_text, send_picture):
    """Handle '/about': the title picture. Returns a short status string for the bot."""
    send_picture(picture(state, bot_version, bot_commit))
    info = "; ".join(t for t, _ in lines(state, bot_version, bot_commit) if t)
    return ("about: the title picture with the versions is already in the room (%s). Add at most one short, friendly "
            "line: this room is hosted by a small computer with a radio modem instead of a second DS." % info)


# ---------------------------------------------------------------- preview
if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        sys.path.insert(0, HERE)
        import mensa
        os.makedirs(sys.argv[2], exist_ok=True)
        sample = {"version": "0.2.0", "commit": "e36052a", "impl": "python", "uptime": 11580}
        mensa._png(picture(sample, "0.2.0", "06da9ad"), os.path.join(sys.argv[2], "about.png"))
        mensa._png(picture(None, "0.2.0", ""), os.path.join(sys.argv[2], "about_nohost.png"))
        print("wrote about.png and about_nohost.png to", sys.argv[2])
    else:
        print(__doc__)
