#!/usr/bin/env python3
"""
pictochat_bot.py - a Claude chatbot for the PictoChat room hosted by `pictochat.py host`.

Every chat message a DS sends arrives as a picture (handwriting, typed text or a drawing). The bot sends that
picture to Claude (vision, Messages API), renders Claude's text reply into PictoChat pictures and posts them to the
room through the host's GUI port, like pictochat_gui.py does. It can run next to the GUI.

Setup (once):
    python3 -m venv .venv && .venv/bin/pip install anthropic pillow
    export ANTHROPIC_API_KEY=...                  # or an `ant auth login` profile

Run:
    sudo python3 pictochat.py host --iface <wifi> --channel 1 --gui-port 7777
    .venv/bin/python pictochat_bot.py             # connects to 127.0.0.1:7777
    .venv/bin/python pictochat_bot.py --dry-run   # no API calls: answers with a fixed text (for testing)

Each reply costs one Messages API request (the picture is about 300 input tokens, plus the recent conversation).
Replies that use the web cost more: web_search results add input tokens, and each search is billed per use.

Tools Claude can use: draw_picture (vector shapes), send_image (download a picture and dither it like the GUI's
"Load image..."), web_search and web_fetch (server-side). --no-web turns off the last three. Downloads only go to
public http(s) addresses (never this machine or the local network), at most 8 MB, 3 images per reply.
    .venv/bin/python pictochat_bot.py --send-image URL   # test the image path without API calls
"""
import argparse
import base64
import importlib.util
import io
import json
import os
import queue
import re
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("pictochat_gui", os.path.join(HERE, "pictochat_gui.py"))
gui = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gui)

SYSTEM = """You are a participant in a Nintendo DS PictoChat room, chatting with people on their Nintendo DS.

Each message you receive is a small picture (256 x 80 pixels, shown enlarged): handwriting drawn with the stylus,
text typed on the DS keyboard, or a drawing. Read it and reply as a friendly chat partner.

Your reply is drawn as text into the same small picture format, about 33 characters per line and 4 lines per
picture at the normal size, so keep replies short: one to three sentences, usually under 120 characters, unless the
person clearly asks for something longer. Write plain text only: no markdown, no emoji, no lists.

Start each reply with a style tag in square brackets that matches its mood: a font, a size and optionally a colour,
e.g. "[comic big orange] Haha, nice one!". The tag is not shown; it sets how your words are drawn.
- fonts: plain (neutral, default), bold (excited, emphatic, loud), comic (playful, joking, silly), script
  (romantic, fancy, poetic, congratulations), serif (serious, wise, storytelling, formal), italic (whispering,
  dreamy, sarcastic, thoughtful), mono (robotic, technical, deadpan), retro (games, pixel art, nostalgia)
- sizes: small (whispers, longer answers: more words per line), normal (4 lines per picture), big (2 lines:
  excitement, short replies), huge (one line of 1-2 words: shouting, YAY, NO!); script runs larger, so use it with
  small or normal
- colours (optional, default black): black, magenta, pink, red, red-orange, orange, yellow-green, green,
  cyan-green, sky-blue, blue, deep-blue, violet; avoid yellow and grey for text, they are hard to read
Vary the style with the conversation's mood rather than using one style for everything; a plain, normal reply needs
no colour. Bigger sizes hold fewer words, so keep big and huge replies very short.

If a picture is a drawing without words, react to what it shows. If you cannot read the writing, say what you can
make out and ask. Earlier pictures in the conversation are earlier messages of the same room.

Messages are labelled with the sender's DS nickname when it is known; use names naturally. A text starting with
"[Room event]" is a notice from the room (you entered, someone joined), not a message from a person: answer it with
one short, warm greeting that uses the names given.

You can also draw: the draw_picture tool sends a picture of your own into the room. Use it when someone asks you to
draw something, or when a quick sketch says it better than words. Keep drawings simple and bold (outlines, big
shapes, a few colours) - the canvas is tiny. You can add a short text message before or after a drawing.

When someone asks for a QR code (or a link that is hard to copy by hand), use send_qr_code: it sends a real,
scannable code that a phone camera can read off the DS screen. It holds at most about 78 characters, so prefer
short URLs. Say in a short text what the code contains."""

SYSTEM_WEB = """

You can also send real pictures from the web: find one with web_search (and web_fetch if you need to look inside a
page), then call send_image with the image URL. A page URL works too: send_image then takes the page's main image.
Use this when someone asks to see something real (an animal, a place, a famous picture, a logo). The picture is
shrunk to 230 x 64 pixels and dithered to black and white like a newspaper photo, so pick images with one clear,
large subject and strong contrast; set colour=true for colourful subjects. You see the result after sending; if it
came out unrecognisable, you may try one better image. Only search when it helps - a plain chat reply needs no web
access. If someone asks a factual question about current events, a quick web_search is fine too."""


def message_png(msg, scale=3):
    """PNG (bytes) of a `03 02` chat message's picture, enlarged `scale` times, in the DS's colours."""
    return rows_png(gui.decode_image(msg), scale)


def rows_png(rows, scale=3):
    """PNG (bytes) of an 80 x 256 PictoChat picture (palette indices), enlarged `scale` times."""
    from PIL import Image
    img = Image.new("RGB", (gui.PW, gui.PH))
    img.putdata([gui.PALETTE.get(v, (128, 128, 128)) for r in rows for v in r])
    img = img.resize((gui.PW * scale, gui.PH * scale), Image.NEAREST)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


# ---------------------------------------------------------------- image download (send_image tool)
MAX_DOWNLOAD = 8 * 1024 * 1024
USER_AGENT = "pictochat-bot/1.0 (hobby project; fetches one image per request)"


class FetchError(Exception):
    pass


