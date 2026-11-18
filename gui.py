#!/usr/bin/env python3
"""Streams board for clip-watcher, served to your browser from this machine only."""
import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import live
import watcher

HERE = Path(__file__).parent
PAGE = HERE / "gui.html"
SAVED = HERE / "streams.json"
PORT = 8791  # gui.html looks for the helper here when it is opened straight from disk
KEEP = 2000  # log lines held for the page
HISTORY = 60  # chat counts kept per stream for its chart
LIMIT = 16  # streams on the board at most
# a row carries its own minimum, multiplier and site; in the shared settings these only preset new rows
PER_STREAM = ("--min", "--factor", "--kick")
ON_THE_PAGE = ("--category", "--top")  # the page has its own finder for these
# the two lines of watcher.py the board reads; the first feeds the row's numbers and is kept out of the log
STAT = re.compile(r"\[[^\]]+\] (\d+)s \| Msgs: (\d+) \| "
                  r"(?:(warming up|chat not counted)|Median: ([\d.]+) \| Threshold: (\d+)) \| (recording|no video)$")
CLIP = re.compile(r"\] saved (\S+\.mp4)")

# what the page calls each option, and its unit; anything missing falls back to the flag name
LABELS = {
    "--kick": ("Plain names are Kick channels", ""), "--quality": ("Stream quality", ""),
    "--rotate-mins": ("New recording file every", "min"), "--overlap-secs": ("Overlap at a switch", "s"),
    "--keep-rec": ("Keep recording files", ""),
    "--min": ("Minimum messages", ""), "--factor": ("Spike multiplier", "x"),
    "--bucket": ("Counting interval", "s"), "--window": ("Comparison window", "s"), "--warmup": ("Warm-up", "s"),
    "--before": ("Before the spike", "s"), "--after": ("After the spike", "s"),
    "--cooldown": ("Pause between clips", "s"), "--ratio": ("Aspect ratio", ""),
    "--height": ("Clip height", "px"), "--crf": ("Picture quality", ""),
    "--cam": ("Streamer cam position", ""), "--face-share": ("Cam panel size", ""),
    "--face-zoom": ("Cam zoom", ""), "--big-face": ("Full-frame face threshold", ""),
    "--model": ("Subtitle model", ""), "--language": ("Subtitle language", ""),
    "--jobs": ("Clips finished at once", ""),
    "--upload": ("Upload to YouTube Shorts", ""), "--privacy": ("Visibility", ""),
    "--title": ("Title template", ""), "--description": ("Description template", ""),
}
# shown when a setting is hovered or focused: what it is for and when to change it
DETAILS = {
    "--kick": "Tick this when the streams you add by plain name are on kick.com. "
              "A pasted twitch.tv or kick.com link is recognised on its own.",
    "--quality": "The sharpest picture that gets recorded. Higher looks better but uses more disk and bandwidth, "
                 "roughly 3 to 4 GB per hour at 1080p. If the stream does not offer it, the next one down is used.",
    "--rotate-mins": "The recording is only a working copy. After this many minutes a fresh file takes over and "
                     "the old one is deleted, so a stream never holds much more than one file's worth of disk.",
    "--overlap-secs": "At a switch the old and the new recording run side by side this long, so a clip that "
                      "falls on the switch is still whole. It is raised by itself when clips are long.",
    "--keep-rec": "Keep every recording file instead of deleting it. A long stream then fills the disk at "
                  "roughly 3 to 4 GB per hour.",
    "--min": "The value new streams start with; each stream has its own on the board. A spike needs at least "
             "this many chat messages in one counting interval. Try 5 for small channels, 100 or more for huge ones.",
    "--factor": "The value new streams start with; each stream has its own on the board. 2.5 means chat must be "
                "two and a half times busier than usual. Lower gives more clips.",
    "--bucket": "Chat is counted in slices of this many seconds. Shorter slices react faster but are jumpier. "
                "10 suits most channels.",
    "--window": "The usual chat rate is the median of this many seconds of history.",
    "--warmup": "Seconds of chat to watch before the first clip can be made. Shorter starts sooner, "
                "but the first comparisons are rougher.",
    "--before": "Seconds of video kept from before the spike was noticed. Chat reacts a few seconds late, "
                "so the moment itself sits in this part.",
    "--after": "Seconds kept after the spike was noticed, for the reaction. The clip is cut once they have passed.",
    "--cooldown": "After a clip, new spikes on that stream are ignored for this long, so one long moment does not "
                  "turn into several overlapping clips. Leave empty to use the clip length.",
    "--ratio": "Shape of the finished clip. 9:16 is upright for Shorts, Reels and TikTok, 1:1 is square, "
               "16:9 is normal widescreen.",
    "--height": "Height of the finished clip in pixels. 1920 with 9:16 gives the standard 1080 x 1920. "
                "A smaller number makes smaller files.",
    "--crf": "Trade between picture quality and file size. 18 is close to the original, 23 is the usual balance, "
             "28 is small and soft.",
    "--cam": "Where the streamer's facecam goes when it is a small box on top of a game: its own panel on top "
             "or at the bottom. Off keeps a single picture, cropped from the middle, or around the streamer "
             "when their face fills the frame.",
    "--face-share": "How tall the cam panel is, as a part of the whole clip. 0.33 gives it a third.",
    "--face-zoom": "How tightly the cam panel is cropped around the face. 4 shows about four face widths. "
                   "A smaller number zooms in closer.",
    "--big-face": "When the streamer's face is wider than this part of the picture, they are the whole show, "
                  "as in a talking stream, so the clip follows the face and no cam panel is made.",
    "--model": "The speech-to-text model that writes the subtitles. It runs on this machine. tiny is fastest, "
               "base is the usual choice, small and medium are more accurate and much slower.",
    "--language": "Language the streamer speaks, as a two-letter code such as en, de or es. "
                  "Leave empty to have it detected for each clip.",
    "--jobs": "How many clips are encoded and transcribed at the same time, counted over all streams. "
              "1 keeps the machine responsive. Raise it when clips pile up and the processor has room.",
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
            and a.option_strings[0] not in ON_THE_PAGE
        ]
        if items:
            groups.append({"title": group.title, "options": items})
    return groups


