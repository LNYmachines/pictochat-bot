"""/connection for the PictoChat bot: how strong the link between a DS and the ESP32-C3 radio modem is.

Data (from the host's `state` event, every 5 s): per DS the signal strength of its frames (last and smoothed dBm,
seconds since it was heard), the poll / reply counters of the modem and the time since the last reply. One picture per
DS: signal bars, dBm with a rating, the share of polls the DS answered, and how long ago it was heard.

    python3 connection.py preview OUTDIR      # render sample pictures as PNG files
"""
import os
import sys
import time
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("pictochat_gui", os.path.join(HERE, "pictochat_gui.py"))
gui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gui)

STALE_REPLY_S = 20           # no reply for this long: the DS is not answering any more
STALE_SIGNAL_S = 30          # signal reading older than this is shown as old
RATE_WINDOW_S = 60           # the reply rate is measured over (up to) this much history
# (minimum dBm, bars, word, bar colour index)
RATINGS = [(-55, 4, "excellent", 10), (-65, 3, "good", 10), (-75, 2, "fair", 8), (-85, 1, "weak", 6),
           (-200, 0, "very weak", 5)]


def rate(dbm):
    """(bars 0-4, word, colour, percent 0-100) for a signal strength in dBm."""
    for lo, bars, word, col in RATINGS:
        if dbm >= lo:
            return bars, word, col, max(0, min(100, round((dbm + 90) * 2)))
    return 0, "very weak", 5, 0


