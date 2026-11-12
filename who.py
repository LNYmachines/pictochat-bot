"""/who for the PictoChat bot: who is in the room, as a picture with one line per DS.

Per DS: a square in its favourite colour (from its member message), the nickname, how long it has been in the room and
the signal strength of its frames (bars and dBm, from the host's `state` event).

    python3 who.py preview OUTDIR      # render a sample picture as a PNG file
"""
import os
import sys
import time

import pics
from pics import BLACK, GREY, GREEN, RED, ORANGE

# the 16 favourite colours a DS user can pick in the settings (index in the member message -> RGB)
DS_FAVOURITE = [(97, 130, 154), (186, 73, 0), (251, 0, 24), (251, 138, 251), (251, 146, 0), (243, 227, 0),
                (170, 251, 0), (0, 251, 0), (0, 162, 56), (73, 219, 138), (48, 186, 243), (0, 89, 243), (0, 0, 146),
                (138, 0, 211), (211, 0, 235), (251, 0, 246)]
PER_PAGE = 4
BARS = [(-55, 4), (-65, 3), (-75, 2), (-85, 1)]      # smoothed dBm -> bars, as in /connection


def people(state, names, colours, since, now=None):
    """One dict per DS in the room, in AID order: name, colour (palette index), minutes, dbm, bars."""
    now = now or time.time()
    aids = (state or {}).get("aids") or {}
    sig = (state or {}).get("rssi") or {}
    out = []
    for mac in sorted(aids, key=aids.get):
        fav = colours.get(mac)
        dbm = sig[mac][1] if mac in sig else None
        out.append({"name": names.get(mac) or mac[-8:], "aid": aids[mac],
                    "colour": pics.nearest_colour(DS_FAVOURITE[fav]) if fav is not None and 0 <= fav < 16 else GREY,
                    "since": now - since[mac] if mac in since else None, "dbm": dbm,
                    "bars": next((b for lo, b in BARS if dbm is not None and dbm >= lo), 0)})
    return out


def pictures(infos, host_name=""):
    """Pictures with PER_PAGE people each."""
    pages = [infos[i:i + PER_PAGE] for i in range(0, len(infos), PER_PAGE)] or [[]]
    out = []
    f, bold, small = pics.font(10), pics.font(11, True), pics.font(9)
    for n, page in enumerate(pages):
        img, d = pics.canvas()
        pics.header(d, "In the room: %d DS" % len(infos), "%d/%d" % (n + 1, len(pages)) if len(pages) > 1 else "")
        y = pics.TOP + 2
        for p in page:
            x = pics.LEFT
            d.rounded_rectangle((x, y, x + 10, y + 10), 2, fill=p["colour"], outline=BLACK)
            x += 15
            d.text((x, y - 1), pics.fit(d, p["name"], bold, 82), fill=BLACK, font=bold)
            if p["since"] is not None:
                d.text((pics.LEFT + 102, y), "seit " + pics.ago(p["since"]), fill=BLACK, font=f)
            # signal: four rising bars, then the dBm value
            bx = pics.RIGHT - 60
            col = GREEN if p["bars"] >= 3 else ORANGE if p["bars"] == 2 else RED
            for i in range(4):
                h = 3 + 2 * i
                d.rectangle((bx + i * 5, y + 10 - h, bx + i * 5 + 3, y + 10), fill=col if i < p["bars"] else GREY)
            if p["dbm"] is not None:
                pics.right_text(d, pics.RIGHT, y + 1, "%d dBm" % round(p["dbm"]), BLACK, small)
            y += 15
        if not page:
            d.text((pics.LEFT + 2, pics.TOP + 8), "Nobody here yet.", fill=BLACK, font=bold)
            d.text((pics.LEFT + 2, pics.TOP + 26), "Open PictoChat on a DS and join this room.", fill=BLACK, font=f)
        if host_name and n == len(pages) - 1 and len(page) < PER_PAGE:
            d.text((pics.LEFT, pics.PH - 13), "Host and bot: %s" % host_name, fill=pics.BLUE, font=small)
        out.append(pics.rows(img))
    return out


def send_who(state, names, colours, since, host_name, send_picture):
    """Handle '/who'. Returns a short status string for the bot."""
    infos = people(state, names, colours, since)
    for rows in pictures(infos, host_name):
        send_picture(rows)
    if not infos:
        return "who: nobody is in the room; the picture says so. Just confirm in a few words."
    return ("who: %s. The picture with names, time in the room and signal is already in the room - do not repeat it, "
            "say at most one short line." % ", ".join(
                "%s (%s)" % (p["name"], "in for " + pics.ago(p["since"]) if p["since"] is not None else "time unknown")
                for p in infos))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        os.makedirs(sys.argv[2], exist_ok=True)
        sample = [dict(name="Lena", aid=1, colour=pics.nearest_colour(DS_FAVOURITE[10]), since=754, dbm=-58.2, bars=3),
                  dict(name="☆NiTRo☆", aid=2, colour=pics.nearest_colour(DS_FAVOURITE[2]), since=40, dbm=-81.0, bars=1),
                  dict(name="Tim", aid=3, colour=pics.nearest_colour(DS_FAVOURITE[6]), since=7300, dbm=-49.0, bars=4)]
        for i, r in enumerate(pictures(sample, "claude-o55")):
            pics.save_png(r, os.path.join(sys.argv[2], "who_%d.png" % (i + 1)))
        pics.save_png(pictures([], "claude-o55")[0], os.path.join(sys.argv[2], "who_empty.png"))
        print("wrote who_*.png to", sys.argv[2])
    else:
        print(__doc__)
