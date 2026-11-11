"""mensa-kl.de menu for the PictoChat bot: /mensa sends one picture per meal (Ausgabe + price in a coloured header,
the food photo on the left, the dish name on the right) into the room.

Data: https://www.mensa-kl.de/api.php?format=json&date=N (the documented "legacy API"; data may be reused if the
source mensa-kl.de is credited, which the last message of every menu does).

    python3 mensa.py preview OUTDIR [ARG]     # render the pictures as PNG files, send nothing
"""
import datetime
import html
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("pictochat_gui", os.path.join(HERE, "pictochat_gui.py"))
gui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gui)

API = "https://www.mensa-kl.de/api.php?format=json&date=%s"
HOST = "www.mensa-kl.de"
USER_AGENT = "pictochat-bot/1.0 (hobby project; reads the public menu of mensa-kl.de)"
MAX_BYTES = 3 * 1024 * 1024
MENU_TTL = 600                       # seconds a downloaded menu is reused
NOT_FOOD = {"News"}                  # entries of the API that are announcements, not meals
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
HEADER_COLOUR = {"1": 5, "2": 13, "Atrium": 15}      # PictoChat palette index per Ausgabe (red, blue, violet)
DIET_TAGS = [(23, "vegan"), (22, "vegetarisch")]                 # food label ids of the API
ICONS = {"beef": "Rind", "pork": "Schwein", "poultry": "Geflügel", "fish": "Fisch", "game": "Wild", "lamb": "Lamm",
         "calf": "Kalb"}                                         # the site's own diet icon of a meal

USAGE = "/mensa [heute|morgen|übermorgen|N Tage|JJJJ-MM-TT]"

_menu_cache = {}                     # date argument -> (time, meals)
_image_cache = {}                    # url -> bytes


class MensaError(Exception):
    pass


# ---------------------------------------------------------------- download
def _get(url):
    """Download from mensa-kl.de only (https, also after redirects), at most MAX_BYTES, 10 s."""
    if not url.startswith("https://" + HOST + "/"):
        raise MensaError("unexpected address %s" % url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            if not r.geturl().startswith("https://" + HOST + "/"):
                raise MensaError("redirected away from %s" % HOST)
            data = r.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError) as e:
        raise MensaError("mensa-kl.de is not reachable (%s)" % e) from None
    if len(data) > MAX_BYTES:
        raise MensaError("download too large")
    return data


def parse_arg(text):
    """The part after /mensa -> (API date argument, wording for the answer). Raises MensaError for bad input."""
    word = text.strip().lower()
    if word in ("", "heute", "today"):
        return "0"
    if word in ("morgen", "tomorrow"):
        return "1"
    if word in ("übermorgen", "uebermorgen"):
        return "2"
    parts = word.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in ("tage", "tag", "days", "day") and int(parts[0]) < 1000:
        return str(int(parts[0]))
    if word.isdigit() and int(word) < 1000:
        return str(int(word))
    try:
        return datetime.date.fromisoformat(word).isoformat()
    except ValueError:
        raise MensaError("usage: " + USAGE) from None


def fetch_meals(date_arg):
    """(date string, meals of that day sorted by Ausgabe). For 'today' on a day without meals (weekend, holiday)
    the next day that has meals is used instead."""
    now = time.time()
    cached = _menu_cache.get(date_arg)
    if cached and now - cached[0] < MENU_TTL:
        return cached[1]
    try:
        raw = json.loads(_get(API % date_arg).decode("utf-8"))
    except ValueError:
        raise MensaError("mensa-kl.de sent an unreadable answer") from None
    meals = [m for m in raw if isinstance(m, dict) and m.get("loc") not in NOT_FOOD and m.get("title")]
    if not meals and date_arg == "0":
        try:
            future = json.loads(_get(API % "all").decode("utf-8"))
        except ValueError:
            future = []
        future = [m for m in future if isinstance(m, dict) and m.get("loc") not in NOT_FOOD and m.get("title")]
        if future:
            first = min(m["date"] for m in future)
            meals = [m for m in future if m["date"] == first]
    meals.sort(key=lambda m: (not str(m["loc"]).isdigit(), str(m["loc"]).zfill(3), m.get("m_id", 0)))
    date = meals[0]["date"] if meals else ""
    result = (date, meals)
    _menu_cache[date_arg] = (now, result)
    return result


