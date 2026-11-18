#!/usr/bin/env python3
"""Settings page for clip-watcher, served to your browser from this machine only."""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import watcher

HERE = Path(__file__).parent
PAGE = HERE / "gui.html"
LAST = HERE / "last_session.txt"
NOISE = ("frames/s", "%|", "WARN:")  # progress bars and library chatter kept out of the log
KEEP = 2000  # log lines held for the page
# what the page calls each option, and its unit; anything missing falls back to the flag name
LABELS = {
    "--kick": ("Channel is on Kick", ""), "--quality": ("Stream quality", ""),
    "--min": ("Minimum messages", ""), "--factor": ("Spike multiplier", "x"),
    "--bucket": ("Counting interval", "s"), "--window": ("Comparison window", "s"),
    "--before": ("Before the spike", "s"), "--after": ("After the spike", "s"),
    "--cooldown": ("Pause between clips", "s"), "--ratio": ("Aspect ratio", ""),
    "--height": ("Clip height", "px"), "--crf": ("Picture quality", ""),
    "--cam": ("Streamer cam position", ""), "--face-share": ("Cam panel size", ""),
    "--face-zoom": ("Cam zoom", ""), "--big-face": ("Full-frame face threshold", ""),
    "--model": ("Subtitle model", ""), "--language": ("Subtitle language", ""),
    "--upload": ("Upload to YouTube Shorts", ""), "--privacy": ("Visibility", ""),
    "--title": ("Title template", ""), "--description": ("Description template", ""),
}
# shown when a setting is hovered or focused: what it is for and when to change it
DETAILS = {
    "--kick": "Tick this when the streamer is on kick.com and you typed only their name. "
              "Leave it off for twitch.tv. A pasted kick.com link is recognised on its own.",
    "--quality": "The sharpest picture that gets recorded. Higher looks better but fills the disk faster, "
                 "roughly 3 to 4 GB per hour at 1080p. If the stream does not offer it, the next one down is used.",
    "--min": "A spike needs at least this many chat messages in one counting interval, so a quiet chat "
             "does not trigger clips on a handful of messages. Try 5 for small channels, 40 or more for huge ones.",
    "--factor": "How much busier than normal chat has to get. 2.5 means two and a half times the usual rate. "
                "Lower gives more clips, higher keeps only the biggest moments.",
    "--bucket": "Chat is counted in slices of this many seconds. Shorter slices react faster but are jumpier. "
                "10 suits most channels.",
    "--window": "The usual chat rate is taken from this many seconds of history. "
                "No clip is made until this much time has passed after you start.",
    "--before": "Seconds of video kept from before the spike was noticed. Chat reacts a few seconds late, "
                "so the moment itself sits in this part.",
    "--after": "Seconds kept after the spike was noticed, for the reaction. The clip is cut once they have passed.",
    "--cooldown": "After a clip, new spikes are ignored for this long, so one long moment does not turn into "
                  "several overlapping clips. Leave empty to use the clip length.",
    "--ratio": "Shape of the finished clip. 9:16 is upright for Shorts, Reels and TikTok, 1:1 is square, "
               "16:9 is normal widescreen.",
    "--height": "Height of the finished clip in pixels. 1920 with 9:16 gives the standard 1080 x 1920. "
                "A smaller number makes smaller files.",
    "--crf": "Trade between picture quality and file size. 18 is close to the original, 23 is the usual balance, "
             "28 is small and soft.",
    "--cam": "Where the streamer's facecam goes when it is a small box on top of a game: its own panel on top "
             "or at the bottom. Off keeps a single picture cropped from the middle.",
    "--face-share": "How tall the cam panel is, as a part of the whole clip. 0.33 gives it a third.",
    "--face-zoom": "How tightly the cam panel is cropped around the face. 4 shows about four face widths. "
                   "A smaller number zooms in closer.",
    "--big-face": "When the streamer's face is wider than this part of the picture, they are the whole show, "
                  "as in a talking stream, so the clip follows the face and no cam panel is made.",
    "--model": "The speech-to-text model that writes the subtitles. It runs on this machine. tiny is fastest, "
               "base is the usual choice, small and medium are more accurate and much slower.",
    "--language": "Language the streamer speaks, as a two-letter code such as en, de or es. "
                  "Leave empty to have it detected for each clip.",
    "--upload": "Sends every finished clip to your YouTube channel as a Short. "
                "Needs the one-time YouTube setup described in the README.",
    "--privacy": "Who can see uploaded clips. Private is only you, unlisted is anyone with the link, "
                 "public is everyone.",
    "--title": "Title given to each upload. {text} becomes the first words spoken in the clip "
               "and {channel} the streamer's name.",
    "--description": "Description given to each upload, with the same {text} and {channel} placeholders. "
                     "A credit link to the stream is added for you.",
}


