"""mensa-kl.de menu for the PictoChat bot. /mensa sends an overview into the room: a few pictures with one line per
dish (counter, diet badge, name, price). /mensa 1, /mensa atrium, /mensa grill ... send one photo card per dish of
that counter (colour photo on the left, name on the right, badges for diet and rating); /mensa alle sends them all.

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
def _find_host_file(name):
    """pictochat_gui.py comes from the PictoChat host repo: next to this file, in $PICTOCHAT_HOST, or ../pictochat_host."""
    for d in (HERE, os.environ.get("PICTOCHAT_HOST", ""), os.path.join(HERE, "..", "pictochat_host")):
        if d and os.path.isfile(os.path.join(d, name)):
            return os.path.join(d, name)
    sys.exit(f"{name} not found: put the PictoChat host repo next to this one (../pictochat_host) or set PICTOCHAT_HOST")


_spec = importlib.util.spec_from_file_location("pictochat_gui", _find_host_file("pictochat_gui.py"))
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

USAGE = "/mensa [1|2|atrium|grill|abend|alle] [heute|morgen|übermorgen|N Tage|JJJJ-MM-TT]"

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
    # Dates are worked out here, on this machine's clock: the service's own "today" (date=0) still shows
    # yesterday for a while after midnight.
    ahead = lambda n: (datetime.date.today() + datetime.timedelta(days=n)).isoformat()
    if word in ("", "heute", "today"):
        return ahead(0)
    if word in ("morgen", "tomorrow"):
        return ahead(1)
    if word in ("übermorgen", "uebermorgen"):
        return ahead(2)
    parts = word.split()
    if len(parts) == 2 and parts[0].isdigit() and parts[1] in ("tage", "tag", "days", "day") and int(parts[0]) < 1000:
        return ahead(int(parts[0]))
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
    if not meals and date_arg == datetime.date.today().isoformat():
        try:
            future = json.loads(_get(API % "all").decode("utf-8"))
        except ValueError:
            future = []
        future = [m for m in future if isinstance(m, dict) and m.get("loc") not in NOT_FOOD and m.get("title")
                  and str(m.get("date", "")) >= date_arg]
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
        # stronger contrast and colour first: the palette has only white, grey, black and a saturated rainbow,
        # so a flat photo would dither into confetti
        from PIL import ImageEnhance
        img = ImageEnhance.Color(ImageOps.autocontrast(img, cutoff=1)).enhance(1.7)
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


COUNTER_COLOUR = {"Ausgabe 1": 5, "Ausgabe 2": 13, "Atrium": 15, "Grill": 6, "Abend": 3}   # one colour per counter
COUNTER_SHORT = {"Ausgabe 1": "A1", "Ausgabe 2": "A2", "Atrium": "Atr", "Grill": "Grill", "Abend": "Abend"}
# diet badge: (text, short text, background colour, text colour)
DIET_BADGE = {"vegan": ("vegan", "V+", 10, 1), "vegetarisch": ("vegetarisch", "V", 9, 1), "Fisch": ("Fisch", "Fi", 13, 0),
              "Geflügel": ("Geflügel", "Gf", 7, 1), "Rind": ("Rind", "Ri", 6, 0), "Schwein": ("Schwein", "Sw", 4, 0),
              "Wild": ("Wild", "Wi", 6, 0), "Lamm": ("Lamm", "La", 6, 0), "Kalb": ("Kalb", "Ka", 6, 0)}
PER_PAGE = 4                         # dishes per overview picture


def counter_colour(meal):
    return COUNTER_COLOUR.get(ausgabe(meal), 1)


def diet(meal):
    tags = set(meal.get("tags") or [])
    return next((name for tag, name in DIET_TAGS if tag in tags), "") or ICONS.get(str(meal.get("icon") or ""), "")


def rating_text(meal):
    if not meal.get("rating_amt"):
        return ""
    return "★ %s (%s)" % (("%.1f" % float(meal["rating"])).replace(".", ","), meal["rating_amt"])


def _canvas():
    from PIL import Image, ImageDraw
    img = Image.new("P", (gui.PW, gui.PH), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"                                        # no anti-aliasing: every pixel is a palette index
    return img, d


def _rows(img):
    px = img.load()
    return [[px[x, y] & 15 for x in range(gui.PW)] for y in range(gui.PH)]


def _chip(d, x, y, text, bg, fg, font, min_w=0, h=11):
    """A filled, rounded label. Returns the x just right of it."""
    w = max(min_w, int(d.textlength(text, font=font)) + 7)
    d.rounded_rectangle((x, y, x + w - 1, y + h - 1), 2, fill=bg)
    d.text((x + (w - d.textlength(text, font=font)) / 2, y + (h - font.size) // 2 - 1), text, fill=fg, font=font)
    return x + w


def _fit(d, text, font, width):
    if d.textlength(text, font=font) <= width:
        return text
    while len(text) > 1 and d.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip(" ,.-&") + "…"


def overview_pictures(date, meals):
    """The whole menu in a few pictures: PER_PAGE dishes each, one line per dish (counter label in the counter's
    colour, diet badge, name, price). The header next to the name tab carries the day and the page number; on the
    last page it also credits the source."""
    pages = [meals[i:i + PER_PAGE] for i in range(0, len(meals), PER_PAGE)]
    out = []
    left, right, top = gui.VIS_X0 + 2, gui.VIS_X1 - 1, gui.TAB_Y1 + 1
    chip_font, font, bold = _font(9, True), _font(10), _font(10, True)
    for n, page in enumerate(pages):
        img, d = _canvas()
        last = n == len(pages) - 1
        title = "Quelle: mensa-kl.de" if last and len(pages) > 1 else "Mensa KL · %s" % day_title(date)
        if last and len(pages) == 1:
            title = "%s · mensa-kl.de" % day_title(date)
        d.text((gui.TAB_X1 + 4, 1), title, fill=1, font=_font(11, True))
        pg = "%d/%d" % (n + 1, len(pages))
        d.text((right - d.textlength(pg, font=bold), 2), pg, fill=1, font=bold)
        d.line((gui.TAB_X1 + 1, top - 1, gui.VIS_X1, top - 1), fill=2)
        y = top + 2
        for meal in page:
            x = _chip(d, left, y, COUNTER_SHORT.get(ausgabe(meal), ausgabe(meal)[:5]), counter_colour(meal), 0,
                      chip_font, min_w=32) + 2
            badge = DIET_BADGE.get(diet(meal))
            if badge:
                x = _chip(d, x, y, badge[1], badge[2], badge[3], chip_font, min_w=15) + 2
            price = price_text(meal).replace(" €", "")     # the overview is tight: prices without the sign
            pw = int(d.textlength(price, font=bold))
            d.text((right - pw, y), price, fill=1, font=bold)
            d.text((x + 1, y), _fit(d, title_text(meal), font, right - pw - 5 - x), fill=1, font=font)
            y += 15
        out.append(gui.clip(_rows(img)))
    return out


def meal_picture(meal, photo=None, colour=True):
    """One 80 x 256 PictoChat picture for a meal: header (counter label left, price right), the photo on the left,
    the name on the right, and a line of badges (diet, rating) at the bottom. Without a photo the text uses the
    full width."""
    left, right, top = gui.VIS_X0 + 2, gui.VIS_X1 - 1, gui.TAB_Y1 + 1
    col = counter_colour(meal)
    img, d = _canvas()
    _chip(d, gui.TAB_X1 + 4, 1, ausgabe(meal), col, 0, _font(10, True), h=13)
    price = price_text(meal)
    hfont = _font(11, True)
    d.text((right - d.textlength(price, font=hfont), 1), price, fill=1, font=hfont)
    d.line((gui.TAB_X1 + 1, top - 1, gui.VIS_X1, top - 1), fill=2)

    photo_w = 84 if photo else 0
    tx = left + (photo_w + 5 if photo else 3)
    font, line_h = _font(11), 12
    badge, stars = DIET_BADGE.get(diet(meal)), rating_text(meal)
    has_meta = bool(badge or stars)
    avail = (gui.PH - top - 3) // line_h                    # 5 lines
    y = top + 3
    for ln in _wrap(d, title_text(meal), font, right - tx, avail - (1 if has_meta else 0)):
        d.text((tx, y), ln, fill=1, font=font)
        y += line_h
    x, by = tx, gui.PH - 12
    if badge:
        x = _chip(d, x, by, badge[0], badge[2], badge[3], _font(9, True)) + 4
    if stars:
        d.text((x, by), stars, fill=6, font=_font(10, True))
    rows = _rows(img)
    if photo:
        _photo_rows(rows, photo, (left, top + 1, photo_w, gui.PH - top - 1), colour)
    return gui.clip(rows)


# ---------------------------------------------------------------- the command
def day_title(date):
    d = datetime.date.fromisoformat(date)
    return "%s %d.%d." % (WEEKDAYS[d.weekday()], d.day, d.month)


COUNTER_WORDS = {"1": "Ausgabe 1", "2": "Ausgabe 2", "a1": "Ausgabe 1", "a2": "Ausgabe 2", "atrium": "Atrium",
                 "atr": "Atrium", "grill": "Grill", "abend": "Abend", "alle": "*", "all": "*", "fotos": "*",
                 "photos": "*"}


def split_arg(arg):
    """'/mensa' argument -> (counter filter or None, the rest for parse_arg). '1', 'atrium', 'grill' ... choose the
    photo cards of one counter, 'alle' the cards of every dish; 'ausgabe 1' works too."""
    words = [w for w in arg.strip().lower().split() if w != "ausgabe"]
    counter = None
    rest = []
    for i, w in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if counter is None and w in COUNTER_WORDS and nxt not in ("tage", "tag", "days", "day"):
            counter = COUNTER_WORDS[w]
        else:
            rest.append(w)
    return counter, " ".join(rest)


def send_menu(arg, send_text, send_picture, colour=True):
    """Handle '/mensa [counter] [day]'. Without a counter: the overview, a few pictures with one line per dish.
    With a counter ('1', '2', 'atrium', 'grill', 'abend') or 'alle': one photo card per dish. Returns a short status
    string (also on errors: they do not raise, so the bot keeps running)."""
    try:
        counter, rest = split_arg(arg)
        date, meals = fetch_meals(parse_arg(rest))
    except MensaError as e:
        return "mensa: %s" % e
    if not meals:
        return "mensa: no meals found for %s" % (arg.strip() or "today")
    counters = []
    for m in meals:
        if ausgabe(m) not in counters:
            counters.append(ausgabe(m))
    hint = ", ".join("/mensa %s" % (c[-1] if c.startswith("Ausgabe ") else c.lower()) for c in counters)
    if counter is None:
        pages = overview_pictures(date, meals)
        for rows in pages:
            send_picture(rows)
        return ("mensa: the overview of %d dishes for %s is already in the room (%d pictures, source credited). Do "
                "not repeat the dishes. Say in one short line that photo cards per counter come with: %s (or "
                "/mensa alle)." % (len(meals), day_title(date), len(pages), hint))
    chosen = meals if counter == "*" else [m for m in meals if ausgabe(m) == counter]
    if not chosen:
        return "mensa: no dishes at %s on %s; counters that day: %s" % (counter, day_title(date), hint)
    with_photo = failed = 0
    for meal in chosen:
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
    send_text("[mono small] %s · Quelle: mensa-kl.de" % day_title(date))
    return ("mensa: sent %d photo cards (%s, %s; %d with photo%s). The pictures are already in the room - do not "
            "repeat the dishes, just confirm in a few words." % (
                len(chosen), "all counters" if counter == "*" else counter, day_title(date), with_photo,
                ", %d photo downloads failed" % failed if failed else ""))


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
        counter, rest = split_arg(arg)
        date, meals = fetch_meals(parse_arg(rest))
        print(date, len(meals), "meals")
        for i, rows in enumerate(overview_pictures(date, meals)):
            _png(rows, os.path.join(out, "overview_%d.png" % (i + 1)))
        for m in meals:
            if counter not in (None, "*") and ausgabe(m) != counter:
                continue
            p = photo_bytes(m)
            _png(meal_picture(m, p), os.path.join(out, "card_%s_%s.png" % (ausgabe(m).replace(" ", ""), m["m_id"])))
            print(ausgabe(m), price_text(m), "|", diet(m), rating_text(m), "|", title_text(m)[:60])
    else:
        print(__doc__)
