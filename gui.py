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
import upload
import watcher

HERE = Path(__file__).parent
PAGE = HERE / "gui.html"
SAVED = HERE / "streams.json"
CLIPS = HERE / "clips"  # where watcher.py saves clips; clips.json there remembers what was reviewed
CLIP_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\.mp4")


def clip_path(name):
    """Where a clip is: in clips/, or in clips/uploaded/ once it went out. None for anything else."""
    if not CLIP_NAME.fullmatch(str(name)):
        return None
    return next((p for p in (CLIPS / name, CLIPS / "uploaded" / name) if p.is_file()), None)
PORT = 8791  # gui.html looks for the helper here when it is opened straight from disk
KEEP = 2000  # log lines held for the page
HISTORY = 60  # chat counts kept per stream for its chart
LIMIT = 16  # streams on the board at most
# a row carries its own minimum, multiplier and site; in the shared settings these only preset new rows
PER_STREAM = ("--min", "--factor", "--kick")
ON_THE_PAGE = ("--category", "--top", "--cam-box", "--game-box", "--cam-share")  # finder and cam editor
# the two lines of watcher.py the board reads; the first feeds the row's numbers and is kept out of the log
STAT = re.compile(r"\[[^\]]+\] (\d+)s \| Msgs: (\d+) \| "
                  r"(?:(warming up|chat not counted)|Median: ([\d.]+) \| Threshold: (\d+)) \| (recording|no video)$")
CLIP = re.compile(r"\] saved (\S+\.mp4)")
FRAME = re.compile(r"\[[^\]]+\] frame (.+)$")  # a still for the cam editor, or "none"; kept out of the log

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
    "--title": ("Title template", ""), "--description": ("Description template", ""), "--hashtags": ("Hashtags", ""),
    "--captions": ("Captions", ""), "--caption-size": ("Caption size", "%"), "--hook": ("Title line", ""),
    "--hook-secs": ("Title line shown for", "s"), "--zoom": ("Zoom at the spike", ""),
    "--tag": ("Channel tag", ""), "--logo": ("Logo image", ""), "--intro": ("Intro video", ""),
    "--outro": ("Outro video", ""), "--teaser": ("Teaser at the start", "s"), "--credit": ("Streamer credit", ""), "--chat-box": ("Chat box", ""),
    "--chat-lines": ("Chat lines shown", ""),
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
    "--face-share": "How tall the cam panel is, as a part of the whole clip, when the face is found on its own. "
                    "0.33 gives it a third. A stream with a cam box set in its Cam & shape editor uses that instead.",
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
    "--upload": "Used when clip-watcher runs in a terminal: sends every finished clip to your YouTube channel "
                "without review. The board never does that: there every clip waits under Clips to review until "
                "you watch it and press Upload. Needs the one-time YouTube setup described in the README.",
    "--privacy": "Who can see uploaded clips. Private is only you, unlisted is anyone with the link, "
                 "public is everyone.",
    "--title": "Title given to each upload. {text} becomes the first words spoken in the clip "
               "and {channel} the streamer's name.",
    "--hashtags": "Hashtags put at the end of every description. {channel} becomes the streamer's name, {game} what they "
                  "were playing when the clip was cut (GTA VI becomes #GTA6), {site} twitch or kick; #Shorts is always added. Keep them few and true to the clip: YouTube ignores a video's hashtags "
                  "when it has more than 60, and misleading ones can get it removed.",
    "--description": "Description given to each upload, with the same {text} and {channel} placeholders. "
                     "A credit link to the stream is added for you.",
    "--captions": "Burn puts what is said into the picture, a few words at a time, the way Shorts viewers expect. "
                  "File only saves the words as an .srt next to the clip. Off skips the speech step.",
    "--caption-size": "Height of the burned-in words as a percent of the clip height. 6 is easy to read on a phone.",
    "--hook": "A line of text at the top of the clip for its first seconds. {text} becomes the first words spoken, "
              "{channel} the streamer's name. Write your own words here to make every clip open with them. "
              "Empty means no title line.",
    "--hook-secs": "How many seconds the title line stays on screen.",
    "--zoom": "How far the picture pushes in on the moment chat exploded, plus a slow push-in across the clip, "
              "so the clip is not one still crop. 0.12 is a clear punch, 0 turns it off. The cam panel stays still.",
    "--tag": "Your channel name or handle, shown small in a bottom corner of every clip, for example @mychannel.",
    "--logo": "Path of an image, best a PNG with a clear background, shown in the bottom right corner.",
    "--intro": "Path of a short video put in front of every clip. Keep it under 2 seconds: Shorts viewers "
               "swipe away from slow starts.",
    "--outro": "Path of a short video put after every clip, for example asking viewers to follow.",
    "--credit": "Names the streamer on screen in the bottom right of every clip, for example twitch.tv/ishowspeed. "
                "{source} is the stream link, {channel} the name. Credit is good manners and YouTube viewers expect it, "
                "but it is not permission: the streamer can still claim or take down a clip.",
    "--teaser": "A few seconds of the best moment, from just before chat exploded, shown first as the hook, "
                "before your intro; then the whole clip plays. 2.5 is a quick cold open, 0 turns it off.",
    "--chat-box": "Shows the real chat of the moment in a box on the left or right of the clip, scrolling the way "
                  "it came in, so viewers see chat explode. Off leaves it out.",
    "--chat-lines": "How many chat lines the box holds at once. More lines make a taller box.",
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