def photo_bytes(meal):
    """JPEG bytes of the meal's photo, or None when nobody uploaded one."""
    if not meal.get("image"):
        return None
    url = meal.get("image_url_jpeg") or ""
    if url not in _image_cache:
        _image_cache[url] = _get(url)
        while len(_image_cache) > 40:
            _image_cache.pop(next(iter(_image_cache)))
    return _image_cache[url]


# ---------------------------------------------------------------- pictures
def ausgabe(meal):
    loc = str(meal["loc"])
    if loc.isdigit():
        return "Ausgabe " + loc
    return "Atrium" if loc.startswith("Atrium") else loc


def header_colour(meal):
    loc = str(meal["loc"])
    return HEADER_COLOUR.get(loc, HEADER_COLOUR.get("Atrium" if loc.startswith("Atrium") else "", 1))


def title_text(meal):
    return " ".join(html.unescape(str(meal["title"])).split())


def price_text(meal):
    try:
        return ("%.2f" % float(meal.get("price"))).replace(".", ",") + " €"
    except (TypeError, ValueError):
        return ""


def meta_text(meal):
    bits = []
    tags = set(meal.get("tags") or [])
    diet = next((name for tag, name in DIET_TAGS if tag in tags), "") or ICONS.get(str(meal.get("icon") or ""), "")
    if diet:
        bits.append(diet)
    if meal.get("rating_amt"):
        bits.append("★ %s (%s)" % (("%.1f" % float(meal["rating"])).replace(".", ","), meal["rating_amt"]))
    return " · ".join(bits)


def _font(px, bold=False):
    from PIL import ImageFont
    for name in (["DejaVuSans-Bold.ttf"] if bold else []) + ["DejaVuSans.ttf", "FreeSans.ttf"]:
        try:
            return ImageFont.truetype(name, px)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, width, max_lines):
    """Word-wrap into at most max_lines lines of at most `width` pixels; the last one ends in '…' when cut."""
    lines, cur = [], ""
    words = text.split()
    i = 0
    while i < len(words):
        word = words[i]
        trial = (cur + " " + word).strip()
        if draw.textlength(trial, font=font) <= width:
            cur = trial
            i += 1
        elif cur:
            lines.append(cur)
            cur = ""
        else:                                               # one word wider than the line: break it
            cut = len(word) - 1
            while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                cut -= 1
            lines.append(word[:cut])
            words[i] = word[cut:]
        if len(lines) == max_lines:
            break
    else:
        if cur:
            lines.append(cur)
        cur = ""
    if len(lines) == max_lines and (i < len(words) or cur):
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip(" ,.-") + "…"
    return lines


def _photo_rows(rows, data, box, colour):
    """Fill `box` = (x, y, w, h) of `rows` with the photo (centre crop, dithered)."""
    from PIL import Image, ImageOps
    x0, y0, w, h = box
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")
    img = ImageOps.fit(img, (w, h), Image.LANCZOS, centering=(0.5, 0.5))
    if colour:
        pal = Image.new("P", (1, 1))
        flat = []
        for i in range(16):
            flat += list(gui.PALETTE[i])
        pal.putpalette(flat + flat[-3:] * (256 - 16))
        q = img.quantize(palette=pal, dither=Image.FLOYDSTEINBERG)
        px = q.load()
        for y in range(h):
            for x in range(w):
                rows[y0 + y][x0 + x] = min(px[x, y], 15)
        return
    one = ImageOps.autocontrast(img.convert("L"), cutoff=1).convert("1", dither=Image.FLOYDSTEINBERG)
    px = one.load()
    for y in range(h):
        for x in range(w):
            if px[x, y] == 0:
                rows[y0 + y][x0 + x] = 1


