"""/wetter for the PictoChat bot: the weather as one picture.

Left: the weather now (icon, temperature, wind). Right: four columns for the coming hours (time, icon, temperature,
chance of rain). Data: Open-Meteo (https://open-meteo.com, no key needed, CC BY 4.0: the picture credits it).
The place is set below or with the environment variables WETTER_LAT, WETTER_LON and WETTER_NAME.

    python3 wetter.py preview OUTDIR      # download the forecast and render the picture as a PNG file
"""
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request

import pics
from pics import BLACK, GREY, AMBER, YELLOW, BLUE, SKY, PAPER

LAT = float(os.environ.get("WETTER_LAT", "49.44"))
LON = float(os.environ.get("WETTER_LON", "7.77"))
NAME = os.environ.get("WETTER_NAME", "KL")                # short: the header has room for about 12 letters
API = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code,"
       "wind_speed_10m,is_day&hourly=temperature_2m,precipitation_probability,weather_code,is_day&forecast_days=2"
       "&timezone=auto")
TTL = 600
STEP_H = 3                      # hours between the forecast columns
_cache = {}


class WetterError(Exception):
    pass


def fetch():
    """The forecast (parsed JSON), cached for TTL seconds."""
    now = time.time()
    if "t" in _cache and now - _cache["t"] < TTL:
        return _cache["data"]
    req = urllib.request.Request(API % (LAT, LON), headers={"User-Agent": "pictochat-bot (hobby project)"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read(400000).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise WetterError("open-meteo.com is not reachable (%s)" % e) from None
    if "current" not in data or "hourly" not in data:
        raise WetterError("open-meteo.com sent an unexpected answer")
    _cache.update(t=now, data=data)
    return data


def kind(code):
    """WMO weather code -> (icon name, German word)."""
    if code == 0:
        return "sun", "klar"
    if code in (1, 2):
        return "partly", "heiter" if code == 1 else "wolkig"
    if code == 3:
        return "cloud", "bedeckt"
    if code in (45, 48):
        return "fog", "Nebel"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow", "Schnee"
    if code in (95, 96, 99):
        return "thunder", "Gewitter"
    if code in (51, 53, 55, 56, 57):
        return "rain", "Niesel"
    if code in (61, 63, 65, 66, 67, 80, 81, 82):
        return "rain", "Regen" if code < 80 else "Schauer"
    return "cloud", "?"


def _cloud(d, x, y, s):
    """A cloud, s = scale (1 small, 2 large); (x, y) is the top-left of its box."""
    for dx, dy, r in ((4, 5, 4), (9, 2, 5), (15, 5, 4)):
        d.ellipse(((x + dx - r) * 1 + 0, y + dy - r + 3, x + dx + r, y + dy + r + 3), fill=GREY, outline=BLACK) if s == 1 \
            else d.ellipse((x + 2 * (dx - r), y + 2 * (dy - r) + 6, x + 2 * (dx + r), y + 2 * (dy + r) + 6),
                           fill=GREY, outline=BLACK)
    if s == 1:
        d.rectangle((x + 3, y + 7, x + 16, y + 11), fill=GREY)
        d.line((x + 2, y + 12, x + 17, y + 12), fill=BLACK)
    else:
        d.rectangle((x + 6, y + 14, x + 32, y + 23), fill=GREY)
        d.line((x + 4, y + 24, x + 34, y + 24), fill=BLACK)


def _sun(d, cx, cy, r, night=False):
    if night:                                   # a crescent moon
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=AMBER, outline=BLACK)
        d.ellipse((cx - r + r // 2 + 1, cy - r - 1, cx + r + r // 2 + 1, cy + r - 1), fill=PAPER)
        return
    for ax, ay in ((1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, 0.7), (0.7, -0.7), (-0.7, -0.7)):
        d.line((cx + ax * (r + 2), cy + ay * (r + 2), cx + ax * (r + 2 + r // 2), cy + ay * (r + 2 + r // 2)),
               fill=AMBER, width=1 if r < 6 else 2)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=YELLOW, outline=AMBER)


def icon(d, name, x, y, s=1, night=False):
    """Draw a weather icon in a box of 20 x 18 pixels (s=1) or 40 x 36 (s=2) at (x, y)."""
    w = 20 * s
    if name == "sun":
        _sun(d, x + w // 2, y + 9 * s, 4 * s + (1 if s == 1 else 0), night)
        return
    if name == "partly":
        _sun(d, x + 14 * s, y + 5 * s, 3 * s + 1, night)
    if name == "fog":
        for i in range(4):
            d.line((x + 2 * s + (i % 2) * 2, y + (4 + 3 * i) * s, x + w - 3 * s + (i % 2) * 2, y + (4 + 3 * i) * s),
                   fill=GREY if i % 2 else BLACK, width=s)
        return
    _cloud(d, x, y + (1 if name == "partly" else 0) * s, s)
    by = y + 14 * s
    if name == "rain":
        for i in range(4):
            d.line((x + (4 + 4 * i) * s, by, x + (2 + 4 * i) * s, by + 4 * s), fill=BLUE, width=s)
    elif name == "snow":
        for i in range(4):
            cx, cy = x + (4 + 4 * i) * s, by + (1 + (i % 2) * 2) * s
            d.rectangle((cx - s // 2, cy - s // 2, cx + s // 2 + (s - 1), cy + s // 2 + (s - 1)), fill=SKY)
    elif name == "thunder":
        d.line((x + 11 * s, by - 1, x + 8 * s, by + 2 * s, x + 11 * s, by + 2 * s, x + 8 * s, by + 5 * s),
               fill=AMBER, width=s + 1)


def picture(data, now=None):
    """The weather picture from an Open-Meteo answer."""
    now = now or datetime.datetime.now()
    cur, hourly = data["current"], data["hourly"]
    img, d = pics.canvas()
    pics.header(d, "Wetter %s" % NAME, "%s %d.%d." % (pics.WEEKDAYS[now.weekday()], now.day, now.month))

    # now: large icon, temperature, word, wind
    name, word = kind(int(cur.get("weather_code", 3)))
    icon(d, name, pics.LEFT + 1, pics.TOP + 4, 2, night=not cur.get("is_day", 1))
    temp = "%d°" % round(cur["temperature_2m"])
    d.text((pics.LEFT + 44, pics.TOP + 2), temp, fill=BLACK, font=pics.font(20, True))
    d.text((pics.LEFT + 45, pics.TOP + 27), word, fill=BLACK, font=pics.font(9))
    d.text((pics.LEFT + 1, pics.PH - 22), "Wind %d km/h" % round(cur.get("wind_speed_10m", 0)), fill=BLACK,
           font=pics.font(9))
    d.text((pics.LEFT + 1, pics.PH - 11), "open-meteo.com", fill=BLUE, font=pics.font(9))

    # the coming hours: every STEP_H hours, four columns
    times = hourly["time"]
    start = now.strftime("%Y-%m-%dT%H:00")
    first = next((i for i, t in enumerate(times) if t > start), 0)
    x0, col_w = pics.LEFT + 92, 34
    d.line((x0 - 5, pics.TOP + 3, x0 - 5, pics.PH - 3), fill=GREY)
    for k in range(4):
        i = first + STEP_H - 1 + k * STEP_H
        if i >= len(times):
            break
        x = x0 + k * col_w
        hh = times[i][11:13]
        f = pics.font(10, True)
        d.text((x + (col_w - 4 - d.textlength(hh + " h", font=f)) / 2, pics.TOP + 2), hh + " h", fill=BLACK, font=f)
        nm, _ = kind(int(hourly["weather_code"][i]))
        icon(d, nm, x + 4, pics.TOP + 15, 1, night=not hourly.get("is_day", [1] * len(times))[i])
        t = "%d°" % round(hourly["temperature_2m"][i])
        d.text((x + (col_w - 4 - d.textlength(t, font=f)) / 2, pics.TOP + 36), t, fill=BLACK, font=f)
        pp = hourly.get("precipitation_probability", [None] * len(times))[i]
        if pp is not None:
            s = "%d%%" % pp
            f9 = pics.font(9, pp >= 50)
            d.text((x + (col_w - 4 - d.textlength(s, font=f9)) / 2, pics.TOP + 50), s, fill=BLUE if pp >= 30 else BLACK,
                   font=f9)
    return pics.rows(img)


def summary(data):
    cur = data["current"]
    return "%d °C, %s, wind %d km/h" % (round(cur["temperature_2m"]), kind(int(cur.get("weather_code", 3)))[1],
                                        round(cur.get("wind_speed_10m", 0)))


def send_wetter(send_picture):
    """Handle '/wetter'. Returns a short status string for the bot (errors do not raise)."""
    try:
        data = fetch()
    except WetterError as e:
        return "wetter: %s" % e
    send_picture(picture(data))
    return ("wetter: the weather picture for %s is already in the room (now %s; the columns show the coming hours "
            "with the chance of rain). Do not repeat the numbers, say at most one short line." % (NAME, summary(data)))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        os.makedirs(sys.argv[2], exist_ok=True)
        data = fetch()
        pics.save_png(picture(data), os.path.join(sys.argv[2], "wetter.png"))
        print(summary(data))
        # every icon once, to check the drawings
        img, d = pics.canvas()
        for i, nm in enumerate(("sun", "partly", "cloud", "rain", "snow", "thunder", "fog")):
            icon(d, nm, pics.LEFT + i * 30, pics.TOP + 2, 1)
        for i, nm in enumerate(("sun", "partly", "rain", "thunder", "snow")):
            icon(d, nm, pics.LEFT + i * 44, pics.TOP + 24, 2, night=i == 0)
        pics.save_png(pics.rows(img), os.path.join(sys.argv[2], "wetter_icons.png"))
    else:
        print(__doc__)