def shape_values(cam, ratio, game=None, share=None):
    """Areas ([x, y, w, h] shares of the frame, or None), a shape ("9:16", or None) and a cam panel height, checked."""
    def area(value):
        if value is None:
            return None
        if not (isinstance(value, list) and len(value) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)):
            raise ValueError("area")
        value = [round(float(v), 4) for v in value]
        try:
            watcher.box(",".join(f"{v:g}" for v in value))
        except argparse.ArgumentTypeError:
            raise ValueError("area") from None
        return value

    cam, game = area(cam), area(game)
    if share is not None:
        try:
            share = watcher.share(str(float(share)))
        except (argparse.ArgumentTypeError, ValueError):
            raise ValueError("share") from None
    if ratio is not None:
        try:
            watcher.ratio(str(ratio))
        except argparse.ArgumentTypeError:
            raise ValueError("ratio") from None
        ratio = str(ratio)
    return cam, ratio, game, share


def clip_source(name):
    """The channel a clip came from, and its stream link, read from the clip's file name."""
    m = re.match(r"(.+?)(-kick)?_\d{8}_\d{6}\.mp4$", name)
    channel = m[1] if m else name.rsplit(".", 1)[0]
    return channel, f"{'kick.com' if m and m[2] else 'twitch.tv'}/{channel}"