def _check_url(url):
    """Allow only http(s) URLs whose host resolves to public addresses only, so a model-chosen URL can never reach
    this machine or the local network (the host's GUI port, the router, ...)."""
    import ipaddress
    from urllib.parse import urlsplit
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise FetchError("only http(s) URLs are allowed")
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError("cannot resolve %s (%s)" % (u.hostname, e)) from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise FetchError("%s resolves to a non-public address" % u.hostname)


def http_get(url, max_bytes=MAX_DOWNLOAD, timeout=10, redirects=5):
    """GET with every redirect target re-checked by _check_url and a size cap. Returns (content_type, data, url)."""
    import urllib.error
    import urllib.request
    from urllib.parse import urljoin

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    for _ in range(redirects + 1):
        _check_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/*,text/html;q=0.8,*/*;q=0.5"})
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                url = urljoin(url, e.headers["Location"])
                continue
            raise FetchError("HTTP %d from %s" % (e.code, url)) from None
        except (urllib.error.URLError, OSError) as e:
            raise FetchError("download failed: %s" % getattr(e, "reason", e)) from None
        with resp:
            data = resp.read(max_bytes + 1)
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if len(data) > max_bytes:
            raise FetchError("file larger than %d MB" % (max_bytes // (1024 * 1024)))
        return ctype, data, url
    raise FetchError("too many redirects")


def page_image_url(html_bytes, base):
    """The main image of an HTML page: og:image, twitter:image, link rel=image_src, else the first <img>."""
    import html
    import re
    from urllib.parse import urljoin
    text = html_bytes[:2_000_000].decode("utf-8", "replace")
    pats = [r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::url|:secure_url)?|twitter:image(?::src)?)["\'][^>]*?'
            r'content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name)=["\'](?:og:image|twitter:image)',
            r'<link[^>]+rel=["\']image_src["\'][^>]*?href=["\']([^"\']+)',
            r'<img[^>]+src=["\']([^"\']+\.(?:jpe?g|png|webp|gif)(?:\?[^"\']*)?)["\']']
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            return urljoin(base, html.unescape(m.group(1)))
    return None


def download_image(url):
    """Image bytes for `url`: an image URL, or a web page whose main image is used. Returns (data, image_url)."""
    ctype, data, final = http_get(url)
    if ctype.startswith("image/") or (not ctype.startswith("text/") and _looks_like_image(data)):
        return data, final
    if "html" in ctype or data.lstrip()[:1] == b"<":
        img = page_image_url(data, final)
        if not img:
            raise FetchError("the page has no recognisable main image; pass a direct image URL")
        ctype, data, final = http_get(img)
        if ctype.startswith("image/") or _looks_like_image(data):
            return data, final
    raise FetchError("not an image (content type %r)" % ctype)


def _looks_like_image(data):
    from PIL import Image
    try:
        Image.open(io.BytesIO(data)).verify()
        return True
    except Exception:  # noqa: BLE001 - any Pillow failure means "not an image we can read"
        return False


def image_rows(data, colour=False, dither=True, invert=False):
    """Convert image bytes exactly like the GUI's "Load image...": fit 230 x 64 below the name tab, dither."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 50_000_000          # Pillow refuses larger images (decompression bombs)
    try:
        return gui.clip(gui.image_to_pixels(io.BytesIO(data), dither=dither, invert=invert, color=colour))
    except Image.DecompressionBombError:
        raise FetchError("image too large") from None
    except Exception as e:  # noqa: BLE001 - Pillow raises many types for broken files
        raise FetchError("cannot read the image: %s" % e) from None


SEND_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "http(s) URL of an image, or of a web page (its main image is used)"},
        "colour": {"type": "boolean", "description": "map to the 16 PictoChat colours instead of black and white "
                                                     "(default false)"},
        "dither": {"type": "boolean", "description": "Floyd-Steinberg dithering (default true); false = hard threshold,"
                                                     " good for logos and line art"},
        "invert": {"type": "boolean", "description": "invert brightness first (default false)"},
    },
    "required": ["url"],
    "additionalProperties": False,
}

MODEL_ALIASES = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5",
                 "fable": "claude-fable-5-1"}
# server-side refusal fallbacks exist only for these (Claude Haiku 5.5 has none: a fallback list there is a 400)
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
# the dynamic-filtering web tool versions are documented for these; every other model gets the basic versions
DYNAMIC_WEB_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                      "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6"}


def web_tools(model):
    search, fetch = (("web_search_20260209", "web_fetch_20260209") if model in DYNAMIC_WEB_MODELS
                     else ("web_search_20250305", "web_fetch_20250910"))
    return [{"type": search, "name": "web_search", "max_uses": 3},
            {"type": fetch, "name": "web_fetch", "max_uses": 3}]


def resolve_model(name):
    name = name.strip().lower()
    return MODEL_ALIASES.get(name, name)


# ---------------------------------------------------------------- text styles (font, size, colour by mood)
# A reply may start with a style tag such as "[comic big red]": known words in any order, removed before drawing.
# Font files are looked up by name (Pillow searches the system font folders); the first one found is used, and
# DejaVu Sans when none is. On Debian/Raspberry Pi OS: fonts-dejavu-core, fonts-dejavu-extra (italic),
# fonts-comic-neue (comic), fonts-urw-base35 (script).
FONT_FILES = {
    "plain": ["DejaVuSans.ttf"],
    "bold": ["DejaVuSans-Bold.ttf"],
    "italic": ["DejaVuSans-Oblique.ttf", "DejaVuSerif-Italic.ttf"],
    "serif": ["DejaVuSerif.ttf"],
    "comic": ["ComicNeue-Bold.otf", "ComicNeue-Regular.otf"],
    "script": ["Z003-MediumItalic.otf", "Z003-MediumItalic.t1"],
    "mono": ["DejaVuSansMono.ttf"],
    "retro": ["DejaVuSansMono-Bold.ttf", "DejaVuSansMono.ttf"],   # drawn at half size without smoothing, doubled
}
FONT_SIZES = {"small": 11, "normal": 13, "big": 20, "huge": 30}
FONT_SCALE = {"script": 1.35, "comic": 1.1, "retro": 1.2}   # these look small at the same nominal size
FONT_MIN = {"script": 16, "retro": 8}   # thinner or blockier fonts need more pixels to stay readable on the DS
STYLE_TAG = re.compile(r"^\s*\[([a-z\- ]+)\]\s*")
_font_cache = {}


def parse_style(text):
    """Split an optional leading style tag off a reply: ('comic', 'big', colour index or None), rest of the text.
    A bracket at the start that contains other words is left alone (it may be part of the message)."""
    m = STYLE_TAG.match(text)
    if not m:
        return ("plain", "normal", None), text
    words = m.group(1).split()
    if not words or not all(w in FONT_FILES or w in FONT_SIZES or w in COLORS for w in words):
        return ("plain", "normal", None), text
    font = next((w for w in words if w in FONT_FILES), "plain")
    size = next((w for w in words if w in FONT_SIZES), "normal")
    colour = next((COLORS[w] for w in words if w in COLORS and COLORS[w] != 0), None)
    return (font, size, colour), text[m.end():]


def load_font(font, px):
    from PIL import ImageFont
    key = (font, px)
    if key not in _font_cache:
        f = None
        for name in FONT_FILES.get(font, []) + ["DejaVuSans.ttf"]:
            try:
                f = ImageFont.truetype(name, px)
                break
            except OSError:
                continue
        _font_cache[key] = f or ImageFont.load_default()
    return _font_cache[key]


def styled_pages(text, style=("plain", "normal", None), color=1, max_pages=3):
    """Render a reply in the given style into PictoChat pictures (80 rows of 256 palette indices each): word-wrapped
    to the visible width below the name tab (columns 26-252, rows 16-79), as many lines per picture as the font
    size allows, at most max_pages pictures (the last one ends in '...' if the text is longer)."""
    from PIL import Image, ImageDraw
    font_name, size_name, colour = style
    colour = colour or color
    retro = font_name == "retro"
    k = 2 if retro else 1                                   # retro: draw at half resolution, then double
    px = max(FONT_MIN.get(font_name, 6), round(FONT_SIZES[size_name] * FONT_SCALE.get(font_name, 1.0) / k))
    font = load_font(font_name, px)
    x0, x1, y0, y1 = gui.TEXT_X0, gui.TEXT_X1, gui.TAB_Y1 + 1, gui.PH - 1
    width, height = (x1 - x0) // k, (y1 - y0 + 1) // k
    d = ImageDraw.Draw(Image.new("L", (1, 1)))
    line_h = max(px + 1, round(px * 1.22))                # 16 px at the normal 13 px, like the GUI's text
    per_page = max(1, height // line_h)

    def fits(s):
        return d.textlength(s, font=font) <= width

    lines = []
    for para in text.strip().split("\n"):
        cur = ""
        for word in para.split(" "):
            while not fits(word):                           # a word wider than the picture: break it
                cut = len(word) - 1
                while cut > 1 and not fits(word[:cut]):
                    cut -= 1
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:cut])
                word = word[cut:]
            trial = (cur + " " + word).strip()
            if fits(trial) or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        lines.append(cur)
    pages = [lines[i:i + per_page] for i in range(0, len(lines), per_page)] or [[""]]
    if len(pages) > max_pages:
        pages = pages[:max_pages]
        last = pages[-1][-1]
        while last and not fits(last + "..."):
            last = last[:-1]
        pages[-1][-1] = last + "..."
    out = []
    for page in pages:
        img = Image.new("L", (width, height), 0)
        dr = ImageDraw.Draw(img)
        if retro:
            dr.fontmode = "1"                               # no smoothing: crisp pixels
        top = 0 if len(page) > 1 or per_page > 2 else (height - line_h) // 2   # a lone big line sits in the middle
        for i, ln in enumerate(page):
            dr.text((0, top + i * line_h), ln, fill=255, font=font)
        if k != 1:
            img = img.resize((width * k, height * k), Image.NEAREST)
        rows = gui.blank()
        p = img.load()
        for y in range(img.height):
            for x in range(img.width):
                if p[x, y] > 110:
                    rows[y0 + y][x0 + x] = colour
        out.append(gui.clip(rows))
    return out


def reply_pictures(text, color=1, max_pages=3):
    """Wrap a reply into lines and split them into PictoChat pictures (4 lines each, at most max_pages)."""
    lines = [ln for ln in gui.wrap_text(text.strip())]
    pages = [lines[i:i + gui.TEXT_LINES] for i in range(0, len(lines), gui.TEXT_LINES)] or [[""]]
    if len(pages) > max_pages:
        pages = pages[:max_pages]
        last = pages[-1]
        last[-1] = (last[-1][:-3] if len(last[-1]) > 30 else last[-1]) + "..."
    return [gui.clip(gui.text_to_pixels("\n".join(p), color)) for p in pages]


# ---------------------------------------------------------------- drawing tool
CANVAS_W, CANVAS_H = gui.VIS_X1 - gui.VIS_X0 + 1, gui.PH - (gui.TAB_Y1 + 1)   # 230 x 64: visible, below the name tab
COLORS = {"black": 1, "grey": 2, "magenta": 3, "pink": 4, "red": 5, "red-orange": 6, "orange": 7, "yellow": 8,
          "yellow-green": 9, "green": 10, "cyan-green": 11, "sky-blue": 12, "blue": 13, "deep-blue": 14,
          "violet": 15, "white": 0}

DRAW_SCHEMA = {
    "type": "object",
    "properties": {
        "shapes": {
            "type": "array",
            "description": "Shapes drawn in order (later ones on top). Coordinates are pixels on a %d x %d canvas, "
                           "x to the right (0-%d), y downwards (0-%d)." % (CANVAS_W, CANVAS_H, CANVAS_W - 1, CANVAS_H - 1),
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["line", "polyline", "polygon", "rect", "ellipse", "text"],
                             "description": "line/polyline: 2+ points; polygon: 3+ points; rect/ellipse: 2 points "
                                            "(opposite corners of the bounding box); text: 1 point (top-left)"},
                    "points": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}},
                               "description": "[[x, y], ...]"},
                    "color": {"type": "string", "enum": sorted(COLORS, key=COLORS.get),
                              "description": "PictoChat palette colour ('white' erases)"},
                    "fill": {"type": "boolean", "description": "fill polygon/rect/ellipse (default false)"},
                    "width": {"type": "integer", "description": "line width in pixels, 1-6 (default 2)"},
                    "text": {"type": "string", "description": "for kind=text: the words"},
                    "size": {"type": "integer", "description": "for kind=text: font height in pixels, 8-40 (default 13)"},
                },
                "required": ["kind", "points", "color"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["shapes"],
    "additionalProperties": False,
}


def render_shapes(shapes):
    """Draw tool shapes into an 80 x 256 PictoChat picture (palette indices). Raises ValueError on bad input.
    Every shape is drawn as a 1-bit mask (no anti-aliasing), so only real palette colours are used."""
    from PIL import Image, ImageDraw, ImageFont
    if not isinstance(shapes, list) or not shapes:
        raise ValueError("shapes must be a non-empty list")
    if len(shapes) > 200:
        raise ValueError("at most 200 shapes")
    canvas = [[0] * CANVAS_W for _ in range(CANVAS_H)]
    for i, sh in enumerate(shapes):
        kind, color = sh.get("kind"), sh.get("color")
        if color not in COLORS:
            raise ValueError("shape %d: unknown color %r" % (i, color))
        try:
            pts = [(int(p[0]), int(p[1])) for p in sh.get("points", [])]
        except (TypeError, ValueError, IndexError):
            raise ValueError("shape %d: points must be [[x, y], ...]" % i) from None
        need = {"line": 2, "polyline": 2, "polygon": 3, "rect": 2, "ellipse": 2, "text": 1}.get(kind)
        if need is None:
            raise ValueError("shape %d: unknown kind %r" % (i, kind))
        if len(pts) < need:
            raise ValueError("shape %d: %s needs at least %d points" % (i, kind, need))
        width = max(1, min(6, int(sh.get("width", 2) or 2)))
        fill = bool(sh.get("fill", False))
        mask = Image.new("1", (CANVAS_W, CANVAS_H), 0)
        d = ImageDraw.Draw(mask)
        if kind in ("line", "polyline"):
            d.line(pts, fill=1, width=width, joint="curve")
        elif kind == "polygon":
            if fill:
                d.polygon(pts, fill=1)
            d.line(pts + [pts[0]], fill=1, width=width, joint="curve")
        elif kind in ("rect", "ellipse"):
            (x0, y0), (x1, y1) = pts[0], pts[1]
            box = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
            f = d.rectangle if kind == "rect" else d.ellipse
            f(box, fill=1 if fill else None, outline=1, width=width)
        else:
            text = str(sh.get("text", ""))[:80]
            size = max(8, min(40, int(sh.get("size", 13) or 13)))
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", size)
            except OSError:
                font = ImageFont.load_default()
            d.fontmode = "1"
            d.text(pts[0], text, fill=1, font=font)
        px = mask.load()
        idx = COLORS[color]
        for y in range(CANVAS_H):
            row = canvas[y]
            for x in range(CANVAS_W):
                if px[x, y]:
                    row[x] = idx
    rows = gui.blank()
    for y in range(CANVAS_H):
        rows[gui.TAB_Y1 + 1 + y][gui.VIS_X0:gui.VIS_X1 + 1] = canvas[y]
    return rows


QR_QUIET = 2   # modules of blank paper around the code (the standard asks for 4; the bubble adds more on the sides)


def qr_rows(text):
    """A real, scannable QR code for `text` as 80 rows of 256 palette indices (black on paper), as large as the DS
    picture allows: the smallest QR version that holds the text, at the largest whole number of pixels per module
    that fits the 80-pixel height with a quiet zone, centred right of the name tab (columns 81-252).
    Returns (rows, description); raises ValueError if the text is too long (about 78 bytes at most)."""
    import segno
    if not text:
        raise ValueError("empty text")
    try:
        qr = segno.make_qr(text, error="l", boost_error=True)   # smallest version, then the best ECC that still fits
    except segno.DataOverflowError:
        raise ValueError("text too long for a QR code")
    n = qr.symbol_size(scale=1, border=0)[0]
    scale = gui.PH // (n + 2 * QR_QUIET)
    if scale < 2:
        raise ValueError("text too long: it needs a %dx%d QR code (version %s), which would be under 2 pixels per "
                         "module on the DS; at most about 78 characters fit (fewer for non-ASCII)" % (n, n, qr.version))
    side = n * scale
    x_left = gui.TAB_X1 + 1
    x0 = x_left + (gui.VIS_X1 + 1 - x_left - side) // 2
    y0 = (gui.PH - side) // 2
    rows = gui.blank()
    for r, line in enumerate(qr.matrix):
        for c, v in enumerate(line):
            if v:
                for dy in range(scale):
                    rows[y0 + r * scale + dy][x0 + c * scale:x0 + (c + 1) * scale] = [1] * scale
    return gui.clip(rows), "QR version %s (%dx%d), error correction %s, %d px per module" % (
        qr.version, n, n, qr.error, scale)


QR_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "What the code should contain: a URL, plain text, a phone number "
                                                  "(tel:...), a Wi-Fi login (WIFI:T:WPA;S:name;P:password;;), etc. "
                                                  "At most about 78 characters."},
    },
    "required": ["text"],
}


class ClaudeChat:
    """Keeps the recent conversation (pictures in, text out) and asks Claude for each new picture."""

    def __init__(self, model, effort, history, fallbacks=True, web=True):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort, self.keep, self.fallbacks, self.web = model, effort, history, fallbacks, web
        self.messages = []      # alternating user (picture) / assistant (text); assistant turns are plain text

    def _extra(self):
        """Fallback parameters for models that have server-side fallbacks, else nothing."""
        if self.fallbacks and self.model in FALLBACK_MODELS:
            return dict(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        return {}

    def set_model(self, name):
        """Switch to another model after a free lookup confirms this key can use it. Returns (ok, message)."""
        new = resolve_model(name)
        old, self.model = self.model, new
        ok, info = self.check()
        if not ok:
            self.model = old
            return False, info + "; still using %s" % old
        return True, "now using %s" % new

    def check(self):
        """Free start-up check (a model lookup, no tokens billed): key, network and model access."""
        a = self.anthropic
        try:
            m = self.client.models.retrieve(self.model)
            return True, "API reachable, key accepted, model %s available" % m.id
        except a.AuthenticationError:
            return False, "the API rejected the key: is ANTHROPIC_API_KEY set in this terminal and complete?"
        except a.NotFoundError:
            return False, "model %s is not available to this key (try /model haiku)" % self.model
        except a.APIStatusError as e:
            return False, "API error %s: %s" % (e.status_code, e.message)
        except a.APIConnectionError as e:
            cause = e.__cause__
            if cause is not None and not isinstance(cause, OSError) and "Error" in type(cause).__name__ \
                    and type(cause).__module__ == "builtins":
                import traceback
                print("internal error inside the HTTP library (not a network problem); traceback:")
                traceback.print_exception(cause)
            proxy = [k for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
                     if os.environ.get(k)]
            return False, ("cannot reach api.anthropic.com: %s (%r)%s. Run with ANTHROPIC_LOG=debug for details."
                           % (type(e).__name__, e.__cause__ or e,
                              "; proxy variables set: " + ", ".join(proxy) if proxy else ""))

    def ask(self, pictures, send_text, send_picture):
        """Answer DS pictures: a list of (png bytes, sender label), usually one; several when they arrived while the
        previous reply was being made. Text goes out through send_text(str), drawings through send_picture(rows), in
        the order Claude produces them. Returns a short status string for the log."""
        from anthropic import beta_tool
        drawn = []

        @beta_tool(name="draw_picture", input_schema=DRAW_SCHEMA,
                   description="Draw a picture and send it into the PictoChat room as its own message. Call this "
                               "when someone asks you to draw something, or when a quick sketch answers better than "
                               "words. The canvas is %d x %d pixels in the 16 PictoChat colours; keep it simple "
                               "and bold." % (CANVAS_W, CANVAS_H))
        def draw_picture(shapes):
            try:
                rows = render_shapes(shapes)
            except (ValueError, TypeError) as e:
                return "Error: %s. Nothing was sent; fix the shapes and call draw_picture again." % e
            send_picture(rows)
            drawn.append(len(shapes))
            return "The picture was sent to the room (%d shapes)." % len(shapes)

        sent_images = []

        @beta_tool(name="send_image", input_schema=SEND_IMAGE_SCHEMA,
                   description="Download a picture from the web and send it into the PictoChat room as its own "
                               "message, converted like PictoChat's image import: shrunk to 230 x 64 pixels and "
                               "dithered to black and white (or mapped to the 16 PictoChat colours with colour=true)."
                               " Returns a preview of exactly what was sent.")
        def send_image(url, colour=False, dither=True, invert=False):
            if len(sent_images) >= 3:
                return "Error: at most 3 images per reply. Nothing was sent."
            try:
                data, src = download_image(str(url))
                rows = image_rows(data, colour=bool(colour), dither=bool(dither), invert=bool(invert))
            except FetchError as e:
                return "Error: %s. Nothing was sent." % e
            send_picture(rows)
            sent_images.append(src)
            return [
                {"type": "text", "text": "Sent to the room (image from %s). This is what the DS shows:" % src},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(rows_png(rows, 2)).decode("ascii")}},
            ]

        sent_qr = []

        @beta_tool(name="send_qr_code", input_schema=QR_SCHEMA,
                   description="Send a real, scannable QR code into the PictoChat room as its own message (black on "
                               "white, as large as the DS picture allows; a phone camera can read it off the DS "
                               "screen). Use it when someone asks for a QR code, or to hand over a link that is "
                               "tedious to copy by hand. The text is encoded exactly as given, so check URLs. "
                               "Returns a preview of what was sent.")
        def send_qr_code(text):
            if len(sent_qr) >= 3:
                return "Error: at most 3 QR codes per reply. Nothing was sent."
            try:
                rows, info = qr_rows(str(text))
            except ValueError as e:
                return "Error: %s. Nothing was sent; shorten the text (e.g. a shorter URL) and try again." % e
            send_picture(rows)
            sent_qr.append(text)
            return [
                {"type": "text", "text": "QR code sent to the room (%s). This is what the DS shows:" % info},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(rows_png(rows, 2)).decode("ascii")}},
            ]

        content = []
        for png, sender in pictures:
            content += [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.standard_b64encode(png).decode("ascii")}},
                {"type": "text", "text": "New PictoChat message from %s." % sender},
            ]
        if len(pictures) > 1:
            content.append({"type": "text", "text": "These %d messages arrived in quick succession; answer them "
                                                    "together in one short reply." % len(pictures)})
        user = {"role": "user", "content": content}
        tools = [draw_picture, send_qr_code] + ([send_image] + web_tools(self.model) if self.web else [])
        kwargs = dict(model=self.model, max_tokens=4096, system=SYSTEM + (SYSTEM_WEB if self.web else ""),
                      messages=self.messages + [user], tools=tools, output_config={"effort": self.effort},
                      max_iterations=8)
        kwargs.update(self._extra())    # on a safety decline the API retries on a fallback model (where it exists)
        said, usage_in, usage_out, last = [], 0, 0, None
        try:
            for message in self.client.beta.messages.tool_runner(**kwargs):
                last = message
                usage_in += message.usage.input_tokens
                usage_out += message.usage.output_tokens
                if message.stop_reason == "refusal":
                    send_text("Sorry, I can't answer that one.")
                    return "refused (%s)" % getattr(message.stop_details, "category", None)
                # each message is yielded before its tool calls run, so text and drawings keep their order
                # cited answers (web search) arrive as several text blocks of one sentence: join without spaces
                text = "".join(b.text for b in message.content if b.type == "text").strip()
                if text:
                    said.append(text)
                    send_text(text)
        except self.anthropic.AuthenticationError:
            return "no valid API key (set ANTHROPIC_API_KEY)"
        except self.anthropic.RateLimitError:
            return "rate limited, try again in a moment"
        except self.anthropic.APIStatusError as e:
            return "API error %s: %s" % (e.status_code, e.message)
        except self.anthropic.APIConnectionError as e:
            return "no connection to the API: %s (%r)" % (type(e).__name__, e.__cause__ or e)
        if last is None or (not said and not drawn and not sent_images and not sent_qr):
            return "empty reply (stop_reason %s)" % (last.stop_reason if last else None)
        # only plain text goes into the history (no thinking or tool blocks), so trimming old turns is safe
        summary = " ".join(said)
        if drawn:
            summary = (summary + " " if summary else "") + "[I sent %d drawing(s).]" % len(drawn)
        for src in sent_images:
            summary = (summary + " " if summary else "") + "[I sent an image from %s.]" % src
        for t in sent_qr:
            summary = (summary + " " if summary else "") + "[I sent a QR code for %r.]" % t
        self.messages += [user, {"role": "assistant", "content": summary}]
        if len(self.messages) > 2 * self.keep:
            self.messages = self.messages[-2 * self.keep:]
        return "ok, %d drawing(s), %d image(s), %d QR code(s), %d in / %d out tokens" % (
            len(drawn), len(sent_images), len(sent_qr), usage_in, usage_out)

    def greet(self, event, send_text):
        """Say hello for a room event (a plain-text note such as 'You just entered the room. In the room: Ann, Bo.').
        No tools: one short text reply, kept in the history so later replies know who is here."""
        user = {"role": "user", "content": "[Room event] " + event}
        kwargs = dict(model=self.model, max_tokens=2048, system=SYSTEM, messages=self.messages + [user],
                      output_config={"effort": self.effort})
        kwargs.update(self._extra())
        try:
            message = self.client.beta.messages.create(**kwargs)
        except self.anthropic.APIStatusError as e:
            return "API error %s: %s" % (e.status_code, e.message)
        except self.anthropic.APIConnectionError as e:
            return "no connection to the API: %s" % type(e).__name__
        if message.stop_reason == "refusal":
            return "refused"
        text = "".join(b.text for b in message.content if b.type == "text").strip()
        if not text:
            return "empty greeting (stop_reason %s)" % message.stop_reason
        send_text(text)
        self.messages += [user, {"role": "assistant", "content": text}]
        if len(self.messages) > 2 * self.keep:
            self.messages = self.messages[-2 * self.keep:]
        return "ok, %d in / %d out tokens" % (message.usage.input_tokens, message.usage.output_tokens)


def member_info(msg):
    """(mac string, nickname, kind) of a `03 00` (joining) / `03 01` (ready) member message, else None."""
    if len(msg) < 28 or msg[0] != 3 or msg[1] not in (0, 1):
        return None
    name = msg[8:28].decode("utf-16-le", "replace").split("\x00")[0].strip()
    return sender_of(msg), name, "joining" if msg[1] == 0 else "ready"


def name_list(names):
    names = [n for n in names if n]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


class Room:
    """Who is in the room, from the host's GUI events: names from member messages, presence from `state` events
    (MACs of associated DSes, every 5 s) and 'deauthenticated' log lines."""

    def __init__(self):
        self.names = {}          # mac -> nickname (also of DSes that left; the replay contains old joins)
        self.present = None      # set of macs from the latest state event; None until the first one arrives
        self.greeted = {}        # mac -> time of the last personal welcome

    def on_member(self, msg):
        info = member_info(msg)
        if info:
            self.names[info[0]] = info[1]
        return info

    def on_state(self, ev):
        self.present = set((ev.get("aids") or {}).keys())

    def on_log(self, text):
        """Handles the host's '# station <mac> deauthenticated ...' line; returns the MAC of a DS that left, or None."""
        import re
        m = re.match(r"# station ([0-9a-f:]{17}) deauthenticated", text or "")
        if not m:
            return None
        if self.present is not None:
            self.present.discard(m.group(1))
        return m.group(1)

    def present_names(self):
        macs = self.present if self.present is not None else set(self.names)
        return [self.names.get(m, "") for m in sorted(macs)]


DEMO_SHAPES = [  # used by --dry-run: a sun, a house and a caption
    {"kind": "ellipse", "points": [[8, 6], [36, 34]], "color": "yellow", "fill": True},
    {"kind": "rect", "points": [[90, 30], [140, 62]], "color": "red", "width": 2},
    {"kind": "polygon", "points": [[84, 30], [115, 8], [146, 30]], "color": "blue", "fill": True},
    {"kind": "rect", "points": [[108, 44], [122, 62]], "color": "orange", "fill": True},
    {"kind": "line", "points": [[0, 63], [229, 63]], "color": "green", "width": 3},
    {"kind": "text", "points": [[160, 20]], "color": "violet", "text": "hello!", "size": 16},
]


def bot_name(model):
    """PictoChat nickname for the bot: 'claude-' + family initial + version digits, e.g. claude-opus-5-5 ->
    claude-o55. PictoChat nicknames hold at most 10 characters, so the full model id does not fit."""
    parts = model.lower().split("-")
    if parts and parts[0] == "claude":
        parts = parts[1:]
    family = next((p for p in parts if p.isalpha()), "")
    digits = "".join(p for p in parts if p.isdigit())
    return ("claude-" + family[:1] + digits)[:10]


def sender_of(msg):
    return ":".join("%02x" % x for x in gui.swap16(msg[2:8]))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7777, help="the host's --gui-port")
    ap.add_argument("--model", default="claude-sonnet-5-5", help="model id, or an alias: haiku, sonnet (default), opus, fable; change it while running with /model")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh", "max"],
                    help="reasoning effort per reply (low keeps chat replies quick)")
    ap.add_argument("--history", type=int, default=10, help="how many earlier exchanges Claude sees")
    ap.add_argument("--max-pages", type=int, default=3, help="at most this many pictures per reply")
    ap.add_argument("--color", type=int, default=1, help="palette index of the reply text (1 = black, 3-15 rainbow)")
    ap.add_argument("--no-fallbacks", action="store_true", help="do not retry declined requests on a fallback model")
    ap.add_argument("--answer-backlog", action="store_true", help="also answer messages replayed when connecting")
    ap.add_argument("--bot-name", help="nickname of the host in the room (max 10 characters; default derived from "
                                       "--model, e.g. claude-o55 for claude-opus-5-5)")
    ap.add_argument("--keep-name", action="store_true", help="do not rename the host")
    ap.add_argument("--dry-run", action="store_true", help="no API calls: reply with a fixed text")
    ap.add_argument("--no-web", action="store_true", help="no web search, web fetch or image downloads")
    ap.add_argument("--no-greet", action="store_true", help="do not say hi on entering or welcome DSes that join")
    ap.add_argument("--send-image", metavar="URL", help="TEST: download, dither and post this image (or page's "
                                                        "main image) to the room, then exit; no API calls")
    ap.add_argument("--colour", action="store_true", help="with --send-image: 16-colour instead of black and white")
    ap.add_argument("--send-qr", metavar="TEXT", help="TEST: post a QR code for TEXT to the room, then exit; no API "
                                                      "calls")
    ap.add_argument("--qr-png", metavar="FILE", help="with --send-qr: only write a preview PNG, do not connect")
    a = ap.parse_args()
    if a.send_qr:
        try:
            rows, info = qr_rows(a.send_qr)
        except ValueError as e:
            sys.exit("send-qr failed: %s" % e)
        if a.qr_png:
            with open(a.qr_png, "wb") as fh:
                fh.write(rows_png(rows, 4))
            print("wrote %s (%s)" % (a.qr_png, info))
            return
        s = socket.create_connection((a.host, a.port), timeout=3)
        s.sendall((json.dumps({"cmd": "chat", "pixels": rows}) + "\n").encode())
        s.close()
        print("sent QR code for %r (%s)" % (a.send_qr, info))
        return

    if a.send_image:
        try:
            data, src = download_image(a.send_image)
            rows = image_rows(data, colour=a.colour)
        except FetchError as e:
            sys.exit("send-image failed: %s" % e)
        s = socket.create_connection((a.host, a.port), timeout=3)
        s.sendall((json.dumps({"cmd": "chat", "pixels": rows}) + "\n").encode())
        s.close()
        print("sent image from %s (%d bytes downloaded)" % (src, len(data)))
        return

    a.model = resolve_model(a.model)
    chat = None if a.dry_run else ClaudeChat(a.model, a.effort, a.history, not a.no_fallbacks, not a.no_web)
    if chat:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            print("note: ANTHROPIC_API_KEY is not set in this terminal")
        ok, info = chat.check()
        print(("API check: " if ok else "API check FAILED: ") + info, flush=True)
    work = queue.Queue()

    def log(s):
        print(time.strftime("%H:%M:%S"), s, flush=True)

    def connect():
        while True:
            try:
                s = socket.create_connection((a.host, a.port), timeout=3)
                s.settimeout(None)
                log("connected to the host at %s:%d" % (a.host, a.port))
                return s
            except OSError:
                time.sleep(2)

    sock_holder = {}

    def post(px):
        s = sock_holder.get("s")
        try:
            s.sendall((json.dumps({"cmd": "chat", "pixels": px}) + "\n").encode())
            return True
        except (OSError, AttributeError):
            log("could not send: not connected to the host")
            return False

    def send_text(text):
        style, body = parse_style(text)
        log("reply%s: %s" % ("" if style == ("plain", "normal", None) else " [%s %s%s]" % (
            style[0], style[1], " colour %d" % style[2] if style[2] else ""), body))
        for px in styled_pages(body, style, a.color, a.max_pages):
            if not post(px):
                break

    def send_picture(rows):
        log("reply: (drawing)")
        post(gui.clip(rows))

    ident = {"name": (a.bot_name or bot_name(a.model))[:10], "model": a.model}

    def handle_job(job):
        if job[0] == "greet":
            _, event, names = job
            if a.dry_run:
                send_text("Hi %s! I'm %s, happy to chat." % (name_list(names) or "everyone", ident["name"]))
                info = "dry run"
            else:
                info = chat.greet(event, send_text)
            log("greeting (%s) -> %s" % (name_list(names) or "everyone", info))
            return
        msgs = job[1]
        labels = []
        for msg in msgs:
            who = sender_of(msg)
            who_name = room.names.get(who)
            labels.append("%s (%s)" % (who_name, who) if who_name else who)
        if a.dry_run:
            send_text("I got %d picture(s). Bot is in dry-run mode; here is a test drawing." % len(msgs))
            send_picture(render_shapes(DEMO_SHAPES))
            info = "dry run"
        else:
            info = chat.ask([(message_png(m), l) for m, l in zip(msgs, labels)], send_text, send_picture)
        log("%s from %s -> %s" % ("message" if len(msgs) == 1 else "%d messages" % len(msgs),
                                  ", ".join(dict.fromkeys(labels)), info))

    later = []

    def next_job():
        """The next job; chat messages that are already waiting are merged into one job, so several pictures sent
        in quick succession get one reply instead of one each (each reply keeps the room busy for about a second)."""
        job = later.pop(0) if later else work.get()
        if job[0] != "chat":
            return job
        msgs = [job[1]]
        while len(msgs) < 6:
            try:
                nxt = work.get_nowait()
            except queue.Empty:
                break
            if nxt[0] == "chat":
                msgs.append(nxt[1])
            else:
                later.append(nxt)
        return ("chat", msgs)

    def worker():
        # one failed job (API error, broken picture, missing library) must not stop the bot: the process would
        # keep running with a dead worker, so a service manager would never restart it
        while True:
            job = next_job()
            try:
                handle_job(job)
            except Exception:  # noqa: BLE001
                import traceback
                log("job %s failed:\n%s" % (job[0], traceback.format_exc().rstrip()))

    def command(line):
        """Terminal commands: /model [name], /effort [level], /help."""
        parts = line.split()
        if not parts:
            return
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd == "/model":
            if not arg:
                log("model: %s (aliases: %s; any model id works)" % (
                    ident["model"], ", ".join("%s = %s" % kv for kv in MODEL_ALIASES.items())))
                return
            if chat:
                ok, info = chat.set_model(arg)
                log(info)
                if not ok:
                    return
            ident["model"] = chat.model if chat else resolve_model(arg)
            if not chat:
                log("now using %s (dry run: no API check)" % ident["model"])
            if not a.bot_name and not a.keep_name:
                ident["name"] = bot_name(ident["model"])
                s = sock_holder.get("s")
                if s:
                    try:
                        s.sendall((json.dumps({"cmd": "set_name", "name": ident["name"]}) + "\n").encode())
                        log("room nickname: %s (DSes see it when they next join)" % ident["name"])
                    except OSError:
                        pass
        elif cmd == "/effort":
            levels = ("low", "medium", "high", "xhigh", "max")
            if arg in levels:
                a.effort = arg
                if chat:
                    chat.effort = arg
                log("effort: %s" % arg)
            else:
                log("effort: %s (choose one of %s)" % (a.effort, ", ".join(levels)))
        elif cmd in ("/help", "/?"):
            log("commands: /model [haiku|sonnet|opus|fable|<model id>], /effort [low|medium|high|xhigh|max], /help")
        else:
            log("unknown command %r (try /help)" % cmd)

    def stdin_reader():
        for line in sys.stdin:
            try:
                command(line.strip())
            except Exception as e:  # noqa: BLE001 - a bad command must not kill the bot
                log("command failed: %s" % e)

    room = Room()
    threading.Thread(target=worker, daemon=True).start()
    if sys.stdin:
        threading.Thread(target=stdin_reader, daemon=True).start()
        log("model %s; type /model to see or change it, /help for all commands" % a.model)
    while True:
        s = connect()
        sock_holder["s"] = s
        if not a.keep_name:
            s.sendall((json.dumps({"cmd": "set_name", "name": ident["name"]}) + "\n").encode())
            log("room nickname: %s (DSes that join from now on see it; rejoin the room to see the new name)" % ident["name"])
        t_conn = time.time()
        ignore_until = 0 if a.answer_backlog else t_conn + 1.5        # the host replays its history on connect
        entry_done = a.no_greet
        s.settimeout(0.5)
        buf = b""
        try:
            while True:
                now = time.time()
                # say hi once the replay is through and the first state event told us who is here (max ~6.5 s)
                if not entry_done and now >= ignore_until and (room.present is not None or now > t_conn + 6.5):
                    entry_done = True
                    names = room.present_names()
                    if names:
                        for m in (room.present or room.names):
                            room.greeted[m] = now
                        known = [n for n in names if n]
                        event = ("You (%s) just entered the PictoChat room. In the room: %s%s. Say hi to everyone, "
                                 "by name." % (ident["name"], name_list(known) or "people",
                                               " and %d more" % (len(names) - len(known)) if known and
                                               len(known) < len(names) else ""))
                        work.put(("greet", event, names))
                    else:
                        log("room is empty; will welcome the first DS that joins")
                try:
                    d = s.recv(65536)
                except socket.timeout:
                    continue
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    kind = ev.get("kind")
                    if kind == "state":
                        room.on_state(ev)
                        continue
                    if kind == "log":
                        gone = room.on_log(ev.get("text"))
                        if gone:
                            log("%s left the room" % (room.names.get(gone) or gone))
                        continue
                    if kind != "message" or ev.get("dir") != "rx" or not ev.get("hex"):
                        continue
                    msg = bytes.fromhex(ev["hex"])
                    info = room.on_member(msg)
                    if info:
                        mac, name, mkind = info
                        if room.present is not None:
                            room.present.add(mac)
                        # a DS that joins after the bot is in: welcome it by name (once per 2 minutes per DS)
                        if (mkind == "ready" and time.time() >= ignore_until and not a.no_greet
                                and time.time() - room.greeted.get(mac, 0) > 120):
                            room.greeted[mac] = time.time()
                            log("%s joined the room" % (name or mac))
                            work.put(("greet", "%s just joined the PictoChat room. Welcome them by name."
                                      % (name or "Someone (no nickname)"), [name]))
                        continue
                    if msg[:2] != b"\x03\x02":
                        continue
                    if time.time() < ignore_until:
                        continue
                    work.put(("chat", msg))
        except OSError:
            pass
        sock_holder.pop("s", None)
        log("lost the host connection, reconnecting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
