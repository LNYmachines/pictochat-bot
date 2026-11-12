"""/stats for the PictoChat bot: how busy the room has been, as a picture.

The bot counts every drawing a DS sends: per day, per hour of the day and per nickname (stats.json next to the bot,
written after each message). The picture shows the last seven days as bars, the busiest hour, the people who drew the
most and the total. Counting starts when this file is first created; there is no history before that.

    python3 stats.py preview OUTDIR      # render a sample picture as a PNG file
"""
import datetime
import json
import os
import sys
import threading

import pics
from pics import BLACK, GREY, BLUE, SKY, ORANGE

FILE = os.path.join(pics.HERE, "stats.json")
KEEP_DAYS = 120


class Stats:
    def __init__(self, path=FILE):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"since": datetime.date.today().isoformat(), "days": {}, "hours": [0] * 24, "names": {},
                     "total": 0}
        try:
            with open(path) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and isinstance(loaded.get("hours"), list) and len(loaded["hours"]) == 24:
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def add(self, name, when=None):
        """Count one drawing from `name`."""
        when = when or datetime.datetime.now()
        with self.lock:
            d = self.data
            day = when.date().isoformat()
            d["days"][day] = d["days"].get(day, 0) + 1
            d["hours"][when.hour] += 1
            d["names"][name or "?"] = d["names"].get(name or "?", 0) + 1
            d["total"] = d.get("total", 0) + 1
            for old in sorted(d["days"])[:-KEEP_DAYS]:
                del d["days"][old]
            try:
                with open(self.path + ".tmp", "w") as fh:
                    json.dump(d, fh)
                os.replace(self.path + ".tmp", self.path)
            except OSError:
                pass

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))


def picture(data, today=None):
    today = today or datetime.date.today()
    img, d = pics.canvas()
    pics.header(d, "Room stats", "%d drawings" % data.get("total", 0))
    f, bold, small = pics.font(10), pics.font(10, True), pics.font(9)

    # last seven days as bars
    days = [today - datetime.timedelta(days=i) for i in range(6, -1, -1)]
    counts = [data["days"].get(x.isoformat(), 0) for x in days]
    peak = max(counts) or 1
    x0, base, bar_w, gap, max_h = pics.LEFT + 2, pics.PH - 13, 11, 4, 36
    for i, (day, n) in enumerate(zip(days, counts)):
        x = x0 + i * (bar_w + gap)
        h = round(n / peak * max_h)
        colour = BLUE if day == today else SKY
        if n:
            d.rectangle((x, base - max(h, 2), x + bar_w - 1, base), fill=colour)
            s = str(n)
            d.text((x + (bar_w - d.textlength(s, font=small)) / 2, base - max(h, 2) - 10), s, fill=BLACK, font=small)
        else:
            d.line((x, base, x + bar_w - 1, base), fill=GREY)
        wd = pics.WEEKDAYS[day.weekday()]
        d.text((x + (bar_w - d.textlength(wd, font=small)) / 2, base + 2), wd, fill=BLACK, font=small)

    # right: busiest hour, top drawers, since
    tx = pics.LEFT + 118
    d.line((tx - 6, pics.TOP + 3, tx - 6, pics.PH - 3), fill=GREY)
    hours = data.get("hours") or [0] * 24
    y = pics.TOP + 2
    if sum(hours):
        h = max(range(24), key=lambda i: hours[i])
        d.text((tx, y), "busiest: %02d-%02d h" % (h, (h + 1) % 24), fill=BLACK, font=bold)
    else:
        d.text((tx, y), "no drawings yet", fill=BLACK, font=bold)
    y += 14
    top = sorted((data.get("names") or {}).items(), key=lambda kv: -kv[1])[:3]
    for rank, (name, n) in enumerate(top):
        d.text((tx, y), "%d." % (rank + 1), fill=ORANGE, font=bold)
        d.text((tx + 13, y), pics.fit(d, name, f, pics.RIGHT - tx - 13 - 26), fill=BLACK, font=f)
        pics.right_text(d, pics.RIGHT, y, str(n), BLACK, bold)
        y += 12
    try:
        since = datetime.date.fromisoformat(data.get("since", ""))
        d.text((tx, pics.PH - 12), "since %d.%d.%d" % (since.day, since.month, since.year), fill=BLUE, font=small)
    except ValueError:
        pass
    return pics.rows(img)


def send_stats(stats, send_picture):
    """Handle '/stats'. Returns a short status string for the bot."""
    data = stats.snapshot()
    send_picture(picture(data))
    today = data["days"].get(datetime.date.today().isoformat(), 0)
    return ("stats: the picture is already in the room (%d drawings counted since %s, %d today). Do not repeat the "
            "numbers, say at most one short line." % (data.get("total", 0), data.get("since", "?"), today))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        os.makedirs(sys.argv[2], exist_ok=True)
        t = datetime.date(2026, 11, 13)
        sample = {"since": "2026-11-13", "total": 87, "hours": [0] * 12 + [9, 14, 3, 0, 5, 11, 21, 12, 8, 4, 0, 0],
                  "names": {"Lena": 41, "☆NiTRo☆": 30, "Tim": 12, "x": 4},
                  "days": {(t - datetime.timedelta(days=i)).isoformat(): n for i, n in enumerate((12, 30, 0, 7, 21, 3, 14))}}
        pics.save_png(picture(sample, t), os.path.join(sys.argv[2], "stats.png"))
        pics.save_png(picture(Stats("/nonexistent/x").snapshot(), t), os.path.join(sys.argv[2], "stats_empty.png"))
        print("wrote stats.png and stats_empty.png")
    else:
        print(__doc__)
