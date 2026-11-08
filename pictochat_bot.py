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
"""
import argparse
import base64
import importlib.util
import io
import json
import os
import queue
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
picture, so keep replies short: one to three sentences, usually under 120 characters, unless the person clearly
asks for something longer. Write plain text only: no markdown, no emoji, no lists.

If a picture is a drawing without words, react to what it shows. If you cannot read the writing, say what you can
make out and ask. Earlier pictures in the conversation are earlier messages of the same room.

You can also draw: the draw_picture tool sends a picture of your own into the room. Use it when someone asks you to
draw something, or when a quick sketch says it better than words. Keep drawings simple and bold (outlines, big
shapes, a few colours) - the canvas is tiny. You can add a short text message before or after a drawing."""


def message_png(msg, scale=3):
    """PNG (bytes) of a `03 02` chat message's picture, enlarged `scale` times, in the DS's colours."""
    from PIL import Image
    rows = gui.decode_image(msg)
    img = Image.new("RGB", (gui.PW, gui.PH))
    img.putdata([gui.PALETTE.get(v, (128, 128, 128)) for r in rows for v in r])
    img = img.resize((gui.PW * scale, gui.PH * scale), Image.NEAREST)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


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


class ClaudeChat:
    """Keeps the recent conversation (pictures in, text out) and asks Claude for each new picture."""

    def __init__(self, model, effort, history, fallbacks=True):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort, self.keep, self.fallbacks = model, effort, history, fallbacks
        self.messages = []      # alternating user (picture) / assistant (text); assistant turns are plain text

    def check(self):
        """Free start-up check (a model lookup, no tokens billed): key, network and model access."""
        a = self.anthropic
        try:
            m = self.client.models.retrieve(self.model)
            return True, "API reachable, key accepted, model %s available" % m.id
        except a.AuthenticationError:
            return False, "the API rejected the key: is ANTHROPIC_API_KEY set in this terminal and complete?"
        except a.NotFoundError:
            return False, "model %s is not available to this key (try --model claude-sonnet-5-5)" % self.model
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

    def ask(self, png, sender, send_text, send_picture):
        """Answer one DS picture. Text goes out through send_text(str), drawings through send_picture(rows), in the
        order Claude produces them. Returns a short status string for the log."""
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

        user = {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(png).decode("ascii")}},
            {"type": "text", "text": "New PictoChat message from %s." % sender},
        ]}
        kwargs = dict(model=self.model, max_tokens=4096, system=SYSTEM, messages=self.messages + [user],
                      tools=[draw_picture], output_config={"effort": self.effort}, max_iterations=4)
        if self.fallbacks:
            # on a safety decline the API retries on a fallback model inside the same request
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
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
                text = " ".join(b.text for b in message.content if b.type == "text").strip()
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
        if last is None or (not said and not drawn):
            return "empty reply (stop_reason %s)" % (last.stop_reason if last else None)
        # only plain text goes into the history (no thinking or tool blocks), so trimming old turns is safe
        summary = " ".join(said)
        if drawn:
            summary = (summary + " " if summary else "") + "[I sent %d drawing(s).]" % len(drawn)
        self.messages += [user, {"role": "assistant", "content": summary}]
        if len(self.messages) > 2 * self.keep:
            self.messages = self.messages[-2 * self.keep:]
        return "ok, %d drawing(s), %d in / %d out tokens" % (len(drawn), usage_in, usage_out)


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
    ap.add_argument("--model", default="claude-opus-5-5")
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
    a = ap.parse_args()

    chat = None if a.dry_run else ClaudeChat(a.model, a.effort, a.history, not a.no_fallbacks)
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
        log("reply: %s" % text)
        for px in reply_pictures(text, a.color, a.max_pages):
            if not post(px):
                break

    def send_picture(rows):
        log("reply: (drawing)")
        post(gui.clip(rows))

    def worker():
        while True:
            msg = work.get()
            who = sender_of(msg)
            if a.dry_run:
                send_text("I got your picture (%d bytes). Bot is in dry-run mode; here is a test drawing." % len(msg))
                send_picture(render_shapes(DEMO_SHAPES))
                info = "dry run"
            else:
                info = chat.ask(message_png(msg), who, send_text, send_picture)
            log("message from %s -> %s" % (who, info))

    threading.Thread(target=worker, daemon=True).start()
    while True:
        s = connect()
        sock_holder["s"] = s
        if not a.keep_name:
            name = (a.bot_name or bot_name(a.model))[:10]
            s.sendall((json.dumps({"cmd": "set_name", "name": name}) + "\n").encode())
            log("room nickname: %s (DSes that join from now on see it; rejoin the room to see the new name)" % name)
        ignore_until = 0 if a.answer_backlog else time.time() + 1.5     # the host replays its history on connect
        buf = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("kind") != "message" or ev.get("dir") != "rx" or not ev.get("hex"):
                        continue
                    msg = bytes.fromhex(ev["hex"])
                    if msg[:2] != b"\x03\x02":
                        continue                         # member messages (joining / ready), not chat
                    if time.time() < ignore_until:
                        continue
                    work.put(msg)
        except OSError:
            pass
        sock_holder.pop("s", None)
        log("lost the host connection, reconnecting")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