def options():
    """Every watcher.py option, grouped as in --help, in a shape the page can draw a form from."""
    groups = []
    # argparse keeps its groups in private attributes; reading them keeps the page in step with the CLI
    for group in watcher.build_parser()._action_groups:
        items = [
            {
                "flag": a.option_strings[0],
                "label": LABELS.get(a.option_strings[0], (a.option_strings[0][2:].replace("-", " ").capitalize(),))[0],
                "unit": LABELS.get(a.option_strings[0], ("", ""))[1],
                "detail": DETAILS.get(a.option_strings[0], ""),
                "help": a.help or "",
                "kind": "bool" if a.nargs == 0 else "choice" if a.choices else "text",
                "choices": list(a.choices or []),
                "default": "" if a.default is None else f"{a.default:g}" if isinstance(a.default, float) else a.default,
            }
            for a in group._group_actions
            if a.option_strings and a.help != argparse.SUPPRESS and a.dest != "help"
        ]
        if items:
            groups.append({"title": group.title, "options": items})
    return groups


FLAGS = {o["flag"] for g in options() for o in g["options"]}


class Runner:
    """The one watcher.py process this page controls, and its output."""

    def __init__(self):
        self.proc, self.exit, self.lines, self.first = None, None, [], 0
        self.lock = threading.Lock()

    def start(self, arguments):
        with self.lock:
            if self.proc:
                return "Already watching. Stop first."
            self.exit = None
            self.lines.append("> watcher.py " + " ".join(arguments))
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(HERE / "watcher.py"), "--stdin-stop", *arguments],
                cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            threading.Thread(target=self.pump, args=(self.proc,), daemon=True).start()

    def pump(self, proc):
        for line in proc.stdout:
            if line.strip() and not any(n in line for n in NOISE):
                with self.lock:
                    self.lines.append(line.rstrip())
                    extra = len(self.lines) - KEEP
                    if extra > 0:
                        del self.lines[:extra]
                        self.first += extra
        code = proc.wait()
        with self.lock:
            self.proc, self.exit = None, code

    def stop(self):
        with self.lock:
            if self.proc and not self.proc.stdin.closed:
                self.proc.stdin.close()  # watcher.py finishes clips in progress, then exits

    def state(self, since):
        with self.lock:
            return {
                "running": self.proc is not None,
                "stopping": bool(self.proc and self.proc.stdin.closed),
                "exit": self.exit,
                "lines": self.lines[max(since - self.first, 0):],
                "next": self.first + len(self.lines),
            }


PORT = 8791  # gui.html looks for the helper here when it is opened straight from disk


def make_server(port=0):
    """HTTP server on 127.0.0.1 plus the secret its page must send with every request."""
    token, runner = secrets.token_urlsafe(16), Runner()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def local(self):
            return self.headers.get("Host", "") in (f"127.0.0.1:{self.server.server_port}",
                                                    f"localhost:{self.server.server_port}")

        def allowed(self):
            # the page we serve carries the token and other websites cannot read that page, so they
            # cannot drive this server; the Host check stops DNS rebinding
            if not self.local() or not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self.reply(403, {"error": "clip-watcher was restarted. Reload this page"})
                return False
            return True

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                if not self.local():
                    return self.reply(403, {"error": "Not found."})
                page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                self.reply(200, page.encode(), "text/html; charset=utf-8")
            elif not self.allowed():
                return
            elif path == "/api/options":
                last = LAST.read_text(encoding="utf-8").splitlines() if LAST.exists() else []
                self.reply(200, {"groups": options(), "last": last})
            elif path == "/api/state":
                since = parse_qs(query).get("since", ["0"])[0]
                self.reply(200, runner.state(int(since) if since.isdigit() else 0))
            else:
                self.reply(404, {"error": "Not found."})

        def do_POST(self):
            if not self.allowed():
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 65536:
                return self.reply(413, {"error": "Request too large."})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                channel, arguments = str(body.get("channel", "")).strip(), list(body.get("args", []))
            except (ValueError, AttributeError, TypeError):
                return self.reply(400, {"error": "Bad request."})
            if self.path == "/api/stop":
                runner.stop()
                return self.reply(200, {})
            if self.path == "/api/open-clips":
                folder = HERE / "clips"
                folder.mkdir(exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(folder)
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
                return self.reply(200, {})
            if self.path != "/api/start":
                return self.reply(404, {"error": "Not found."})
            if not watcher.parse_channel(channel):
                return self.reply(400, {"error": "Enter a channel name, or a twitch.tv or kick.com link"})
            if not all(isinstance(a, str) and a.partition("=")[0] in FLAGS for a in arguments):
                return self.reply(400, {"error": "Unknown setting."})
            error = runner.start([channel, *arguments])
            if error:
                return self.reply(409, {"error": error})
            LAST.write_text("".join(f"{a}\n" for a in [channel, *arguments]), encoding="utf-8")
            self.reply(200, {})

    class Server(ThreadingHTTPServer):
        # on Windows address reuse would let a second clip-watcher bind the same port and steal requests
        allow_reuse_address = sys.platform != "win32"

    httpd = Server(("127.0.0.1", port), Handler)
    httpd.runner = runner
    return httpd, token


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=PORT, help=f"port to serve on (default {PORT})")
    p.add_argument("--no-browser", action="store_true", help="print the link instead of opening a browser")
    args = p.parse_args()
    url = f"http://127.0.0.1:{args.port}/"
    try:
        httpd, _ = make_server(args.port)
    except OSError:
        # most likely clip-watcher is already running: show that one instead of starting a second
        print(f"Port {args.port} is in use. If clip-watcher is already running, its page is {url}", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        return
    print(f"clip-watcher settings: {url}\nKeep this window open. Ctrl+C quits.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.runner.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
