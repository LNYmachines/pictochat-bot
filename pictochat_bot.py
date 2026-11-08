#!/usr/bin/env python3
"""
pictochat_bot.py - a Claude chatbot for the PictoChat room hosted by `pictochat.py host`.

Every chat message a DS sends arrives as a picture (handwriting, typed text or a drawing). The bot sends that
picture to Claude (vision, Messages API), renders Claude's text reply into PictoChat pictures and posts them to the
room through the host's GUI port, like pictochat_gui.py does. It can run next to the GUI.

Setup (once):
    python3 -m venv --system-site-packages .venv && .venv/bin/pip install anthropic
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
make out and ask. Earlier pictures in the conversation are earlier messages of the same room."""


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


class ClaudeChat:
    """Keeps the recent conversation (pictures in, text out) and asks Claude for each new picture."""

    def __init__(self, model, effort, history, fallbacks=True):
        import anthropic
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model, self.effort, self.keep, self.fallbacks = model, effort, history, fallbacks
        self.messages = []      # alternating user (picture) / assistant (text); assistant turns are plain text

    def ask(self, png, sender):
        user = {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(png).decode("ascii")}},
            {"type": "text", "text": "New PictoChat message from %s." % sender},
        ]}
        kwargs = dict(model=self.model, max_tokens=1024, system=SYSTEM, messages=self.messages + [user],
                      output_config={"effort": self.effort})
        try:
            if self.fallbacks:
                # on a safety decline the API retries on a fallback model inside the same request
                resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                        fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except self.anthropic.AuthenticationError:
            return None, "no valid API key (set ANTHROPIC_API_KEY)"
        except self.anthropic.RateLimitError:
            return None, "rate limited, try again in a moment"
        except self.anthropic.APIStatusError as e:
            return None, "API error %s: %s" % (e.status_code, e.message)
        except self.anthropic.APIConnectionError:
            return None, "no connection to the API"
        if resp.stop_reason == "refusal":
            return "Sorry, I can't answer that one.", "refused (%s)" % getattr(resp.stop_details, "category", None)
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            return None, "empty reply (stop_reason %s)" % resp.stop_reason
        # only plain text goes back into the history, so trimming old turns never edits a thinking block
        self.messages += [user, {"role": "assistant", "content": text}]
        if len(self.messages) > 2 * self.keep:
            self.messages = self.messages[-2 * self.keep:]
        return text, "ok, %d in / %d out tokens" % (resp.usage.input_tokens, resp.usage.output_tokens)


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
    ap.add_argument("--dry-run", action="store_true", help="no API calls: reply with a fixed text")
    a = ap.parse_args()

    chat = None if a.dry_run else ClaudeChat(a.model, a.effort, a.history, not a.no_fallbacks)
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

    def worker():
        while True:
            msg = work.get()
            who = sender_of(msg)
            if a.dry_run:
                text, info = "I got your picture (%d bytes). Bot is in dry-run mode." % len(msg), "dry run"
            else:
                text, info = chat.ask(message_png(msg), who)
            log("message from %s -> %s" % (who, info))
            if not text:
                continue
            log("reply: %s" % text)
            for px in reply_pictures(text, a.color, a.max_pages):
                s = sock_holder.get("s")
                try:
                    s.sendall((json.dumps({"cmd": "chat", "pixels": px}) + "\n").encode())
                except (OSError, AttributeError):
                    log("could not send the reply: not connected")
                    break

    threading.Thread(target=worker, daemon=True).start()
    while True:
        s = connect()
        sock_holder["s"] = s
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