FLAGS = {o["flag"] for g in options() for o in g["options"]}


def number(value, low, high, whole=False):
    """value as a number from low to high, or None."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not low <= n <= high or whole and n != int(n):
        return None
    return int(n) if whole else n


class Stream:
    """One row of the board: a channel, its own spike settings, and the watcher.py process following it."""

    def __init__(self, channel, kick, minimum, factor):
        self.id = f"{'kick' if kick else 'twitch'}-{channel}"
        self.channel, self.kick, self.min, self.factor = channel, kick, minimum, factor
        self.proc, self.stopping, self.forced, self.exit = None, False, False, None
        self.stats, self.history, self.clips, self.last = None, [], 0, ""

    def public(self):
        return {
            "id": self.id, "channel": self.channel, "kick": self.kick, "min": self.min, "factor": self.factor,
            "running": self.proc is not None, "stopping": self.stopping, "exit": self.exit,
            "stats": self.stats, "history": self.history, "clips": self.clips, "last": self.last,
        }

    def tell(self, line):
        """Send watcher.py a command; False when it is no longer listening."""
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False


class Board:
    """Every stream on the page, and the log they share."""

    def __init__(self):
        self.lock = threading.Lock()
        self.streams, self.shared, self.lines, self.first = {}, [], [], 0
        self.topped = {}  # site -> (when asked, its most watched categories)
        try:
            saved = json.loads(SAVED.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = None  # no file yet, or an unreadable one: an empty board
        # the settings and the rows are read apart, so damage to one does not cost the other
        shared, rows = (saved.get("shared"), saved.get("streams")) if isinstance(saved, dict) else (None, None)
        if isinstance(shared, list):
            self.shared = [a for a in shared if isinstance(a, str) and a.partition("=")[0] in FLAGS]
        for row in rows if isinstance(rows, list) else []:
            try:  # one damaged row must not cost the rows after it
                self.add(f"{'kick.com' if row['kick'] else 'twitch.tv'}/{row['channel']}", row["min"], row["factor"],
                         keep=False)
            except (KeyError, TypeError):
                pass

    def save(self):
        streams = [{"channel": s.channel, "kick": s.kick, "min": s.min, "factor": s.factor} for s in self.streams.values()]
        fresh = SAVED.with_name(SAVED.name + ".tmp")
        try:  # written beside the file and swapped in, so a kill half way never leaves a broken board
            fresh.write_text(json.dumps({"shared": self.shared, "streams": streams}, indent=1), encoding="utf-8")
            os.replace(fresh, SAVED)
        except OSError:
            pass  # the board still works; it is just not remembered this time

    def remember(self, shared):
        """Keep the settings every stream shares; they are used by a stream from its next start."""
        with self.lock:
            self.shared = list(shared)
            self.save()

    def categories(self, site):
        """The site's most watched categories right now; the site is asked at most every five minutes."""
        asked, names = self.topped.get(site, (0, []))
        if time.time() - asked > 300:
            names = live.categories(site)
            self.topped[site] = (time.time(), names)
        return names

    def say(self, line):
        self.lines.append(line)
        extra = len(self.lines) - KEEP
        if extra > 0:
            del self.lines[:extra]
            self.first += extra

    def add(self, text, minimum, factor, kick=False, keep=True):
        """Put a channel on the board. Returns what is wrong with the request, or None.

        A link names its site; only a plain name follows `kick`.
        """
        parsed = watcher.parse_channel(str(text))
        minimum, factor = number(minimum, 1, 1_000_000, whole=True), number(factor, 0.01, 1000)
        if not parsed:
            return f"'{text}' is not a channel name or a twitch.tv / kick.com link"
        if minimum is None or factor is None:
            return "The minimum must be a whole number from 1 and the multiplier a number above zero"
        name, site = parsed
        stream = Stream(name, site == "kick" if site else bool(kick), minimum, factor)
        with self.lock:
            if stream.id in self.streams:
                return f"{stream.channel} is already on the board"
            if len(self.streams) >= LIMIT:
                return f"The board holds {LIMIT} streams at most"
            self.streams[stream.id] = stream
            if keep:
                self.save()

    def remove(self, stream_id):
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                return "That stream is not on the board"
            if stream.proc:
                return "Stop the stream before removing it"
            del self.streams[stream_id]
            self.save()

    def start(self, stream_id, shared):
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                return "That stream is not on the board"
            if stream.proc:
                return None
            if shared is not None:
                self.shared = list(shared)
                self.save()
            link = f"{'kick.com' if stream.kick else 'twitch.tv'}/{stream.channel}"
            stream.stopping, stream.forced, stream.exit, stream.stats, stream.history = False, False, None, None, []
            # started apart from our terminal: a Ctrl+C or a closed window here must not hit it as a signal;
            # it is stopped through its stdin alone, and losing that stdin stops it cleanly
            stream.proc = subprocess.Popen(
                [sys.executable, "-u", str(HERE / "watcher.py"), "--stdin-control", link,
                 f"--min={stream.min}", f"--factor={stream.factor:g}",
                 *(a for a in self.shared if a.partition("=")[0] not in PER_STREAM)],
                cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, errors="replace", **watcher.APART,
            )
            threading.Thread(target=self.pump, args=(stream, stream.proc), daemon=True).start()

    def pump(self, stream, proc):
        for line in proc.stdout:
            line = line.rstrip()
            stat = STAT.match(line)
            with self.lock:
                if stat:
                    elapsed, msgs, phase, median, threshold, video = stat.groups()
                    threshold = int(threshold) if threshold else None
                    stream.stats = {"elapsed": int(elapsed), "msgs": int(msgs), "phase": phase, "threshold": threshold,
                                    "median": float(median) if median else None, "video": video == "recording"}
                    stream.history = (stream.history + [[int(msgs), threshold]])[-HISTORY:]
                elif line:
                    clip = CLIP.search(line)
                    if clip:
                        stream.clips, stream.last = stream.clips + 1, Path(clip[1].replace("\\", "/")).name
                    self.say(line)
        code = proc.wait()
        with self.lock:
            stream.proc, stream.stopping, stream.exit = None, False, 0 if stream.forced else code

    def stop(self, stream_id=None, force=False, lost=False):
        """Ask one stream, or all of them, to stop. watcher.py finishes the clips it has cut, then exits.

        force: quit that stream at once and drop those clips; only ever asked for one row by name.
        lost: we are going away ourselves, so the streams are cut loose and wind down on their own.
        """
        with self.lock:
            for stream in self.streams.values():
                if not stream.proc or stream_id not in (None, stream.id):
                    continue
                if lost:
                    stream.proc.stdin.close()
                elif force and stream_id:
                    stream.forced = True  # its exit code will say "killed"; that was asked for, not an error
                    stream.tell("quit")
                    threading.Thread(target=self.end, args=(stream.proc,), daemon=True).start()
                elif not stream.stopping:
                    stream.stopping = stream.tell("stop")

    @staticmethod
    def end(proc):
        """Make sure a stream that was told to quit is gone, even if it could not hear us."""
        for last_resort in (proc.terminate, lambda: watcher.kill_tree(proc)):
            try:
                proc.wait(4)
                return
            except subprocess.TimeoutExpired:
                last_resort()  # a stopping watcher takes a terminate as its second signal and quits with its helpers

    def tune(self, stream_id, minimum, factor):
        """Change a stream's own spike settings. A running stream uses them from its next count."""
        minimum, factor = number(minimum, 1, 1_000_000, whole=True), number(factor, 0.01, 1000)
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream or minimum is None or factor is None:
                return "The minimum must be a whole number from 1 and the multiplier a number above zero"
            stream.min, stream.factor = minimum, factor
            self.save()
            if stream.proc:
                stream.tell(f"min={minimum}")
                stream.tell(f"factor={factor:g}")

    def state(self, since):
        with self.lock:
            return {
                "streams": [s.public() for s in self.streams.values()],
                "lines": self.lines[max(since - self.first, 0):],
                "next": self.first + len(self.lines),
            }


