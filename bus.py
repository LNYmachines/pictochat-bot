"""/bus for the PictoChat bot: the next departures of one bus line at one stop, as a picture.

Default: line 115 from Kaiserslautern Hbf towards the university (it stops at Uni West on the way to Uni Zentral).
One line per departure: the time, minutes from now, the delay if the timetable service reports one, and the
platform. Data: the departure monitor of the VRN timetable service (vrn.de), credited in the picture.
Other stops or lines: environment variables BUS_STOP (stop id), BUS_STOP_NAME, BUS_LINE, BUS_DIRECTION (a word that
must occur in the direction) and BUS_TO (the destination shown in the header).

    python3 bus.py preview OUTDIR      # download the departures and render the picture as a PNG file
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

import pics
from pics import BLACK, GREY, GREEN, RED, BLUE, ORANGE

STOP = os.environ.get("BUS_STOP", "6009035")                    # Kaiserslautern, Hbf
STOP_NAME = os.environ.get("BUS_STOP_NAME", "Hbf")
LINE = os.environ.get("BUS_LINE", "115")
DIRECTION = os.environ.get("BUS_DIRECTION", "Uni")              # 115 runs Hbf - Uni West - Uni Zentral
TO = os.environ.get("BUS_TO", "Uni West")
API = ("https://www.vrn.de/mngvrn/XML_DM_REQUEST?outputFormat=JSON&type_dm=stop&name_dm=%s&mode=direct"
       "&useRealtime=1&limit=80&depType=stopEvents")
TTL = 45
ROWS = 4
_cache = {}


class BusError(Exception):
    pass


def _dt(d):
    return datetime.datetime(int(d["year"]), int(d["month"]), int(d["day"]), int(d["hour"]), int(d["minute"]))


def fetch(now=None):
    """The next departures of LINE towards DIRECTION at STOP: a list of dicts (planned, real, delay in minutes or
    None when there is no live data, platform, direction), soonest first."""
    t = time.time()
    if "t" in _cache and t - _cache["t"] < TTL:
        return _cache["deps"]
    deps, start = [], None
    for _ in range(4):          # at night the first 80 departures of a big stop do not reach the first bus: read on
        deps, last = _page(start)
        if len(deps) >= ROWS or last is None or last == start:
            break
        if deps:
            more, _last = _page(last)
            deps += [p for p in more if p["planned"] not in {q["planned"] for q in deps}]
            break
        start = last
    deps.sort(key=lambda p: p["real"])
    _cache.update(t=t, deps=deps)
    return deps


def _page(start=None):
    """(matching departures, time of the last departure listed) from `start` (a datetime) or from now."""
    url = API % STOP + (start.strftime("&itdDate=%Y%m%d&itdTime=%H%M") if start else "")
    req = urllib.request.Request(url, headers={"User-Agent": "pictochat-bot (hobby project)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read(2000000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise BusError("vrn.de is not reachable (%s)" % e) from None
    deps, last = [], None
    for x in data.get("departureList") or []:
        try:
            last = _dt(x["dateTime"])
        except (KeyError, ValueError, TypeError):
            pass
        sl = x.get("servingLine") or {}
        number = str(sl.get("number", ""))
        if LINE not in (number, number.split()[-1] if number else "", str(sl.get("symbol", ""))):
            continue
        if DIRECTION.lower() not in str(sl.get("direction", "")).lower():
            continue
        try:
            planned = _dt(x["dateTime"])
            real = _dt(x["realDateTime"]) if x.get("realDateTime") else None
        except (KeyError, ValueError, TypeError):
            continue
        deps.append({"planned": planned, "real": real or planned,
                     "delay": round((real - planned).total_seconds() / 60) if real else None,
                     "platform": str(x.get("platformName") or x.get("platform") or ""),
                     "direction": str(sl.get("direction", "")).replace("Kaiserslautern, ", "")})
    return deps, last


def picture(deps, now=None):
    now = now or datetime.datetime.now()
    img, d = pics.canvas()
    pics.header(d, "%s: %s > %s" % (LINE, STOP_NAME, TO), now.strftime("%H:%M"))
    big, f, small = pics.font(12, True), pics.font(10), pics.font(9)
    y = pics.TOP + 2
    shown = [p for p in deps if (p["real"] - now).total_seconds() > -60][:ROWS]
    for p in shown:
        mins = max(0, round((p["real"] - now).total_seconds() / 60))
        d.text((pics.LEFT, y - 1), p["planned"].strftime("%H:%M"), fill=BLACK, font=big)
        when = "jetzt" if mins == 0 else "in %d min" % mins if mins < 100 else "in %d h %02d" % (mins // 60, mins % 60)
        d.text((pics.LEFT + 46, y), when, fill=BLUE if mins <= 5 else BLACK, font=pics.font(10, mins <= 5))
        x = pics.LEFT + 108
        if p["delay"] is None:
            d.text((x, y + 1), "Fahrplan", fill=GREY, font=small)
        elif p["delay"] <= 0:
            pics.chip(d, x, y, "pünktlich", GREEN, BLACK, pics.font(9, True))
        else:
            pics.chip(d, x, y, "+%d min" % p["delay"], RED if p["delay"] >= 5 else ORANGE, 0, pics.font(9, True))
        if p["platform"]:
            pics.right_text(d, pics.RIGHT, y, "Steig %s" % p["platform"], BLACK, f)
        y += 15
    if not shown:
        d.text((pics.LEFT + 2, pics.TOP + 8), "Keine Abfahrt in Sicht.", fill=BLACK, font=pics.font(11, True))
        d.text((pics.LEFT + 2, pics.TOP + 26), "Linie %s fährt gerade nicht ab %s." % (LINE, STOP_NAME), fill=BLACK, font=f)
    if len(shown) < ROWS:
        d.text((pics.LEFT, pics.PH - 12), "Quelle: vrn.de", fill=BLUE, font=small)
    else:
        pass                                            # four rows fill the picture: the credit goes in the text
    return pics.rows(img)


def send_bus(send_text, send_picture):
    """Handle '/bus'. Returns a short status string for the bot (errors do not raise)."""
    try:
        deps = fetch()
    except BusError as e:
        return "bus: %s" % e
    now = datetime.datetime.now()
    send_picture(picture(deps, now))
    coming = [p for p in deps if (p["real"] - now).total_seconds() > -60][:ROWS]
    if len(coming) >= ROWS:
        send_text("[mono small] Quelle: vrn.de")
    if not coming:
        return "bus: no departure of line %s from %s in sight; the picture says so. Confirm in a few words." % (LINE, STOP_NAME)
    return ("bus: the next departures of line %s from %s towards %s are already in the room (next: %s). Do not "
            "repeat the times, say at most one short line." % (
                LINE, STOP_NAME, TO, ", ".join(p["planned"].strftime("%H:%M") for p in coming[:2])))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        os.makedirs(sys.argv[2], exist_ok=True)
        deps = fetch()
        for p in deps[:6]:
            print(p["planned"].strftime("%a %H:%M"), p["delay"], p["platform"], p["direction"])
        pics.save_png(picture(deps), os.path.join(sys.argv[2], "bus.png"))
        base = datetime.datetime(2026, 11, 13, 7, 30)
        sample = [dict(planned=base + datetime.timedelta(minutes=m), real=base + datetime.timedelta(minutes=m + (dl or 0)),
                       delay=dl, platform="A", direction="Uni Zentral") for m, dl in ((3, 0), (18, 2), (33, 7), (48, None))]
        pics.save_png(picture(sample, base), os.path.join(sys.argv[2], "bus_sample.png"))
        pics.save_png(picture([], base), os.path.join(sys.argv[2], "bus_none.png"))
    else:
        print(__doc__)