def ago(seconds):
    s = int(seconds)
    if s < 90:
        return "%d s" % s
    if s < 5400:
        return "%d min" % round(s / 60)
    return "%d h %02d min" % (s // 3600, s % 3600 // 60)


def reply_rate(history, n_ds, cycle):
    """Share (0-1) of the expected replies that arrived, from (time, polls, replies) samples; None if unknown.
    Each poll that is addressed to the DSes (every `cycle`-th one) should get a reply from each of them."""
    if len(history) < 2 or n_ds < 1:
        return None
    t1, c1, r1 = history[-1]
    old = [h for h in history if t1 - h[0] <= RATE_WINDOW_S]
    t0, c0, r0 = old[0] if old else history[0]
    if t1 - t0 < 8 or c1 <= c0:
        return None
    expected = (c1 - c0) / max(1, cycle) * n_ds
    return max(0.0, min(1.0, (r1 - r0) / expected)) if expected > 0 else None


def report(state, history, names):
    """One dict per DS in the room: name, mac, dbm (last), avg, seen_ago, bars, word, colour, percent, rate,
    reply_age, silent. `state` is the host's latest state event (or None)."""
    if not state:
        return []
    aids = state.get("aids") or {}
    sig = state.get("rssi") or {}
    rr = reply_rate(history, len(aids), state.get("cycle") or 1)
    out = []
    for mac in sorted(aids, key=aids.get):
        last = sig.get(mac)
        info = {"mac": mac, "name": names.get(mac) or mac, "rate": rr, "reply_age": state.get("reply_age"),
                "silent": (state.get("reply_age") or 0) >= STALE_REPLY_S, "dbm": None, "avg": None, "seen_ago": None,
                "bars": 0, "word": "no signal reading", "colour": 2, "percent": None}
        if last:
            dbm, avg, seen = last[0], last[1], last[2]
            bars, word, col, pct = rate(avg)
            info.update(dbm=dbm, avg=avg, seen_ago=seen, bars=bars, word=word, colour=col, percent=pct)
        out.append(info)
    return out


def _font(px, bold=False):
    from PIL import ImageFont
    for name in (["DejaVuSans-Bold.ttf"] if bold else []) + ["DejaVuSans.ttf", "FreeSans.ttf"]:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def picture(info):
    """80 x 256 PictoChat picture for one DS (see report())."""
    from PIL import Image, ImageDraw
    PW, PH = gui.PW, gui.PH
    top = gui.TAB_Y1 + 1
    rows = gui.blank()
    silent = info["silent"]
    old = info["seen_ago"] is not None and info["seen_ago"] > STALE_SIGNAL_S
    col = 5 if silent else info["colour"]
    if silent or old or info["dbm"] is None:
        bar_on = 2                                            # grey: not a live reading
    else:
        bar_on = col

    # signal bars: four, rising; filled ones in the rating colour, the others in light grey
    x0, base, w, gap = 28, PH - 4, 11, 3
    for i in range(4):
        h = 12 + 13 * i
        colour = bar_on if i < info["bars"] else 2
        for y in range(base - h, base + 1):
            for x in range(x0 + i * (w + gap), x0 + i * (w + gap) + w):
                rows[y][x] = colour
    if silent:                                                # a cross over the bars: no answer
        for k in range(-18, 19):
            for t in (0, 1):
                x, y = x0 + 26 + k, base - 30 + k
                if 0 <= y < PH:
                    rows[y][x + t] = 5

    img = Image.new("L", (PW, PH), 0)
    d = ImageDraw.Draw(img)
    tx, tw = 92, 250 - 92
    lines = []                                                # (text, font size, bold, colour index)
    lines.append((info["name"], 12, True, 1))
    if info["dbm"] is None:
        lines.append(("no signal reading yet", 11, False, 1))
    else:
        lines.append(("%d dBm · %s%s" % (round(info["avg"]), info["word"], " (old)" if silent or old else ""), 11, True, col))
    rr = info["rate"]
    lines.append(("replies %s" % ("%d %% of polls" % round(rr * 100) if rr is not None else "n/a (measuring)"), 11, False, 1))
    if silent:
        lines.append(("no reply for %s" % ago(info["reply_age"]), 11, True, 5))
    elif info["seen_ago"] is not None:
        lines.append(("signal read %s ago" % ago(info["seen_ago"]), 11, False, 1))
    head = Image.new("L", (PW, top), 0)
    hd = ImageDraw.Draw(head)
    title = "DS ↔ ESP32-C3"
    hd.text((gui.TAB_X1 + 4, 1), title, fill=255, font=_font(11, True))
    hp = head.load()
    for y in range(top):
        for x in range(PW):
            if hp[x, y] > 110:
                rows[y][x] = 1
    for x in range(gui.TAB_X1 + 1, gui.VIS_X1 + 1):
        rows[top - 1][x] = 2
    y = top + 3
    for text, px, bold, colour in lines:
        f = _font(px, bold)
        t = text
        while d.textlength(t, font=f) > tw and len(t) > 1:
            t = t[:-2] + "…" if not t.endswith("…") else t[:-2] + "…"
        layer = Image.new("L", (PW, PH), 0)
        ImageDraw.Draw(layer).text((tx, y), t, fill=255, font=f)
        lp = layer.load()
        for yy in range(y, min(PH, y + px + 4)):
            for xx in range(tx, tx + tw + 2):
                if xx < PW and lp[xx, yy] > 110:
                    rows[yy][xx] = colour
        y += 13 if px == 11 else 15
    return gui.clip(rows)


def summary(infos):
    parts = []
    for i in infos:
        if i["dbm"] is None:
            sig = "no signal reading"
        else:
            sig = "%d dBm (%s, last %d dBm, read %s ago)" % (round(i["avg"]), i["word"], i["dbm"], ago(i["seen_ago"]))
        rr = "%d%% of polls answered" % round(i["rate"] * 100) if i["rate"] is not None else "reply rate n/a"
        silent = ", NO REPLY for %s" % ago(i["reply_age"]) if i["silent"] else ""
        parts.append("%s: %s, %s%s" % (i["name"], sig, rr, silent))
    return "; ".join(parts)


def send_connection(state, history, names, send_text, send_picture):
    """Handle '/connection': one picture per DS in the room. Returns a short status string."""
    infos = report(state, history, names)
    if not infos:
        send_text("[italic normal] No DS in the room right now.")
        return "connection: no DS in the room. Said so in the room already; just confirm in a few words."
    for i in infos:
        send_picture(picture(i))
    return "connection: " + summary(infos) + ". The pictures are already in the room - do not repeat the numbers."


# ---------------------------------------------------------------- preview
if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        sys.path.insert(0, HERE)
        import mensa
        out = sys.argv[2]
        os.makedirs(out, exist_ok=True)
        samples = {
            "good": dict(mac="a", name="Lena", dbm=-58, avg=-60.2, seen_ago=2.0, bars=3, word="good", colour=10,
                         percent=60, rate=0.91, reply_age=0.4, silent=False),
            "weak": dict(mac="b", name="Tim", dbm=-83, avg=-81.5, seen_ago=5.0, bars=1, word="weak", colour=6,
                         percent=17, rate=0.52, reply_age=1.0, silent=False),
            "silent": dict(mac="c", name="DS of Anna", dbm=-63, avg=-63.0, seen_ago=4800, bars=3, word="good",
                           colour=10, percent=54, rate=0.0, reply_age=4800, silent=True),
            "noreading": dict(mac="d", name="00:1b:7a:12:34:56", dbm=None, avg=None, seen_ago=None, bars=0,
                              word="no signal reading", colour=2, percent=None, rate=None, reply_age=0, silent=False),
        }
        for k, v in samples.items():
            mensa._png(picture(v), os.path.join(out, "conn_%s.png" % k))
            print(k, "->", summary([v]))
    else:
        print(__doc__)