def make_server(port=0):
    """HTTP server on 127.0.0.1 plus the secret its page must send with every request."""
    token, board = secrets.token_urlsafe(16), Board()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, kind="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")  # no other site may show this page inside its own
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
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
            try:
                self.show()
            except Exception as e:  # the page must get an answer it can show, not a dropped connection
                self.reply(500, {"error": f"clip-watcher could not do that: {e}"})

        def show(self):
            path, _, query = self.path.partition("?")
            if path == "/":
                if not self.local():
                    return self.reply(403, {"error": "Not found."})
                page = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", token)
                self.reply(200, page.encode(), "text/html; charset=utf-8")
            elif not self.allowed():
                return
            elif path == "/api/options":
                self.reply(200, {"groups": options(), "shared": board.shared, "board": True})
            elif path == "/api/state":
                since = parse_qs(query).get("since", ["0"])[0]
                self.reply(200, board.state(int(since) if since.isdigit() else 0))
            elif path == "/api/categories":
                site = "kick" if parse_qs(query).get("site", [""])[0] == "kick" else "twitch"
                try:
                    self.reply(200, {"site": site, "categories": board.categories(site)})
                except live.Problem as e:
                    self.reply(400, {"error": str(e)})
            else:
                self.reply(404, {"error": "Not found."})

        def do_POST(self):
            if not self.allowed():
                return
            try:
                self.act()
            except Exception as e:  # the page must get an answer it can show, not a dropped connection
                self.reply(500, {"error": f"clip-watcher could not do that: {e}"})

        def act(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 65536:
                    return self.reply(413, {"error": "Request too large."})
                body = json.loads(self.rfile.read(length) or b"{}")
                stream_id, shared = str(body.get("id", "")), body.get("args")
            except (ValueError, AttributeError, TypeError):
                return self.reply(400, {"error": "Bad request."})
            if shared is not None and not (isinstance(shared, list) and all(
                    isinstance(a, str) and a.partition("=")[0] in FLAGS for a in shared)):
                return self.reply(400, {"error": "Unknown setting."})
            error = None
            if self.path == "/api/add":
                names = [text for text in re.split(r"[\s,]+", str(body.get("channels", ""))) if text]
                problems = [board.add(text, body.get("min"), body.get("factor"), body.get("kick")) for text in names]
                # every name is tried; one that is already there does not hold back the others
                real = [m for m in problems if m and "already on the board" not in m]
                error = real[0] if real else problems[0] if len(names) == 1 else None if names else "Enter a channel name or link"
            elif self.path == "/api/remove":
                error = board.remove(stream_id)
            elif self.path == "/api/start":
                error = board.start(stream_id, shared)
            elif self.path == "/api/start-all":
                for known in list(board.streams):
                    error = error or board.start(known, shared)
            elif self.path == "/api/stop":
                board.stop(stream_id, force=body.get("force") is True)
            elif self.path == "/api/stop-all":
                board.stop()
            elif self.path == "/api/tune":
                error = board.tune(stream_id, body.get("min"), body.get("factor"))
            elif self.path == "/api/shared":
                board.remember(shared or [])
            elif self.path == "/api/find":
                site, top = "kick" if body.get("site") == "kick" else "twitch", number(body.get("count"), 1, LIMIT, whole=True)
                if top is None:
                    return self.reply(400, {"error": f"Ask for 1 to {LIMIT} channels; the board holds {LIMIT} streams"})
                try:
                    name, found = live.live(str(body.get("category", "")), site, top)
                except live.Problem as e:
                    return self.reply(400, {"error": str(e)})
                rows = [{**c, "link": f"{'kick.com' if site == 'kick' else 'twitch.tv'}/{c['channel']}",
                         "id": f"{site}-{c['channel'].lower()}"} for c in found if watcher.parse_channel(c["channel"])]
                return self.reply(200, {"category": name, "site": site, "streams": rows})
            elif self.path == "/api/open-clips":
                folder = HERE / "clips"
                folder.mkdir(exist_ok=True)
                if sys.platform == "win32":
                    os.startfile(folder)
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(folder)])
            else:
                return self.reply(404, {"error": "Not found."})
            self.reply(400 if error else 200, {"error": error} if error else {})

    class Server(ThreadingHTTPServer):
        # on Windows address reuse would let a second clip-watcher bind the same port and steal requests
        allow_reuse_address = sys.platform != "win32"

    httpd = Server(("127.0.0.1", port), Handler)
    httpd.board = board
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
    print(f"clip-watcher streams board: {url}\nKeep this window open. Ctrl+C quits.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.board.stop(lost=True)
        httpd.server_close()


if __name__ == "__main__":
    main()