class Stream:
    """One row of the board: a channel, its own spike settings, and the watcher.py process following it."""

    def __init__(self, channel, kick, minimum, factor):
        self.id = f"{'kick' if kick else 'twitch'}-{channel}"
        self.channel, self.kick, self.min, self.factor = channel, kick, minimum, factor
        self.proc, self.stopping, self.forced, self.exit = None, False, False, None
        self.stats, self.history, self.clips, self.last = None, [], 0, ""
        # set by hand in the cam editor; None means face detection, the middle, the box's shape, the shared shape
        self.cam, self.game, self.share, self.ratio = None, None, None, None
        self.frame, self.frames = None, 0  # newest still from watcher.py, and how many answers came

    def public(self):
        return {
            "id": self.id, "channel": self.channel, "kick": self.kick, "min": self.min, "factor": self.factor,
            "running": self.proc is not None, "stopping": self.stopping, "exit": self.exit,
            "stats": self.stats, "history": self.history, "clips": self.clips, "last": self.last,
            "cam": self.cam, "game": self.game, "share": self.share, "ratio": self.ratio,
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
            self.reviewed = json.loads((CLIPS / "clips.json").read_text(encoding="utf-8"))
            assert isinstance(self.reviewed, dict)
        except (OSError, ValueError, AssertionError):
            self.reviewed = {}  # clip name -> {"state": "uploading" / "uploaded" / "failed", ...}
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
                if not self.add(f"{'kick.com' if row['kick'] else 'twitch.tv'}/{row['channel']}", row["min"],
                                row["factor"], keep=False):
                    stream = list(self.streams.values())[-1]
                    stream.cam, stream.ratio, stream.game, stream.share = shape_values(
                        row.get("cam"), row.get("ratio"), row.get("game"), row.get("share"))
            except (KeyError, TypeError, ValueError, AttributeError):
                pass

    def save(self):
        streams = [{"channel": s.channel, "kick": s.kick, "min": s.min, "factor": s.factor, "cam": s.cam, "game": s.game,
                    "share": s.share, "ratio": s.ratio}
                   for s in self.streams.values()]
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
            own = [f"--min={stream.min}", f"--factor={stream.factor:g}"]
            own += [f"--cam-box={','.join(f'{v:g}' for v in stream.cam)}"] if stream.cam else []
            own += [f"--game-box={','.join(f'{v:g}' for v in stream.game)}"] if stream.game else []
            own += [f"--cam-share={stream.share:g}"] if stream.share else []
            own += [f"--ratio={stream.ratio}"] if stream.ratio else []
            stream.proc = subprocess.Popen(
                [sys.executable, "-u", str(HERE / "watcher.py"), "--stdin-control", link, *own,
                 *(a for a in self.shared if a.partition("=")[0] not in PER_STREAM
                   and not (stream.ratio and a.startswith("--ratio=")) and a != "--upload")],  # uploads: reviewed
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
                elif FRAME.match(line):
                    path = FRAME.match(line)[1]
                    stream.frame, stream.frames = None if path == "none" else path, stream.frames + 1
                elif line:
                    clip = CLIP.search(line)
                    if clip:
                        stream.clips, stream.last = stream.clips + 1, Path(clip[1].replace("\\", "/")).name
                        threading.Thread(target=self.first_thumbnail, args=(stream.last,), daemon=True).start()
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

    def shape(self, stream_id, cam, ratio, game=None, share=None):
        """Set a stream's cam area, game area, cam panel height and clip shape by hand.

        A running stream uses them from its next clip.
        """
        try:
            cam, ratio, game, share = shape_values(cam, ratio, game, share)
        except (ValueError, TypeError):
            return "The areas must lie inside the picture, the cam panel 0.12 to 0.6 high, and the shape like 9:16"
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                return "That stream is not on the board"
            stream.cam, stream.ratio, stream.game, stream.share = cam, ratio, game, share
            self.save()
            if stream.proc:
                shared = next((a.partition("=")[2] for a in self.shared if a.startswith("--ratio=")), "9:16")
                stream.tell("cam=" + (",".join(f"{v:g}" for v in cam) if cam else "auto"))
                stream.tell("game=" + (",".join(f"{v:g}" for v in game) if game else "auto"))
                stream.tell(f"share={share:g}" if share else "share=auto")
                stream.tell(f"ratio={ratio or shared}")

    @staticmethod
    def peek(stream):
        """A still of a stream that is not being recorded: a few seconds of it are fetched just for this."""
        link = f"https://{'kick.com' if stream.kick else 'twitch.tv'}/{stream.channel}"
        out = HERE / "rec" / f"peek-{stream.id}.jpg"
        out.parent.mkdir(exist_ok=True)
        out.unlink(missing_ok=True)
        source = subprocess.Popen(
            [sys.executable, "-m", "streamlink", "--loglevel", "error", "-O", link, "720p,720p60,480p,best"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **watcher.APART)
        grab = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", "-", "-frames:v", "1", "-vf", "scale=-2:540",
             "-q:v", "4", str(out)], stdin=source.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **watcher.APART)
        source.stdout.close()
        try:
            grab.wait(45)  # Twitch can hold a viewer in an ad break before the first picture
        except subprocess.TimeoutExpired:
            pass
        finally:
            for proc in (grab, source):
                if proc.poll() is None:
                    watcher.kill_tree(proc)
        said = source.stderr.read().decode(errors="replace")
        if out.exists() and out.stat().st_size:
            return out.read_bytes(), None
        if "No playable streams" in said or "offline" in said.lower():
            return None, "Not live right now, so there is no picture. The settings can still be made."
        return None, "No picture came; try again in a moment"

    def frame(self, stream_id):
        """A fresh still of what the stream records, as JPEG bytes, or the reason there is none."""
        with self.lock:
            stream = self.streams.get(stream_id)
            if not stream:
                return None, "That stream is not on the board"
            running, asked = stream.proc is not None, stream.frames
            if running:
                stream.tell("frame")
        if not running:
            return self.peek(stream)
        deadline = time.time() + 12
        while time.time() < deadline:
            with self.lock:
                if stream.frames != asked:
                    path = stream.frame
                    break
            time.sleep(0.1)
        else:
            return None, "No picture came; try again in a moment"
        if not path:
            return None, "Nothing recorded yet; try again once the row says recording"
        picture = (HERE / path).resolve()
        if picture.suffix.lower() != ".jpg" or (HERE / "rec").resolve() not in picture.parents:
            return None, "No picture"
        try:
            return picture.read_bytes(), None
        except OSError:
            return None, "No picture came; try again in a moment"

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

    def setting(self, flag, default):
        return next((a.partition("=")[2] for a in self.shared if a.startswith(flag + "=")), default)

    def clips(self):
        """Clips on disk with what reviewing needs: the suggested title and description, and their state."""
        out = []
        with self.lock:
            reviewed = dict(self.reviewed)
        def newest(folder, n):
            return sorted(folder.glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)[:n] if folder.is_dir() else []

        # the newest 60 waiting and the newest 20 uploaded; older ones stay on disk untouched
        for clip in newest(CLIPS, 60) + newest(CLIPS / "uploaded", 20):
            if not CLIP_NAME.fullmatch(clip.name):
                continue
            channel, source = clip_source(clip.name)
            try:
                title, description = upload.describe(upload.transcript(clip.with_suffix(".srt")), channel,
                                                     self.setting("--title", upload.TITLE),
                                                     self.setting("--description", upload.DESCRIPTION))
            except (KeyError, IndexError, ValueError):
                title, description = upload.describe(upload.transcript(clip.with_suffix(".srt")), channel)
            try:
                layout_of = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                layout_of = None  # made before clips said how they are put together: no teaser choice
            out.append({"name": clip.name, "channel": channel, "source": source, "made": int(clip.stat().st_mtime),
                        "parts": layout_of, "thumb": clip.with_suffix(".jpg").is_file(),
                        "tags": upload.hashtags(self.setting("--hashtags", upload.HASHTAGS), source,
                                                (layout_of or {}).get("game", "")),
                        "mb": round(clip.stat().st_size / 1e6, 1), "title": title, "description": description,
                        **reviewed.get(clip.name, {"state": "uploaded" if clip.parent.name == "uploaded" else "waiting"})})
        return {"clips": out, "privacy": self.setting("--privacy", "private")}

    def keep_reviewed(self):
        fresh = CLIPS / "clips.json.tmp"
        try:
            fresh.write_text(json.dumps(self.reviewed, indent=1), encoding="utf-8")
            os.replace(fresh, CLIPS / "clips.json")
        except OSError:
            pass

    def publish(self, name, title, description, privacy, tags=None):
        """Upload one reviewed clip in the background, then keep it in clips/uploaded/; the clip list shows how it went."""
        clip = CLIPS / str(name)
        if not CLIP_NAME.fullmatch(str(name)) or not clip.is_file():
            return "That clip is not there any more"
        if privacy not in ("private", "unlisted", "public"):
            return "Pick who can see it"
        title, description = str(title or "").strip(), str(description or "").strip()
        if not title or len(title) > 100:
            return "The title must have 1 to 100 characters"
        with self.lock:
            if self.reviewed.get(name, {}).get("state") in ("uploading", "uploaded", "editing"):
                return "That clip is uploading, uploaded or being remade"
            self.reviewed[name] = {"state": "uploading", "privacy": privacy}
            self.keep_reviewed()

        def send():
            literal = {ord("{"): "{{", ord("}"): "}}"}  # the text as typed, not a template
            try:
                said = []
                picture = clip.with_suffix(".jpg")
                video = upload.publish(clip, clip_source(name)[1], privacy, title.translate(literal),
                                       description.translate(literal), say=said.append,
                                       tags=str(tags if tags is not None else self.setting("--hashtags", upload.HASHTAGS)),
                                       thumbnail=picture if picture.is_file() else None)
                result = {"state": "uploaded", "privacy": privacy, "id": video}
                note = f"[review] uploaded {name} ({privacy}): https://youtube.com/shorts/{video}"
                note += "".join(f"; {line}" for line in said if line.startswith("thumbnail"))
                # kept apart, with its subtitles and thumbnail, to post on other sites by hand
                (CLIPS / "uploaded").mkdir(exist_ok=True)
                for path in (clip, clip.with_suffix(".srt"), clip.with_suffix(".json"), picture):
                    if path.exists():
                        os.replace(path, CLIPS / "uploaded" / path.name)
            except (Exception, SystemExit) as e:  # a refused upload must reach the page, not end the server
                result = {"state": "failed", "error": str(e)[:300]}
                note = f"[review] upload of {name} failed: {e}"
            with self.lock:
                self.reviewed[name] = result
                self.keep_reviewed()
                self.say(note)

        threading.Thread(target=send, daemon=True).start()

    def first_thumbnail(self, name, keep_own=True):
        """The default thumbnail: the teaser's first frame (or the spike without a teaser), with the first words said.

        keep_own: leave a thumbnail made by hand as it is.
        """
        clip = clip_path(name)
        try:
            parts = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
        except (AttributeError, OSError, ValueError):
            return  # made before clips said how they are put together
        if keep_own and parts.get("own_thumbnail"):
            return
        at = 0.05 if parts.get("teaser") else parts.get("intro", 0) + parts.get("spike", 0)
        said = " ".join(upload.transcript(clip.with_suffix(".srt")).split()[:5])
        self.thumbnail(name, at, said, own=False)

    def thumbnail(self, name, at, text, own=True):
        """Make the clip's thumbnail: its frame at `at` seconds, with big words on it, as <clip>.jpg.

        own: made by hand, so a new teaser does not replace it.
        """
        clip = clip_path(name)
        if not clip:
            return "That clip is not there any more"
        try:
            at = max(0.0, float(at))
        except (TypeError, ValueError):
            return "Pick a moment in the clip"
        words = " ".join(str(text or "").split()).upper()[:60]
        lines, line = [], ""
        for word in words.split():  # two or three short lines read best on a phone
            if line and len(line) + 1 + len(word) > 12:
                lines.append(line)
                line = word
            else:
                line = f"{line} {word}".strip()
        lines += [line] if line else []
        draw = "scale=1080:1920,setsar=1"  # the words sit below the cam panel, so the face stays in view
        cards = []
        # big-thumbnail lettering in the channel's colours: spike red, then light, with a graphite outline
        size = min(250, round(1080 * 1.9 / max([len(x) for x in lines] or [1])))
        face = next((f"fontfile={watcher.quote(f)}" for f in (HERE / "fonts" / "Anton-Regular.ttf",
                                                              Path("C:/Windows/Fonts/impact.ttf")) if f.exists()),
                    "font='DejaVu Sans:style=Bold'")
        if lines:  # the channel's chat meter above the words: grey bars, the last one spiking red
            for i, tall in enumerate((30, 50, 38, 62, 96)):
                x = 451 + i * 38
                draw += (f",drawbox=x={x - 5}:y={690 - tall - 5}:w=36:h={tall + 10}:color=0x141415:t=fill,"
                         f"drawbox=x={x}:y={690 - tall}:w=26:h={tall}:color={'0xE5484D' if i == 4 else '0x85858B'}:t=fill")
        for n, words_on_line in enumerate(lines):
            card = clip.with_suffix(f".thumb{n}.txt")
            card.write_text(words_on_line, encoding="utf-8")
            cards.append(card)
            draw += (f",drawtext=textfile={watcher.quote(card)}:expansion=none:{face}:fontsize={size}:"
                     f"fontcolor={'0xE5484D' if n == 0 else '0xEDEDED'}:borderw={max(6, size // 11)}:bordercolor=0x141415:"
                     f"shadowcolor=0x141415@0.8:shadowx={size // 22}:shadowy={size // 18}:"
                     f"x=(w-text_w)/2:y={712 + n * round(size * 1.08)}")
        try:
            watcher.run("ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{at:.3f}", "-i", clip,
                        "-frames:v", "1", "-vf", draw, "-q:v", "2", clip.with_suffix(".jpg"))
        except RuntimeError as e:
            return f"The thumbnail could not be made: {e}"
        finally:
            for card in cards:
                card.unlink(missing_ok=True)
        try:
            parts = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
            parts["own_thumbnail"] = own
            clip.with_suffix(".json").write_text(json.dumps(parts), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def clear(self):
        """Delete every clip still waiting for review; uploaded ones in clips/uploaded/ stay."""
        gone = 0
        with self.lock:
            for clip in CLIPS.glob("*.mp4") if CLIPS.is_dir() else []:
                if CLIP_NAME.fullmatch(clip.name) and self.reviewed.get(clip.name, {}).get("state") not in (
                        "uploading", "editing"):
                    for path in (clip, clip.with_suffix(".srt"), clip.with_suffix(".json"), clip.with_suffix(".jpg")):
                        path.unlink(missing_ok=True)
                    self.reviewed.pop(clip.name, None)
                    gone += 1
            self.keep_reviewed()
            self.say(f"[review] deleted {gone} waiting clips")

    def reteaser(self, name, start, seconds):
        """Cut the teaser again from another moment of the clip (or drop it), in the background."""
        clip = CLIPS / str(name)
        if not CLIP_NAME.fullmatch(str(name)) or not clip.is_file():
            return "That clip is not there any more"
        try:
            parts = json.loads(clip.with_suffix(".json").read_text(encoding="utf-8"))
            start, seconds = float(start), float(seconds)
        except (OSError, ValueError, TypeError):
            return "This clip was made before teasers could be changed"
        if not (seconds == 0 or 1 <= seconds <= 10) or not 0 <= start <= max(0, parts["main"] - seconds):
            return "The teaser must be 1 to 10 seconds long and lie inside the clip"
        with self.lock:
            if self.reviewed.get(name, {}).get("state") in ("uploading", "uploaded", "editing"):
                return "That clip is uploading, uploaded or being remade"
            self.reviewed[name] = {"state": "editing"}

        def remake():
            main_at = parts["teaser"] + parts["intro"]  # the old teaser and the intro come before the clip itself
            first = f"{main_at + start:.3f}"
            graph = (f"[0:v]trim=start={parts['teaser']:.3f},setpts=PTS-STARTPTS[rv];"
                     f"[0:a]atrim=start={parts['teaser']:.3f},asetpts=PTS-STARTPTS[ra]")
            if seconds:
                graph = (f"[0:v]split[v1][v2];[0:a]asplit[a1][a2];"
                         f"[v1]trim=start={first}:duration={seconds:.3f},setpts=PTS-STARTPTS[tv];"
                         f"[a1]atrim=start={first}:duration={seconds:.3f},asetpts=PTS-STARTPTS[ta];"
                         f"[v2]trim=start={parts['teaser']:.3f},setpts=PTS-STARTPTS[rv];"
                         f"[a2]atrim=start={parts['teaser']:.3f},asetpts=PTS-STARTPTS[ra];"
                         "[tv][ta][rv][ra]concat=n=2:v=1:a=1[v][a]")
            else:
                graph = graph.replace("[rv]", "[v]").replace("[ra]", "[a]")
            fresh = clip.with_name(clip.stem + ".remake.mp4")
            try:
                watcher.run("ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", clip, "-filter_complex", graph,
                            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-crf", "20", "-c:a", "aac", fresh)
                os.replace(fresh, clip)
                parts.update(teaser=round(seconds, 3), teaser_from=round(start, 3))
                clip.with_suffix(".json").write_text(json.dumps(parts), encoding="utf-8")
                self.first_thumbnail(name)  # the default thumbnail follows the new teaser
                note = f"[review] new teaser for {name}" if seconds else f"[review] teaser taken out of {name}"
            except (RuntimeError, OSError) as e:
                fresh.unlink(missing_ok=True)
                note = f"[review] could not remake {name}: {e}"
            with self.lock:
                self.reviewed.pop(name, None)
                self.keep_reviewed()
                self.say(note)

        threading.Thread(target=remake, daemon=True).start()

    def discard(self, name):
        """Delete a clip that should never go out, with its subtitles."""
        clip = CLIPS / str(name)
        if not CLIP_NAME.fullmatch(str(name)) or not clip.is_file():
            return "That clip is not there any more"
        with self.lock:
            if self.reviewed.get(name, {}).get("state") in ("uploading", "editing"):
                return "That clip is uploading or being remade right now"
            for path in (clip, clip.with_suffix(".srt"), clip.with_suffix(".json"), clip.with_suffix(".jpg")):
                path.unlink(missing_ok=True)
            self.reviewed.pop(name, None)
            self.keep_reviewed()
            self.say(f"[review] deleted {name}")

    @staticmethod
    def media(query, headers, body):
        """Keep a file picked on the page (intro, outro or logo) in brand/, and say where it went."""
        kind, name = query.get("kind", [""])[0], query.get("name", [""])[0]
        suffix = Path(name).suffix.lower()
        if kind not in ("intro", "outro", "logo") or suffix not in (
                (".png", ".jpg", ".jpeg", ".webp") if kind == "logo" else (".mp4", ".mov", ".mkv", ".webm")):
            return 400, {"error": "Pick a video file (an image for the logo)"}
        try:
            length = int(headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= 500_000_000:
            return 400, {"error": "The file must be under 500 MB"}
        folder = HERE / "brand"
        folder.mkdir(exist_ok=True)
        stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(name).stem).strip("-")[:40] or kind
        target = folder / f"{kind}-{stem}{suffix}"
        with open(target, "wb") as out:
            left = length
            while left:
                chunk = body.read(min(left, 1 << 20))
                if not chunk:
                    break
                out.write(chunk)
                left -= len(chunk)
        if left:
            target.unlink(missing_ok=True)
            return 400, {"error": "The file did not arrive whole; try again"}
        return 200, {"path": f"brand/{target.name}"}

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
            elif path == "/api/clips":
                self.reply(200, board.clips())
            elif path in ("/api/clip", "/api/thumb"):
                clip = clip_path(parse_qs(query).get("name", [""])[0])
                picture = clip.with_suffix(".jpg") if clip and path == "/api/thumb" else clip
                if not picture or not picture.is_file():
                    return self.reply(404, {"error": "That clip is not there any more"})
                self.reply(200, picture.read_bytes(), "video/mp4" if path == "/api/clip" else "image/jpeg")
            elif path == "/api/frame":
                picture, error = board.frame(parse_qs(query).get("id", [""])[0])
                self.reply(200, picture, "image/jpeg") if picture else self.reply(409, {"error": error})
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
            if self.path.startswith("/api/media?"):
                return self.reply(*board.media(parse_qs(self.path.partition("?")[2]), self.headers, self.rfile))
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
            elif self.path == "/api/shape":
                error = board.shape(stream_id, body.get("cam"), body.get("ratio"), body.get("game"), body.get("share"))
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
            elif self.path == "/api/review/upload":
                error = board.publish(body.get("name"), body.get("title"), body.get("description"), body.get("privacy"),
                                      body.get("tags"))
            elif self.path == "/api/review/thumbnail":
                error = board.thumbnail(body.get("name"), body.get("at"), body.get("text"))
            elif self.path == "/api/review/clear":
                board.clear()
            elif self.path == "/api/review/teaser":
                error = board.reteaser(body.get("name"), body.get("start"), body.get("seconds"))
            elif self.path == "/api/review/delete":
                error = board.discard(body.get("name"))
            elif self.path == "/api/open-clips":
                folder = CLIPS / "uploaded" if body.get("folder") == "uploaded" else CLIPS
                folder.mkdir(parents=True, exist_ok=True)
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