def meal_picture(meal, photo=None, colour=False):
    """One 80 x 256 PictoChat picture for a meal: header (Ausgabe left, price right), photo on the left, name and
    a line with diet and rating on the right. Without a photo the text uses the full width."""
    from PIL import Image, ImageDraw
    PW, PH = gui.PW, gui.PH
    top = gui.TAB_Y1 + 1                                    # 16: the first row below the name tab
    left, right = gui.VIS_X0 + 2, gui.VIS_X1 - 1            # 25 .. 251
    col = header_colour(meal)
    rows = gui.blank()

    # header: x > 80 is free in rows 0-15 (the name tab only covers the left part)
    head = Image.new("L", (PW, top), 0)
    hd = ImageDraw.Draw(head)
    hfont = _font(11, bold=True)
    hd.text((gui.TAB_X1 + 4, 1), ausgabe(meal), fill=255, font=hfont)
    price = price_text(meal)
    if price:
        hd.text((right - hd.textlength(price, font=hfont), 1), price, fill=255, font=hfont)
    hp = head.load()
    for y in range(top):
        for x in range(PW):
            if hp[x, y] > 110:
                rows[y][x] = col
    for x in range(gui.VIS_X0, gui.VIS_X1 + 1):             # thin rule under the header
        rows[top - 1][x] = 2 if x > gui.TAB_X1 else 0

    # body
    photo_w = 0
    if photo:
        photo_w = 84
        _photo_rows(rows, photo, (left, top + 1, photo_w, PH - top - 1), colour)
    tx = left + (photo_w + 5 if photo else 3)
    tw = right - tx
    body = Image.new("L", (PW, PH), 0)
    bd = ImageDraw.Draw(body)
    font = _font(11)
    meta = meta_text(meal)
    line_h = 12
    avail = (PH - top - 3) // line_h                        # 5 lines
    lines = _wrap(bd, title_text(meal), font, tw, avail - (1 if meta else 0))
    y = top + 3
    for ln in lines:
        bd.text((tx, y), ln, fill=255, font=font)
        y += line_h
    bp = body.load()
    for yy in range(top, PH):
        for xx in range(PW):
            if bp[xx, yy] > 110:
                rows[yy][xx] = 1
    if meta:
        meta_img = Image.new("L", (PW, PH), 0)
        ImageDraw.Draw(meta_img).text((tx, PH - line_h - 1), meta, fill=255, font=_font(10, bold=True))
        mp = meta_img.load()
        for yy in range(PH - line_h - 1, PH):
            for xx in range(tx, right + 1):
                if mp[xx, yy] > 110:
                    rows[yy][xx] = col
    return gui.clip(rows)


# ---------------------------------------------------------------- the command
def day_title(date):
    d = datetime.date.fromisoformat(date)
    return "%s %d.%d." % (WEEKDAYS[d.weekday()], d.day, d.month)


def send_menu(arg, send_text, send_picture, colour=False):
    """Handle '/mensa [arg]': a title, one picture per meal, then the source line. Returns a short status string
    (also on errors: they do not raise, so the bot keeps running)."""
    try:
        date_arg = parse_arg(arg)
        date, meals = fetch_meals(date_arg)
    except MensaError as e:
        return "mensa: %s" % e
    if not meals:
        return "mensa: no meals found for %s" % (arg.strip() or "today")
    send_text("[comic big blue] Mensa KL, %s" % day_title(date))
    with_photo = failed = 0
    for meal in meals:
        try:
            photo = photo_bytes(meal)
        except MensaError:
            photo, failed = None, failed + 1
        try:
            rows = meal_picture(meal, photo, colour)
        except Exception:                                    # noqa: BLE001 - a broken photo must not stop the menu
            rows = meal_picture(meal, None, colour)
            photo = None
        with_photo += 1 if photo else 0
        send_picture(rows)
    send_text("[mono small] Quelle: mensa-kl.de")
    return ("mensa: sent %d meals for %s (%d with photo%s). The pictures are already in the room - do not "
            "repeat the dishes, just confirm in a few words." % (
                len(meals), date, with_photo, ", %d photo downloads failed" % failed if failed else ""))


# ---------------------------------------------------------------- preview
def _png(rows, path, scale=3):
    from PIL import Image
    img = Image.new("RGB", (gui.PW, gui.PH))
    px = img.load()
    for y, r in enumerate(rows):
        for x, v in enumerate(r):
            px[x, y] = gui.PALETTE[v]
    img.resize((gui.PW * scale, gui.PH * scale), Image.NEAREST).save(path)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "preview":
        out = sys.argv[2]
        os.makedirs(out, exist_ok=True)
        arg = sys.argv[3] if len(sys.argv) > 3 else ""
        colour = "--colour" in sys.argv
        date, meals = fetch_meals(parse_arg(arg.replace("--colour", "")))
        print(date, len(meals), "meals")
        for m in meals:
            p = photo_bytes(m)
            path = os.path.join(out, "%s_%s%s.png" % (m["m_id"], "colour_" if colour else "", "photo" if p else "nophoto"))
            _png(meal_picture(m, p, colour), path)
            print(path, "|", ausgabe(m), price_text(m), "|", meta_text(m), "|", title_text(m)[:60])
    else:
        print(__doc__)
