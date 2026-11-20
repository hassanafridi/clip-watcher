import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import gui
import live
import upload
import watcher
from upload import describe, transcript
from watcher import Recorder, is_spike, layout, lock_file, parse_channel, parse_target, ratio, steady_face

BIG_FACE = 0.12
said = []
watcher.log = lambda tag, text: said.append(text)
watcher.kill_tree = lambda proc: proc.terminate()


class Clock:
    """Stands in for the time module: sleeping only moves the hands."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(time, name)


def serve(handler):
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


# --- spike rule ---
quiet = [4] * 18
assert is_spike(10, quiet, 10, 2.5)
assert not is_spike(9, quiet, 10, 2.5), "below --min"
assert not is_spike(20, [10] * 18, 5, 2.5), "under 2.5x the median"
assert is_spike(20, [10] * 18, 5, 2), "--factor lowers the bar"
assert is_spike(5, [0] * 18, 5, 2.5), "dead chat: --min alone decides"

# --- face grouping and clip layout ---
assert steady_face([], 30) is None
assert steady_face([(100, 100, 50, 50)] * 5, 30) is None, "seen in too few frames"
face = steady_face([(100, 100, 50, 50)] * 20 + [(900, 500, 40, 40)] * 3, 30)
assert face == (100, 100, 50, 50), face

assert ratio("9:16") == 9 / 16 and ratio("1:1") == 1
for bad in ("9x16", "0:16", "16"):
    try:
        ratio(bad)
        raise AssertionError(bad)
    except Exception as e:
        assert type(e).__name__ == "ArgumentTypeError", (bad, e)

# (out_w, out_h, face_h, may stack)
outputs = [(1080, 1920, 640, True), (720, 720, 240, True), (1920, 1080, 360, False), (1080, 1920, 0, False),
           (608, 1080, 540, True), (2520, 1080, 360, False)]
for w, h in [(1920, 1080), (1280, 720), (852, 480)]:
    small, big = (w // 20, h // 10, w // 20, w // 20), (w // 2, h // 4, int(w * BIG_FACE) + 2, w // 6)
    corner = (w - 30, h - 30, 28, 28)
    for out_w, out_h, face_h, may_stack in outputs:
        o = SimpleNamespace(width=out_w, height=out_h, face_h=face_h, face_zoom=4, big_face=BIG_FACE, cam="top")
        for face in (None, small, big, corner):
            graph = layout(w, h, face, o)
            crops = [tuple(map(int, m)) for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", graph)]
            for cw, ch, cx, cy in crops:
                assert cw > 0 and ch > 0 and cw % 2 == 0 and ch % 2 == 0, graph
                assert cx + cw <= w and cy + ch <= h, (w, h, face, graph)
            stacked = "vstack" in graph
            assert stacked == (may_stack and face in (small, corner)), (out_w, out_h, face, graph)
            if stacked:
                assert f"scale={out_w}:{face_h}" in graph and f"scale={out_w}:{out_h - face_h}" in graph
            else:
                assert len(crops) == 1 and f"scale={out_w}:{out_h}" in graph

o = SimpleNamespace(width=1080, height=1920, face_h=640, face_zoom=4, big_face=BIG_FACE, cam="bottom")
assert "[game][cam]vstack" in layout(1920, 1080, (96, 108, 96, 96), o)
o.cam = "top"
assert "[cam][game]vstack" in layout(1920, 1080, (96, 108, 96, 96), o)

# a cam area set by hand: always its own panel when the shape allows one, holding the whole area
for bx, by, bw, bh in ((1500, 50, 360, 220), (0, 0, 200, 600), (1700, 900, 220, 180)):
    graph = layout(1920, 1080, None, o, box=(bx, by, bw, bh))
    cw, ch, cx, cy = map(int, re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", graph)[0])
    assert "vstack" in graph and cx <= bx + 1 and cy <= by + 1 and cx + cw >= min(bx + bw, 1920) - 2, graph
    assert cy + ch >= min(by + bh, 1080) - 2 and cx + cw <= 1920 and cy + ch <= 1080, graph
    assert abs(cw / ch - 1080 / 640) < 0.02, "the cam crop has the panel's shape"
wide = SimpleNamespace(width=1920, height=1080, face_h=360, face_zoom=4, big_face=BIG_FACE, cam="top")
assert "vstack" not in layout(1920, 1080, None, wide, box=(1500, 50, 360, 220)), "a wide clip has no panel"
assert "zoompan=" in layout(1920, 1080, None, o, "[base]", lambda w, h: f",zoompan=s={w}x{h}", (1500, 50, 360, 220))
assert ",zoompan=s=1080x1280[game]" in layout(1920, 1080, None, o, "[base]", lambda w, h: f",zoompan=s={w}x{h}",
                                              (1500, 50, 360, 220)), "only the game moves; the cam stays still"
for good, want in (("0.7,0.05,0.25,0.3", (0.7, 0.05, 0.25, 0.3)), ("auto", None), ("", None)):
    assert watcher.box(good) == want
for bad in ("1,2,3", "0.9,0,0.2,0.2", "-0.1,0,0.2,0.2", "a,b,c,d", "0,0,0,0.5"):
    try:
        watcher.box(bad)
        raise AssertionError(bad)
    except Exception as e:
        assert type(e).__name__ == "ArgumentTypeError", (bad, e)
assert watcher.opening("one two three four five six seven eight") == "one two three four five six"
# the game area set by hand: the game panel, or the whole clip when there is no panel, is cut around it
graph = layout(1920, 1080, None, o, box=(1500, 50, 360, 220), game=(0, 0, 600, 1080))
assert re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", graph)[1][2] == "0", graph
graph = layout(1920, 1080, None, wide, game=(1200, 500, 640, 360))
cw, ch, cx, cy = map(int, re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", graph)[0])
assert (cw, ch) == (640, 360) and (cx, cy) == (1200, 500), graph
# the cam panel takes the cam box's shape unless its height is set by hand, and stays between 12% and 60%
edits = SimpleNamespace(width=1080, height=1920, face_h=640, face_zoom=4, big_face=BIG_FACE, cam="top", ratio=9 / 16,
                        cam_box=(0.7, 0.05, 0.25, 0.25), game_box=None, cam_share=None, zoom=0, caption_size=6, hook_secs=3,
                        teaser=0)
assert "scale=1080:606[cam]" in watcher.edit(1920, 1080, None, edits, 0, 30, 20, "60/1", True), "a 16:9 cam gets a 16:9 panel"
edits.cam_share = 0.25
assert "scale=1080:480[cam]" in watcher.edit(1920, 1080, None, edits, 0, 30, 20, "60/1", True)
edits.cam_share, edits.cam_box = None, (0.7, 0.05, 0.05, 0.6)
assert "scale=1080:1152[cam]" in watcher.edit(1920, 1080, None, edits, 0, 30, 20, "60/1", True), "at most 60%"
for good, want in (("0.25", 0.25), ("auto", None)):
    assert watcher.share(good) == want
for bad in ("0.05", "0.9", "half"):
    try:
        watcher.share(bad)
        raise AssertionError(bad)
    except Exception as e:
        assert type(e).__name__ == "ArgumentTypeError", (bad, e)
assert watcher.quote(r"C:\Users\a b\x.srt") == "'C\\:/Users/a b/x.srt'"

# --- upload text ---
assert transcript(Path("missing.srt")) == ""
assert describe("", "somechannel") == ("live moment | somechannel", "")
assert describe("that was <insane>", "abc") == ("that was insane | abc", "that was insane")
long = ("word " * 40).strip()
title, description = describe(long, "abc", "{channel}: {text}", "{text} #clips")
assert title == "abc: " + ("word " * 12).strip() and len(title) <= 100, title
assert description == long + " #clips"

# --- channels on the command line: a link names its site, a plain name does not ---
assert parse_channel("OhnePixel") == ("ohnepixel", None)
assert parse_channel("https://www.twitch.tv/ohnepixel") == ("ohnepixel", "twitch")
assert parse_channel("kick.com/xqc/") == ("xqc", "kick")
assert parse_channel("https://evil.example/x") is None and parse_channel("-x y") is None
assert parse_target("xqc:170") == ("xqc", None, 170) and parse_target("xqc") == ("xqc", None, None)
assert parse_target("https://kick.com/xqc:40") == ("xqc", "kick", 40)
assert parse_target("https://www.twitch.tv/dinah") == ("dinah", "twitch", None)
assert parse_target("xqc:lots") is None and parse_target(":5") is None
parsed = watcher.build_parser().parse_intermixed_args(["xqc:170", "--min", "20", "dinah", "--kick"])
assert parsed.channels == ["xqc:170", "dinah"] and parsed.min == 20 and parsed.kick
assert watcher.build_parser().parse_intermixed_args(["--category", "Just Chatting"]).channels == []

# --- one lock, one holder ---
with tempfile.TemporaryDirectory() as folder:
    first = lock_file(Path(folder) / "slot")
    assert first and lock_file(Path(folder) / "slot") is None, "a held lock cannot be taken again"
    first.close()
    again = lock_file(Path(folder) / "slot")
    assert again, "closing gives it back"
    again.close()


# --- the rolling recording, driven by a fake clock and fake recorder programs ---
class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    def wait(self, timeout=None):
        return self.code


def recorder_in(folder, **changed):
    args = SimpleNamespace(before=20, after=10, overlap_secs=60, rotate_mins=60, keep_rec=False, quality="1080p")
    vars(args).update(changed)
    said.clear()
    watcher.REC = watcher.RUN = Path(folder)
    rec = Recorder("chan", "twitch.tv/chan", args, 0)
    rec.spawn = lambda path: (FakeProc(), FakeProc())
    return rec


def grow(seg):
    with open(seg.path, "ab") as f:
        f.write(b"x" * 1000)


def die(seg):
    for proc in seg.procs:
        proc.code = 1


with tempfile.TemporaryDirectory() as folder:
    # start, first video, then a normal rotation with overlap and deletion
    rec = recorder_in(folder)
    rec.tick(1000)
    one = rec.segments[0]
    assert one.path.name.startswith("chan_seg1_") and one.born is None and not rec.recording(1000)
    grow(one)
    rec.tick(1001)
    assert one.born == one.grew == one.since == 1001 and rec.recording(1002) and "recording to" in said[-1]
    for t in range(1002, 1001 + 3600, 5):
        grow(one)
        rec.tick(t)
    assert len(rec.segments) == 1 and one.since == 1001, "no switch before --rotate-mins, and no hole in the video"
    grow(one)
    rec.tick(1001 + 3600)
    assert len(rec.segments) == 2 and "new recording file" in said[-1]
    two = rec.segments[1]
    for t in range(4605, 4700, 5):
        grow(one)
        rec.tick(t)
    assert one.ended is None, "the old one keeps recording while the new one has no video (pre-roll ads)"
    grow(one)
    grow(two)
    rec.tick(4700)  # the new file's first video: the overlap clock starts here
    for t in range(4705, 4725, 5):
        grow(one)
        grow(two)
        rec.tick(t)
    # early in the overlap the new file is too young, so a clip comes from the old one, which holds all of it
    held = rec.acquire(4720 - 36, 4720 - 2)
    assert held[0] is one and held[1:] == (36, 34, 4720 - 36), held
    for t in range(4725, 4760, 5):
        grow(one)
        grow(two)
        rec.tick(t)
    assert one.ended is None and two.born == 4700, "less than the overlap: both still record"
    newest = rec.acquire(4755 - 36, 4755 - 2)
    assert newest[0] is two, "once both hold the whole clip the newer file is used"
    rec.release(two, cut_done=False)
    grow(two)
    rec.tick(4760)
    assert one.ended == 4760 and one.procs[0].code == 0, "after the overlap only the old recorder is stopped"
    assert two.ended is None and two.procs[0].code is None
    die(one)  # its remux reaches the end of its input
    rec.tick(4760 + 60)
    assert one.path.exists(), "a file a clip is still reading is not deleted"
    rec.release(held[0], cut_done=False)
    grow(two)
    rec.tick(4760 + 61)
    assert not one.path.exists() and rec.segments == [two] and "deleted" in said[-1]

    # a clip that is still waiting to cut blocks deletion too
    rec.pending = 1
    two.ended = 4900
    die(two)
    rec.next_try = 1e12
    rec.tick(5000)
    assert two.path.exists()
    rec.release(None)
    rec.tick(5001)
    assert not two.path.exists()

with tempfile.TemporaryDirectory() as folder:
    # a clip on a file younger than the clip takes only what exists, and ends where the clip ends
    rec = recorder_in(folder)
    rec.tick(100)
    seg = rec.segments[0]
    for t in range(110, 135, 5):
        grow(seg)
        rec.tick(t)
    held = rec.acquire(130 - 10 - 34, 130 - 10)
    assert held == (seg, 20, 10, 110), "starts at the file's first video, stops 10 s before its end"
    rec.release(seg, cut_done=False)
    assert rec.acquire(10, 50) is None, "nothing recorded that moment"
    # video that stops arriving is not mistaken for the present
    rec.tick(400)
    assert not rec.recording(400) and rec.acquire(400 - 36, 400 - 2) is None
    # ... and when it comes back after the hole (an ad break), a clip never reaches across the hole:
    # what lies before it in the file is from minutes earlier, not the lead-up to this moment
    for t in range(500, 520, 5):
        grow(seg)
        rec.tick(t)
    assert seg.since == 500 and seg.born == 110
    held = rec.acquire(515 - 36, 515 - 2)
    assert held == (seg, 15, 13, 500), held
    rec.release(seg, cut_done=False)

with tempfile.TemporaryDirectory() as folder:
    # offline at start: tried again with growing pauses, never given up, and noticed when it comes back
    rec = recorder_in(folder)
    now, starts = 1000, []
    for _ in range(600):
        made = rec.made
        rec.tick(now)
        if rec.made > made:
            starts.append(now)
        for seg in rec.segments:
            if seg.ended is None and now - seg.started >= 3:
                die(seg)  # streamlink gives up: no playable streams
        now += 1
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # each try lives 4 s here, then the pause before the next one doubles from 10 s up to 2 min
    assert gaps[:4] == [14, 24, 44, 84] and set(gaps[4:]) == {124}, gaps
    assert sum("not live" in line for line in said) == 1, "said once, not on every try"
    rec.tick(now + 200)
    grow(rec.segments[-1])
    rec.tick(now + 201)
    assert said[-1] == "live again, recording" and rec.recording(now + 202)
    # the stream drops for a moment: recording starts again within seconds
    back = rec.segments[-1]
    die(back)
    rec.tick(now + 300)
    assert back.ended == now + 300 and "starting it again" in said[-1]
    rec.tick(now + 303)
    assert rec.segments[-1] is not back and rec.segments[-1].ended is None

with tempfile.TemporaryDirectory() as folder:
    # a switch whose new file never starts leaves the old recording alone and is tried again later
    rec = recorder_in(folder, rotate_mins=2)
    rec.tick(0)
    old = rec.segments[0]
    for t in range(1, 126, 4):
        grow(old)
        rec.tick(t)
    new = rec.segments[1]
    assert new.started == 121, "the switch comes 2 min after the first video"
    die(new)
    grow(old)
    rec.tick(126)
    assert new.ended == 126 and old.ended is None and "could not start" in said[-1]
    grow(old)
    rec.tick(130)
    assert len([s for s in rec.segments if s.ended is None]) == 1, "no new try before the pause is over"
    grow(old)
    rec.tick(137)
    assert rec.segments[-1].started == 137

    # a recorder that cannot even be run (ffmpeg gone) is not relaunched every second
    rec = recorder_in(folder)
    tries = []

    def broken(path):
        tries.append(path)
        raise FileNotFoundError("ffmpeg")

    rec.spawn = broken
    for t in range(0, 60):
        rec.tick(t)
    assert len(tries) == 3 and rec.segments == [] and "cannot be started" in said[-1], (len(tries), said[-1:])

with tempfile.TemporaryDirectory() as folder:
    # stopping: nothing new starts, files go, and --keep-rec keeps them under a name no clean-up touches
    rec = recorder_in(folder)
    rec.tick(0)
    grow(rec.segments[0])
    rec.tick(1)
    path = rec.segments[0].path
    rec.close()
    assert not path.exists() and rec.segments == []
    rec.tick(2)
    assert rec.segments == [], "a closed recorder starts nothing"

    rec = recorder_in(folder, keep_rec=True)
    rec.tick(0)
    kept = rec.segments[0].path
    assert re.fullmatch(r"chan_\d{8}_\d{6}_part1\.ts", kept.name), kept.name
    grow(rec.segments[0])
    rec.tick(1)
    rec.close()
    assert kept.exists()

# --- a killed run's temporary folder is removed by the next run, a living run's folder is not ---
with tempfile.TemporaryDirectory() as folder:
    watcher.REC, watcher.CLIPS = Path(folder) / "rec", Path(folder) / "clips"
    watcher.REC.mkdir()
    dead, alive = watcher.REC / "run-111-1", watcher.REC / "run-222-2"
    for run in (dead, alive):
        run.mkdir()
        (run / "chan_seg1_20260101_000000.ts").write_bytes(b"x" * 5000)
    old = time.time() - 60
    os.utime(dead, (old, old))
    os.utime(alive, (old, old))
    owner = lock_file(alive / "lock")
    held, freed = watcher.claim_run_folder()
    assert freed == 5000 and not dead.exists() and alive.exists() and watcher.RUN.is_dir()
    assert watcher.RUN.parent == watcher.REC and lock_file(watcher.RUN / "lock") is None, "our own folder is locked"
    owner.close()
    held.close()


# --- the count loop: warm-up, the threshold, a count with a hole in it, live changes ---
class StubRecorder:
    def __init__(self, tag, source, args, lag, index=0):
        self.tag, self.lock, self.pending = tag, threading.Lock(), 0
        self.thread = SimpleNamespace(start=lambda: None)

    def recording(self, now):
        return True

    def close(self):
        closed.append(self.tag)


def counts(*buckets):
    """Per-second chat values for whole 10 s counts; None marks a count during which chat went unseen."""
    out = []
    for total in buckets:
        out += [None] + [0] * 9 if total is None else [total] + [0] * 9
    return out


def run_watch(script, minimum, kick=False, before=None):
    clock, clips.clear(), closed.clear(), said.clear()
    watcher.STOP.clear()
    watcher.TUNE.clear()

    def scripted(tag, session, channel):
        for second, value in enumerate(script):
            if before and second in before:
                before[second]()
            clock.sleep(1)
            yield value
        watcher.STOP.set()
        yield 0

    def fake_clip(recorder, out, source, args, lag, noticed):
        clips.append((lag, noticed - clock_start))
        watcher.backlog -= 1

    clock_start = clock.now
    real = watcher.time, watcher.chat, watcher.Recorder, watcher.make_clip
    watcher.time, watcher.chat, watcher.Recorder, watcher.make_clip = clock, scripted, StubRecorder, fake_clip
    try:
        args = SimpleNamespace(before=20, after=10, cooldown=None, warmup=30, bucket=10, window=180, factor=2.5)
        watcher.watch("chan", "chan", kick, minimum, args, 0)
    finally:
        watcher.time, watcher.chat, watcher.Recorder, watcher.make_clip = real
        watcher.STOP.clear()
        watcher.TUNE.clear()
    for thread in threading.enumerate():
        if thread.name.startswith("clip "):
            thread.join()
    return [line for line in said if "Msgs:" in line]


clock, clips, closed = Clock(), [], []
lines = run_watch(counts(10, 10, 10, 12, 40, 50, None, 10, 10, 60, 70), 15,
                  before={100: lambda: watcher.TUNE.update(min=1000)})
assert [line.split(" | ", 2)[2] for line in lines[:4]] == ["warming up | recording"] * 3 + [
    "Median: 10 | Threshold: 25 | recording"], lines[:4]
assert lines[6].endswith("Msgs: 0 | chat not counted | recording"), lines[6]
assert "Median: 10 | Threshold: 25" in lines[9], "the count with a hole in it did not drag the usual rate down"
assert "Threshold: 1000" in lines[10], "a minimum changed while running applies from the next count"
# the spike at the 5th count, none 10 s later (cooldown), the next at the 10th; none after the minimum was raised
assert clips == [(0, 50), (0, 100)], clips
assert closed == ["chan"], "the recorder is closed when the channel stops"
assert sum("SPIKE" in line for line in said) == 2
run_watch(counts(*[0] * 8), 0)
assert clips == [], "a minimum of 0 does not make silence a spike"
run_watch(counts(10, 10, 10, 10, 40), 15, kick=True)
assert clips == [(watcher.KICK_LAG, 50)], "a Kick clip is told how late its chat was counted"


# hashtags: filled in, cleaned, each once, #Shorts last, never a pile
assert upload.hashtags(upload.HASHTAGS, "twitch.tv/ishowspeed") == "#ishowspeed #twitch #clips #Shorts"
assert upload.hashtags("#chatwentoff #{channel} #GTA6 #gta-6", "kick.com/xqc") == "#chatwentoff #xqc #GTA6 #Shorts"
assert upload.hashtags(" ".join(f"#t{i}" for i in range(40)), "twitch.tv/x").count("#") == 15


# --- the clip: which moment it asks for, how it is cut and encoded, and that it always lets go ---
class Holder:
    tag = "chan"

    def __init__(self, *answers):
        self.answers, self.asked, self.released = answers, [], []

    def acquire(self, first, last):
        self.asked.append((first, last))
        return self.answers[min(len(self.asked), len(self.answers)) - 1]  # the last answer is repeated

    def release(self, seg, cut_done=True):
        self.released.append((seg, cut_done))


def run_clip(answer, lag=0, fail=None, probe="1.5\n1.5,34.5\n", speech=None, **own):
    ran.clear()
    said.clear()

    def fake_run(*command):
        command = [str(c) for c in command]
        ran.append(command)
        if fail and fail in command:
            raise RuntimeError("ffmpeg failed: boom")
        if command[0] == "ffprobe" and "stream=codec_type,r_frame_rate:format=duration" in command:
            return "video,60/1\naudio,0/0\n34.5\n"
        if command[0] == "ffprobe":
            return "[h264 @ 000001] no frame!\n" + probe  # first picture at, then: the cut begins at, its length
        if command[1:3] == ["-m", "whisper"] and speech is not None:
            Path(folder, "chan_20260101_000000.srt").write_text(
                "1\n00:00:00,000 --> 00:00:02,000\nwhat a play\n" if speech else "", encoding="utf-8")
        if command[:2] == [sys.executable, "-c"]:
            return "noise\n[1920, 1080, null]\n"
        if "libx264" in command:
            Path(command[-1]).write_bytes(b"clip")
        return ""

    holder, clock = Holder(*answer) if isinstance(answer, list) else Holder(answer), Clock()
    real = watcher.time, watcher.run, watcher.take_slot
    watcher.time, watcher.run, watcher.take_slot = clock, fake_run, lambda jobs: SimpleNamespace(close=lambda: None)
    watcher.backlog = 1
    args = SimpleNamespace(before=20, after=10, jobs=1, crf=23, model="tiny", language=None, upload=False,
                           ratio=9 / 16, height=1920, face_share=1 / 3, face_zoom=4, big_face=BIG_FACE, cam="top",
                           cam_box=None, game_box=None, cam_share=None, captions="off", caption_size=6, hook="", hook_secs=3, zoom=0, tag=None,
                           logo=None, intro=None, outro=None, teaser=0, chat_box="left", chat_lines=7, credit="")
    vars(args).update(own)
    try:
        out = watcher.CLIPS / "chan_20260101_000000.mp4"
        watcher.make_clip(holder, out, "twitch.tv/chan", args, lag, clock.now)
    finally:
        watcher.time, watcher.run, watcher.take_slot = real
    return holder, clock.now - 1_000_000, out


ran = []
with tempfile.TemporaryDirectory() as folder:
    watcher.RUN, watcher.CLIPS = Path(folder), Path(folder) / "clips"
    watcher.CLIPS.mkdir()
    segment = SimpleNamespace(path=Path(folder) / "chan_seg1.ts")
    holder, waited, out = run_clip((segment, 36, 34, 1_000_000 - 24))
    assert waited == 12, "a Twitch clip waits for its 10 s after the spike, and 2 s more for the recorder"
    assert holder.asked == [(1_000_000 - 24, 1_000_000 + 10)], "20 s before the spike plus the lead-in, to 10 s after"
    cut, encode = ran[0], next(c for c in ran if "libx264" in c)
    assert cut[cut.index("-sseof"):][:8] == ["-sseof", "-36", "-i", str(segment.path), "-t", "34", "-c", "copy"], cut
    assert encode[encode.index("-i"):][:2] == ["-i", str(Path(folder) / "chan_20260101_000000.cut.ts")], encode
    assert "[0:v]trim=start=4.500," in encode[encode.index("-filter_complex") + 1], "the lead-in is cut after decoding"
    assert ["-c:v", "libx264"] == encode[encode.index("-c:v"):][:2] and "aac" in encode, "H.264 and AAC, said outright"
    assert holder.released == [(segment, True)] and watcher.backlog == 0 and out.read_bytes() == b"clip"
    assert said[-1].startswith("saved ") and not list(Path(folder).glob("*.cut.ts")) + list(Path(folder).glob("*.part.mp4"))

    holder, waited, out = run_clip((segment, 46, 34, 1_000_000 - 44), lag=20)
    assert waited == 0 and holder.asked == [(1_000_000 - 44, 1_000_000 - 10)], "a Kick clip sits 20 s further back"
    dropped = lambda: format(float(re.search(r"trim=start=([\d.]+)", next(c for c in ran if "libx264" in c)[
        next(c for c in ran if "libx264" in c).index("-filter_complex") + 1])[1]), "g")  # of a 34.5 s cut
    assert dropped() == "4.5"

    # the recording is 1.5 s behind: the clip waits for it, here one second, and is whole
    behind = (segment, 34.5, 32.5, 1_000_000 - 24)
    holder, waited, out = run_clip([behind, behind, (segment, 36, 34, 1_000_000 - 24)])
    assert waited == 13 and holder.released == [(segment, False), (segment, False), (segment, True)], holder.released
    assert dropped() == "4.5"
    # the recording stopped 5 s before the clip's end: after 6 s more of waiting the lead-in still goes,
    # and the clip is 5 s shorter at its end
    holder, waited, out = run_clip((segment, 29, 29, 1_000_000 - 24))
    assert dropped() == "9.5" and waited == 18 and holder.released[-1] == (segment, True)
    assert holder.released[:-1] == [(segment, False)] * 12, "while it waits it does not hold the file"
    # the recording only began 12 s before the spike: there is no lead-in to drop, the clip starts where it does
    run_clip((segment, 22, 22, 1_000_000 - 12))
    assert dropped() == "12.5"
    # ... and when its picture begins 1.25 s after its sound, as at the very start of a recording, that goes too
    run_clip((segment, 34, 34, 1_000_000 - 12), probe="2.75\n1.5,34.5\n")
    assert dropped() == "1.25"
    # only the lead-in in front of the clip was recorded: nothing to make a clip of
    holder, waited, out = run_clip((segment, 3, 3, 1_000_000 - 24))
    assert ran == [] and "no video was recorded" in said[-1] and holder.released[-1] == (segment, True)
    assert watcher.backlog == 0 and holder.released.count((segment, True)) == 1

    # the channel's own edits: burned-in words, a title line from them, the tag, motion, intro and outro
    extra = Path(folder) / "intro.mp4"
    extra.write_bytes(b"x")
    run_clip((segment, 36, 34, 1_000_000 - 24), speech=True, captions="burn", hook="{text}!", zoom=0.12,
             tag="@me", intro=str(extra), outro=str(extra))
    whisper = next(c for c in ran if c[1:3] == ["-m", "whisper"])
    assert whisper[3].endswith(".wav") and "--max_words_per_line" in whisper, "words are read before the encode"
    encode = next(c for c in ran if "libx264" in c)
    graph = encode[encode.index("-filter_complex") + 1]
    assert encode.count("-i") == 3 and "subtitles=" in graph and "zoompan=" in graph and "concat=n=3" in graph, graph
    assert "it-19.850" in graph, "the punch-in lands on the spike, 20 s into the clip"
    assert "[1:v]" in graph and "[2:v]" in graph and "[3:v]" not in graph, "intro is input 1, outro input 2"
    assert (Path(folder) / "clips" / "chan_20260101_000000.srt").exists() and said[-1].endswith(".srt")
    assert not list(Path(folder).glob("chan_20260101_000000.*")), "no working files are left"
    # the real chat of the moment goes into a box, and a teaser of the spike plays before the intro
    watcher.CHAT_LOG["twitch.tv/chan"] = deque([(1_000_000 - 40, "early", "too early"), (1_000_000 - 5, "fan", "LETS GO"),
                                               (1_000_000 + 1, "other", "no way {x}")])
    run_clip((segment, 36, 34, 1_000_000 - 24), teaser=2.5, intro=str(extra), credit="{source}")
    encode = next(c for c in ran if "libx264" in c)
    graph = encode[encode.index("-filter_complex") + 1]
    assert "ass=" in graph, "the chat box is burned in"
    assert "chan_20260101_000000.credit.txt" in graph and "x=w-tw-" in graph, "the streamer is named on screen"
    assert graph.endswith("[vteaser][ateaser][vintro][aintro][vmain][amain]concat=n=3:v=1:a=1[v][a]"), graph[-120:]
    assert "trim=start=19.500:end=22.000" in graph, "the teaser starts just before the spike, 20 s into the clip"
    del watcher.CHAT_LOG["twitch.tv/chan"]
    run_clip((segment, 36, 34, 1_000_000 - 24), speech=False, captions="burn", hook="{text}")
    graph = next(c for c in ran if "libx264" in c)
    graph = graph[graph.index("-filter_complex") + 1]
    assert "subtitles=" not in graph and "drawtext" not in graph and "concat" not in graph, "nothing said, no title"
    watcher.TUNE.update(ratio=1.0, cam_box=(0.7, 0.05, 0.25, 0.3))
    run_clip((segment, 36, 34, 1_000_000 - 24))
    watcher.TUNE.clear()
    graph = next(c for c in ran if "libx264" in c)
    graph = graph[graph.index("-filter_complex") + 1]
    assert "scale=1920:1152[cam]" in graph, "a shape and cam area changed while running apply to the next clip"

    holder, waited, out = run_clip(None)
    assert holder.released == [(None, True)] and watcher.backlog == 0 and "no video was recorded" in said[-1]
    assert ran == [], "nothing is cut when no recording holds the moment"

    holder, waited, out = run_clip((segment, 36, 34, 1_000_000 - 24), fail="copy")
    assert holder.released == [(segment, True)] and watcher.backlog == 0 and "failed" in said[-1]

# the chat box: lines in the order they came, the newest at the bottom, the box never longer than asked
box_o = SimpleNamespace(width=1080, height=1920, chat_box="left", chat_lines=2)
ass = watcher.chat_box([(103.0, "b", "second"), (100.0, "a", r"first {x} \ y"), (103.02, "c", "third"),
                        (110.0, "d", "a" * 80)], 100.0, 30, box_o)
events = [line for line in ass.splitlines() if line.startswith("Dialogue")]
assert len(events) == 3, events  # b and c landed together and show up together
assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:03.00,") and "first (x) / y" in events[0]
assert "second" in events[1] and "third" in events[1] and "first" not in events[1], "two lines at most"
assert events[2].endswith("...") and events[2].split(",")[2] == "0:00:30.00", "a long line is cut; the last stays to the end"
assert r"\an1\pos(43,1229)" in events[0] and "PlayResY: 1920" in ass
box_o.chat_box = "right"
assert r"\an3\pos(1037," in watcher.chat_box([(100.0, "a", "x")], 100.0, 30, box_o)
box_o.chat_box = "off"
assert watcher.chat_box([(100.0, "a", "x")], 100.0, 30, box_o) is None
assert watcher.chat_box([], 100.0, 30, SimpleNamespace(chat_box="left")) is None
# slurs are masked, words that only look alike are not, and chat bots are left out
masks = [w for w in ("Niggas", "nigga!", "spic", "spicy", "raccoon", "coon", "retarded", "fine") if watcher.masked(w)]
assert masks == ["Niggas", "nigga!", "spic", "coon", "retarded"], masks
box_o.chat_box = "left"
ass = watcher.chat_box([(100.0, "Nightbot", "follow!"), (100.5, "kaimafiabot", "!watchtime"),
                        (101.0, "fan", "wake up nigga lol")], 100.0, 30, box_o)
assert "Nightbot" not in ass and "kaimafiabot" not in ass and "wake up *** lol" in ass

# --- commands from the board on stdin: quit is still heard after stop ---
quits = []
real_stdin, real_quit = sys.stdin, watcher.quit_now
sys.stdin, watcher.quit_now = io.StringIO("min=170\nfactor=1.8\nnonsense\nmin=abc\nratio=4:5\nratio=wide\n"
                                         "cam=0.7,0.05,0.25,0.3\ncam=2,2,2,2\nstop\nquit\n"), lambda: quits.append(1)
watcher.STOP.clear()
watcher.control()
sys.stdin, watcher.quit_now = real_stdin, real_quit
assert watcher.TUNE == {"min": 170, "factor": 1.8, "ratio": 0.8, "cam_box": (0.7, 0.05, 0.25, 0.3)}, watcher.TUNE
assert watcher.STOP.is_set() and quits == [1]
watcher.STOP.clear()
watcher.TUNE.clear()

# --- Twitch chat reader: counts split messages, answers PING, comes back after a drop, and says when it is blind ---
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(2)
state = {}


def irc_server():
    conn, _ = listener.accept()
    state["hello"] = conn.recv(200)
    conn.sendall(b"".join(b":u!u@u.tmi.twitch.tv PRIVMSG #test :hi %d\r\n" % i for i in range(50)) + b"PING :tmi.twitch.tv\r\n")
    conn.settimeout(5)
    state["pong"] = conn.recv(100)
    conn.close()  # the connection drops
    conn, _ = listener.accept()  # the reader came back
    conn.recv(200)
    flood = b"".join(b":u!u@u.tmi.twitch.tv PRIVMSG #test :message number %d\r\n" % i for i in range(1000))
    for i in range(0, len(flood), 1777):  # odd chunks split lines mid-message
        conn.sendall(flood[i:i + 1777])
    time.sleep(2)
    conn.close()


threading.Thread(target=irc_server, daemon=True).start()
real_irc, real_retry = watcher.TWITCH_IRC, watcher.RETRY
watcher.TWITCH_IRC, watcher.RETRY = ("127.0.0.1", listener.getsockname()[1], False), 1
said.clear()
total, blind, deadline = 0, 0, time.time() + 20
for n in watcher.chat("test", watcher.twitch_session, "test"):
    if n is None:
        blind += 1
    else:
        total += n
    if total >= 1050 or time.time() > deadline:
        break
watcher.TWITCH_IRC, watcher.RETRY = real_irc, real_retry
assert total == 1050, total
assert state["hello"] == b"NICK justinfan12345\r\nJOIN #test\r\n" and state["pong"] == b"PONG :tmi.twitch.tv\r\n"
assert blind >= 1 and said.count("chat connected") == 2 and any("chat lost" in line for line in said)
heard = list(watcher.CHAT_LOG["twitch.tv/test"])
assert len(heard) == 1050 and heard[0][1:] == ("u", "hi 0") and heard[-1][1:] == ("u", "message number 999"), heard[:2]


# --- Kick chat reader: nothing counts until the replay has caught up with the moment it joined ---
class KickStub(BaseHTTPRequestHandler):
    born = time.time()

    def log_message(self, *args):
        pass

    def do_GET(self):
        sent = int((time.time() - self.born) * 5)  # five messages a second since the server started
        stamp = lambda i: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.born + i / 5))
        messages = [{"id": i, "created_at": stamp(i)} for i in range(max(0, sent - 25), sent)][::-1]
        body = json.dumps({"data": {"messages": messages, "cursor": None}}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


httpd, base = serve(KickStub)
real_kick = watcher.KICK_API, watcher.kick_channel_id, watcher.KICK_LAG, watcher.KICK_POLL
watcher.KICK_API, watcher.kick_channel_id, watcher.KICK_LAG, watcher.KICK_POLL = base, lambda channel: 1, 2, 1
seen, started = [], time.time()
for n in watcher.kick_session("anyone"):
    seen.append((time.time() - started, n))
    if time.time() - started > 8:
        break
watcher.KICK_API, watcher.kick_channel_id, watcher.KICK_LAG, watcher.KICK_POLL = real_kick
httpd.shutdown()
assert all(n is None for t, n in seen if t < 2.5), "the first seconds are not counted: the replay is still short"
counted = [n for t, n in seen if n is not None]
assert counted and 10 <= sum(counted) <= 40, (len(counted), sum(counted))  # about five a second once it counts


# --- who is live in a category: the official APIs, stood in for by a local server ---
class Sites(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def answer(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        if form["client_secret"] == ["portal"]:
            return self.answer(200, {"message": "sign in to the wifi first"})
        ok = form["grant_type"] == ["client_credentials"] and form["client_secret"] != ["bad"]
        self.answer(200 if ok else 400, {"access_token": "tok-" + self.path.split("/")[1]} if ok else {"message": "invalid"})

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        Sites.seen.append((url.path, self.headers.get("Authorization"), self.headers.get("Client-Id")))
        if url.path == "/helix/games":
            if q["name"] == "portal page":
                return self.answer(200, ["not", "what", "was", "asked"])
            self.answer(200, {"data": [{"id": "509658", "name": "Just Chatting"}] if q["name"].lower() == "just chatting" else []})
        elif url.path == "/helix/games/top" and self.headers.get("Client-Id") == "odd":
            self.answer(200, ["not", "what", "was", "asked"])
        elif url.path == "/helix/games/top":
            self.answer(200, {"data": [{"id": "509658", "name": "Just Chatting"}, {"id": "33214", "name": "Fortnite"}][:int(q["first"])]})
        elif url.path == "/helix/search/categories":
            self.answer(200, {"data": [{"id": "509658", "name": "Just Chatting"}, {"id": "2", "name": "Chatting Simulator"}]})
        elif url.path == "/helix/streams":
            rows = [{"user_login": f"streamer{i}", "viewer_count": 9000 - i * 1000, "title": f"title {i}"} for i in range(8)]
            self.answer(200, {"data": rows[:int(q["first"])]} if q["game_id"] == "509658" else {"data": []})
        elif url.path == "/public/v2/categories":
            self.answer(200, {"data": [{"id": 15, "name": "Just Chatting"}] if q["name"].lower() == "just chatting" else []})
        elif url.path == "/public/v1/categories":
            self.answer(200, {"data": [{"id": 15, "name": "Just Chatting"}]})
        elif url.path == "/public/v2/livestreams":
            # two pages, oldest first; the most watched channels went live last
            chat, slots = {"id": 15, "name": "Just Chatting"}, {"id": 28, "name": "Slots"}
            page = [{"channel": {"slug": "latecomer"}, "viewer_count": 9999, "title": "late", "category": chat},
                    {"channel": {"slug": "spinner"}, "viewer_count": 30000, "title": "spin", "category": slots},
                    {"channel": {"slug": "nameless"}, "viewer_count": 5, "title": "", "category": None}
                    ] if q.get("cursor") == "page2" else [
                {"channel": {"slug": f"kicker-{i}"}, "viewer_count": v, "title": f"kick {i}", "category": chat}
                for i, v in enumerate([5, 900, 40, 7000, 0])]
            if q.get("limit") != "1000":
                page = page[:1]  # Kick's own page size is small
            if "category_id" in q:
                page = [s for s in page if s["category"] and str(s["category"]["id"]) == q["category_id"]]
            self.answer(200, {"data": page, "pagination": {"next_cursor": "" if "cursor" in q else "page2"}})
        else:
            self.answer(404, {"message": "no"})


httpd, base = serve(Sites)
with tempfile.TemporaryDirectory() as folder:
    live.KEYS = Path(folder) / "apps.json"
    live.TOKEN = {"twitch": base + "/twitch/token", "kick": base + "/kick/token"}
    live.API = {"twitch": base + "/helix", "kick": base + "/public"}

    def problem(*args):
        try:
            live.live(*args)
        except live.Problem as e:
            return str(e)

    assert "needs your own app keys in apps.json" in problem("Just Chatting"), "no keys file yet"
    live.KEYS.write_text(json.dumps({"twitch": {"client_id": "id1", "client_secret": "bad"}}), encoding="utf-8")
    assert "did not accept the app keys" in problem("Just Chatting")
    assert "needs your own app keys" in problem("Just Chatting", "kick"), "keys for one site do not open the other"
    live.KEYS.write_text(json.dumps({"twitch": {"client_id": "id1", "client_secret": "s1"},
                                     "kick": {"client_id": "id2", "client_secret": "s2"}}), encoding="utf-8")
    name, found = live.live("just chatting", "twitch", 3)
    assert name == "Just Chatting" and [c["channel"] for c in found] == ["streamer0", "streamer1", "streamer2"]
    assert found[0] == {"channel": "streamer0", "viewers": 9000, "title": "title 0"}
    assert ("/helix/streams", "Bearer tok-twitch", "id1") in Sites.seen, "the token and the client id go with every call"
    assert problem("Chatting") == "Twitch has no category named 'Chatting'. Close ones: Just Chatting, Chatting Simulator"
    name, found = live.live("Just Chatting", "kick", 3)
    assert name == "Just Chatting" and [(c["channel"], c["viewers"]) for c in found] == [
        ("latecomer", 9999), ("kicker-3", 7000), ("kicker-1", 900)], "Kick lists oldest first, so every page is read"
    assert problem("Nope", "kick") == "Kick has no category named 'Nope'. Close ones: Just Chatting"
    assert problem("  ") == "Enter a category, for example Just Chatting"
    assert problem("Just Chatting", "twitch", 101) == problem("Just Chatting", "kick", 0) == "Ask for 1 to 100 channels"
    assert problem("portal page") == "Twitch answered in a form this version does not understand"

    # the categories for the dropdown: Twitch has a call for them, Kick's are added up from its live channels
    assert live.categories("twitch") == ["Just Chatting", "Fortnite"]
    assert ("/helix/games/top", "Bearer tok-twitch", "id1") in Sites.seen
    assert live.categories("kick") == ["Slots", "Just Chatting"], "30000 against 17944 viewers; no category, not counted"
    assert live.categories("kick", 1) == ["Slots"]
    assert len(live.live("Just Chatting", "twitch", 100)[1]) == 8 and len(live.live("Just Chatting", "kick", 1)[1]) == 1

    def said_by(call, *args):
        try:
            call(*args)
        except live.Problem as e:
            return str(e)

    # an answer in a form nobody expected is a Problem the person can read, from the token call on
    odd = "Twitch answered in a form this version does not understand"
    live.KEYS.write_text(json.dumps({"twitch": {"client_id": "odd", "client_secret": "s1"}}), encoding="utf-8")
    assert said_by(live.categories, "twitch") == odd
    live.KEYS.write_text(json.dumps({"twitch": {"client_id": "id1", "client_secret": "portal"}}), encoding="utf-8")
    assert said_by(live.categories, "twitch") == said_by(live.live, "Just Chatting") == odd
    live.KEYS.unlink()
    try:
        live.categories("kick")
        raise AssertionError("no keys, no list")
    except live.Problem as e:
        assert "needs your own app keys" in str(e)
httpd.shutdown()


# --- the board's server ---
class FakeChild:
    made = []

    def __init__(self, command, **kw):
        self.command, self.kw, self.told, self.gone, self.code = command, kw, [], threading.Event(), 0
        self.stdin = SimpleNamespace(write=self.told.append, flush=lambda: None, close=lambda: self.told.append("<closed>"))
        self.stdout = iter(FakeChild.lines)
        FakeChild.made.append(self)

    def wait(self, timeout=None):
        if not self.gone.wait(timeout):
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.code

    def terminate(self):
        self.gone.set()


FakeChild.lines = [
    "[xqc] watching twitch.tv/xqc\n",
    "[xqc] 20s | Msgs: 84 | Median: 91 | Threshold: 46 | recording\n",
    "[xqc] SPIKE: 84 msgs (median 91), cutting xqc_20261118_201228.mp4\n",
    "[xqc] saved clips\\xqc_20261118_201228.mp4 and xqc_20261118_201228.srt\n",
    "[xqc] frame none\n",
    "[xqc] 30s | Msgs: 3 | chat not counted | recording\n",
]

with tempfile.TemporaryDirectory() as folder:
    gui.SAVED = Path(folder) / "streams.json"
    httpd, token = gui.make_server()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    board = httpd.board

    def call(path, body=None, sent_token=token, host=None):
        headers = {"X-Token": sent_token, **({"Host": host} if host else {})}
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                framing = r.headers["X-Frame-Options"]
                return r.status, json.loads(r.read()) if "json" in r.headers["Content-Type"] else (r.read(), framing)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    status, (page, framing) = call("/", sent_token="")
    assert status == 200 and token.encode() in page and b"__TOKEN__" not in page, "the served page carries the token"
    assert framing == "DENY", "no other site may put the board in a frame"
    assert call("/", host="evil.example")[0] == 403, "the token is not handed to a rebound hostname"
    assert call("/api/options", sent_token="wrong")[0] == 403, "another website cannot drive the server"
    assert call("/api/options", host="evil.example")[0] == 403, "DNS rebinding"
    assert call("/api/add", {"channels": "xqc"}, sent_token="")[0] == 403

    groups = call("/api/options")[1]["groups"]
    offered = {o["flag"] for g in groups for o in g["options"]}
    flags = {a.option_strings[0] for a in watcher.build_parser()._actions if a.option_strings}
    assert offered == flags - {"-h", "--stdin-control", "--category", "--top", "--cam-box", "--game-box", "--cam-share"}, "the page offers the rest"
    assert all(o["detail"] and o["label"] for g in groups for o in g["options"]), "every option explains itself"

    # add: several at once, a link names its site, plain names follow the Kick box
    new = {"min": 15, "factor": 2.5}
    assert call("/api/add", {"channels": "xqc https://kick.com/xqc, Dinah", **new})[0] == 200
    ids = lambda: [s["id"] for s in call("/api/state")[1]["streams"]]
    assert ids() == ["twitch-xqc", "kick-xqc", "twitch-dinah"]
    assert call("/api/add", {"channels": "xqc", **new}) == (400, {"error": "xqc is already on the board"})
    assert call("/api/add", {"channels": "xqc, newone", **new})[0] == 200, "one already there does not hold back the rest"
    assert call("/api/add", {"channels": "plain https://twitch.tv/linked", "kick": True, **new})[0] == 200
    assert ids()[3:] == ["twitch-newone", "kick-plain", "twitch-linked"], "the Kick box never turns a twitch.tv link into Kick"
    for stray in ("twitch-newone", "kick-plain", "twitch-linked", "kick-xqc"):
        assert call("/api/remove", {"id": stray})[0] == 200
    for bad in ({"channels": "@secrets.txt"}, {"channels": "https://evil.example/x"}, {"channels": ""},
                {"channels": "ok", "min": "many"}, {"channels": "ok", "min": 0}):
        assert call("/api/add", {**new, **bad})[0] == 400, bad
    assert call("/api/tune", {"id": "twitch-xqc", "min": 170, "factor": 2})[0] == 200
    assert call("/api/tune", {"id": "twitch-xqc", "min": 0, "factor": 2})[0] == 400
    assert call("/api/tune", {"id": "nobody", "min": 1, "factor": 2})[0] == 400
    for bad in (["--nope=1"], ["@file"], ["--stdin-control"], ["--category=x"], "--ratio=1:1"):
        assert call("/api/start", {"id": "twitch-xqc", "args": bad})[0] == 400, bad
    assert call("/api/nothing", {})[0] == 404

    # the settings are remembered whole, defaults for new streams included
    assert call("/api/shared", {"args": ["--min=40", "--ratio=1:1", "--kick"]})[0] == 200
    saved = json.loads(gui.SAVED.read_text(encoding="utf-8"))
    assert saved == {"shared": ["--min=40", "--ratio=1:1", "--kick"], "streams": [
        {"channel": "xqc", "kick": False, "min": 170, "factor": 2.0, "cam": None, "game": None, "share": None, "ratio": None},
        {"channel": "dinah", "kick": False, "min": 15, "factor": 2.5, "cam": None, "game": None, "share": None,
         "ratio": None}]}, saved
    assert call("/api/options")[1]["shared"] == ["--min=40", "--ratio=1:1", "--kick"]

    # finding streams by category goes through live.py; its message reaches the page as it is
    real_live = live.live
    live.live = lambda category, site, count: ("Just Chatting", [{"channel": "Someone", "viewers": 12, "title": "hi"}][:count])
    status, found = call("/api/find", {"category": "just chatting", "site": "kick", "count": 3})
    assert status == 200 and found == {"category": "Just Chatting", "site": "kick", "streams": [
        {"channel": "Someone", "viewers": 12, "title": "hi", "link": "kick.com/Someone", "id": "kick-someone"}]}, found

    for many in ("16", 16, 1):  # the page sends what was typed, as text
        assert call("/api/find", {"category": "just chatting", "count": many})[0] == 200, many
    for many in (30, 17, 0, "2.5", "", "a few", None):
        status, refused = call("/api/find", {"category": "just chatting", "count": many})
        assert status == 400 and "1 to 16" in refused["error"], "more than the board holds is refused, not cut to 5"

    def refuse(category, site, count):
        raise live.Problem("Twitch has no category named 'x'")

    live.live = refuse
    assert call("/api/find", {"category": "x", "count": 5}) == (400, {"error": "Twitch has no category named 'x'"})
    live.live = real_live

    # the dropdown's categories: the site is asked once, then the list is kept for five minutes
    real_categories, asked = live.categories, []
    live.categories = lambda site: asked.append(site) or ["Just Chatting", "Slots"]
    assert call("/api/categories?site=kick") == (200, {"site": "kick", "categories": ["Just Chatting", "Slots"]})
    assert call("/api/categories?site=kick")[0] == 200 and asked == ["kick"], "not asked again so soon"
    assert call("/api/categories")[1]["site"] == "twitch" and asked == ["kick", "twitch"]
    board.topped["kick"] = (time.time() - 301, ["old"])
    assert call("/api/categories?site=kick")[1]["categories"] == ["Just Chatting", "Slots"] and asked[2:] == ["kick"]
    assert call("/api/categories", sent_token="wrong")[0] == 403

    def no_keys(site):
        raise live.Problem("needs your own app keys")

    board.topped.clear()
    live.categories = no_keys
    assert call("/api/categories?site=twitch") == (400, {"error": "needs your own app keys"})
    live.categories = real_categories

    # starting a row: its own minimum and multiplier, the shared settings without theirs, apart from our terminal
    real_popen = subprocess.Popen
    subprocess.Popen = FakeChild
    try:
        assert call("/api/start", {"id": "twitch-xqc"})[0] == 200
    finally:
        subprocess.Popen = real_popen
    child = FakeChild.made[-1]
    assert child.command == [sys.executable, "-u", str(gui.HERE / "watcher.py"), "--stdin-control", "twitch.tv/xqc",
                             "--min=170", "--factor=2", "--ratio=1:1"], child.command
    assert all(child.kw[k] == v for k, v in watcher.APART.items()) and child.kw["cwd"] == gui.HERE
    stream = board.streams["twitch-xqc"]
    deadline = time.time() + 3
    while stream.clips == 0 or stream.stats["elapsed"] != 30:
        assert time.time() < deadline, "the board did not read its stream's lines"
        time.sleep(0.02)
    row = call("/api/state")[1]
    assert row["streams"][0]["stats"] == {"elapsed": 30, "msgs": 3, "phase": "chat not counted", "threshold": None,
                                          "median": None, "video": True}
    assert row["streams"][0]["history"] == [[84, 46], [3, None]] and row["streams"][0]["last"] == "xqc_20261118_201228.mp4"
    assert not any("Msgs:" in line for line in row["lines"]) and any("SPIKE" in line for line in row["lines"])
    assert not any("frame" in line for line in row["lines"]) and stream.frames == 1 and stream.frame is None
    assert call("/api/remove", {"id": "twitch-xqc"}) == (400, {"error": "Stop the stream before removing it"})

    # stopping: asked once; asking everything to stop again never forces; only a named, forced stop quits at once
    assert call("/api/tune", {"id": "twitch-xqc", "min": 200, "factor": 3})[0] == 200
    assert child.told == ["min=200\n", "factor=3\n"], "a running stream is told its new values"
    call("/api/stop-all", {})
    call("/api/stop-all", {})
    call("/api/stop", {"id": "twitch-xqc"})
    assert child.told[2:] == ["stop\n"] and stream.stopping, child.told
    call("/api/stop", {"id": "twitch-xqc", "force": True})
    assert child.told[3:] == ["quit\n"]
    child.code = 1  # it quits the way a forced quit does, with an exit code that says "killed"
    child.gone.set()

    def gone():
        deadline = time.time() + 3
        while stream.proc:
            assert time.time() < deadline
            time.sleep(0.02)

    gone()
    assert not stream.stopping and stream.forced and stream.exit == 0, "a quit that was asked for is not an error"
    # a cam area and a shape set by hand are kept, checked, and used from the next start
    box = [0.7, 0.05, 0.25, 0.3]
    assert call("/api/shape", {"id": "twitch-xqc", "cam": box, "ratio": "4:5"})[0] == 200
    for bad in ({"cam": [0.9, 0, 0.3, 0.3]}, {"cam": "0.1,0.1,0.2,0.2"}, {"cam": [0.1, 0.1]}, {"ratio": "wide"},
                {"cam": [0.1, 0.1, 0.2, "x"]}):
        assert call("/api/shape", {"id": "twitch-xqc", "cam": box, "ratio": "4:5", **bad})[0] == 400, bad
    assert call("/api/shape", {"id": "nobody", "cam": box})[0] == 400
    peeked = []
    board.peek = lambda s: peeked.append(s.id) or (b"jpeg peek", None)
    status, (body, _) = call("/api/frame?id=twitch-xqc")
    assert status == 200 and body == b"jpeg peek" and peeked == ["twitch-xqc"], "a stopped stream is peeked at"
    board.peek = lambda s: (None, "Not live right now, so there is no picture. The settings can still be made.")
    assert call("/api/frame?id=twitch-xqc")[0] == 409
    del board.peek
    # the same exit code without that request is one, and the row says so
    subprocess.Popen = FakeChild
    try:
        assert call("/api/start", {"id": "twitch-xqc"})[0] == 200
    finally:
        subprocess.Popen = real_popen
    second = FakeChild.made[-1]
    assert "--cam-box=0.7,0.05,0.25,0.3" in second.command and "--ratio=4:5" in second.command
    assert "--ratio=1:1" not in second.command, "the stream's own shape wins over the shared one"
    assert call("/api/state")[1]["streams"][0]["cam"] == box
    assert call("/api/shape", {"id": "twitch-xqc", "cam": None, "ratio": None})[0] == 200
    assert second.told[-4:] == ["cam=auto\n", "game=auto\n", "share=auto\n", "ratio=1:1\n"], "back to automatic"
    assert call("/api/shape", {"id": "twitch-xqc", "cam": box, "ratio": "4:5", "game": [0.2, 0, 0.5, 1],
                               "share": 0.25})[0] == 200
    assert second.told[-4:] == ["cam=0.7,0.05,0.25,0.3\n", "game=0.2,0,0.5,1\n", "share=0.25\n", "ratio=4:5\n"], \
        "a running stream is told at once"
    deadline = time.time() + 3
    while stream.clips < 2 or stream.frames < 2:  # its own start-up lines are read first
        assert time.time() < deadline
        time.sleep(0.02)

    # a still of the stream for the cam editor: asked of watcher.py, and only ever a picture from rec/
    picture = gui.HERE / "rec" / "frame-selfcheck.jpg"
    picture.parent.mkdir(exist_ok=True)
    picture.write_bytes(b"\xff\xd8 picture")

    def answer(path):
        def reply():
            deadline = time.time() + 3
            while second.told.count("frame\n") <= len(answered) and time.time() < deadline:
                time.sleep(0.01)
            answered.append(path)
            with board.lock:
                stream.frame, stream.frames = path, stream.frames + 1
        threading.Thread(target=reply, daemon=True).start()

    answered = []
    try:
        answer("rec/frame-selfcheck.jpg")
        status, (body, _) = call("/api/frame?id=twitch-xqc")
        assert status == 200 and body == b"\xff\xd8 picture"
        answer("gui.py")
        assert call("/api/frame?id=twitch-xqc")[0] == 409, "nothing outside rec/ is served"
        answer(None)
        assert call("/api/frame?id=twitch-xqc") == (409, {"error": "Nothing recorded yet; try again once the row says recording"})
    finally:
        picture.unlink()
    second.code = 1
    FakeChild.made[-1].gone.set()
    gone()
    assert not stream.forced and stream.exit == 1
    httpd.shutdown()

    # the board comes back as it was left, and one damaged row costs only itself
    board = gui.Board()
    assert [s.id for s in board.streams.values()] == ["twitch-xqc", "twitch-dinah"] and board.streams["twitch-xqc"].min == 200
    assert board.streams["twitch-xqc"].cam == [0.7, 0.05, 0.25, 0.3] and board.streams["twitch-xqc"].ratio == "4:5"
    saved = json.loads(gui.SAVED.read_text(encoding="utf-8"))
    saved["streams"].insert(1, {"channel": "broken"})
    gui.SAVED.write_text(json.dumps(saved), encoding="utf-8")
    assert [s.id for s in gui.Board().streams.values()] == ["twitch-xqc", "twitch-dinah"]
    saved["shared"] = None
    gui.SAVED.write_text(json.dumps(saved), encoding="utf-8")
    assert [s.id for s in gui.Board().streams.values()] == ["twitch-xqc", "twitch-dinah"], "damaged settings cost no row"
    del saved["streams"]
    saved["shared"] = ["--ratio=1:1", 7, "--nope=1"]
    gui.SAVED.write_text(json.dumps(saved), encoding="utf-8")
    assert gui.Board().shared == ["--ratio=1:1"] and gui.Board().streams == {}
    for odd_rows in (5, {}, "x"):
        gui.SAVED.write_text(json.dumps({"shared": ["--ratio=1:1"], "streams": odd_rows}), encoding="utf-8")
        assert gui.Board().shared == ["--ratio=1:1"] and gui.Board().streams == {}
    for unreadable in ("{ not json", "[1, 2]", "null"):
        gui.SAVED.write_text(unreadable, encoding="utf-8")
        assert gui.Board().streams == {}, "an unreadable file means an empty board, not a crash"

# --- watcher.py --category: what it says when the lookup brings nothing ---
real_argv, real_live, real_stderr = sys.argv, live.live, sys.stderr
live.live = lambda category, site, count: ("Chess", [])
for argv, message in ((["--category", "chess"], "nobody is live in Chess right now"),
                      (["--category", "chess", "--top", "100"], "nobody is live in Chess right now"),
                      (["--category", "chess", "--top", "101"], "--top must be from 1 to 100"),
                      (["--category", "chess", "--top", "0"], "--top must be from 1 to 100"),
                      ([], "give at least one channel")):
    sys.argv, sys.stderr = ["watcher.py", *argv], io.StringIO()
    try:
        watcher.main()
        raise AssertionError("it should not have started")
    except SystemExit:
        said_last = sys.stderr.getvalue()
    finally:
        sys.argv, sys.stderr = real_argv, real_stderr
    assert message in said_last, said_last
live.live = real_live

# --- clips to review: nothing goes to YouTube until a person presses Upload ---
with tempfile.TemporaryDirectory() as folder:
    gui.SAVED, gui.CLIPS = Path(folder) / "streams.json", Path(folder) / "clips"
    gui.CLIPS.mkdir()
    (gui.CLIPS / "xqc_20261120_120000.mp4").write_bytes(b"video one")
    (gui.CLIPS / "xqc_20261120_120000.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nwhat a play\n", encoding="utf-8")
    (gui.CLIPS / "dinah-kick_20261120_130000.mp4").write_bytes(b"video two")
    httpd, token = gui.make_server()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    board = httpd.board

    def ask(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=data, headers={"X-Token": token})
        try:
            with urllib.request.urlopen(request, timeout=5) as r:
                return r.status, json.loads(r.read()) if "json" in r.headers["Content-Type"] else r.read()
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    listed = ask("/api/clips")[1]
    clips = {c["name"]: c for c in listed["clips"]}
    assert listed["privacy"] == "private" and set(clips) == {"xqc_20261120_120000.mp4", "dinah-kick_20261120_130000.mp4"}
    assert clips["xqc_20261120_120000.mp4"]["title"] == "what a play | xqc" and clips["xqc_20261120_120000.mp4"]["state"] == "waiting"
    assert clips["dinah-kick_20261120_130000.mp4"]["source"] == "kick.com/dinah"
    assert ask("/api/clip?name=xqc_20261120_120000.mp4") == (200, b"video one")
    for bad in ("../streams.json", "..%5Cgui.py", "nothere_20261120_120000.mp4"):
        assert ask("/api/clip?name=" + bad)[0] == 404, bad

    sent, real_publish = [], upload.publish

    def fake_publish(clip, source, privacy, title, description, say=print, tags="", thumbnail=None):
        sent.append((Path(clip).name, source, privacy, title, description, tags, thumbnail and Path(thumbnail).name))
        say("thumbnail set")
        return "abc123"

    upload.publish = fake_publish
    try:
        for bad in ({"privacy": "everyone"}, {"title": ""}, {"title": "x" * 101}, {"name": "../gui.py"}):
            body = {"name": "xqc_20261120_120000.mp4", "title": "t", "description": "", "privacy": "unlisted", **bad}
            assert ask("/api/review/upload", body)[0] == 400, bad
        assert sent == [], "nothing went out for a request that was refused"
        # a thumbnail made from the clip goes out with it; the frame grab is stood in for
        real_run = watcher.run
        grabbed = []
        watcher.run = lambda *c: grabbed.append([str(x) for x in c]) or Path(str(c[-1])).write_bytes(b"jpg") or ""
        try:
            assert ask("/api/review/thumbnail", {"name": "xqc_20261120_120000.mp4", "at": 21.5, "text": "no way he did that"})[0] == 200
            assert ask("/api/review/thumbnail", {"name": "../gui.py", "at": 1, "text": "x"})[0] == 400
        finally:
            watcher.run = real_run
        assert grabbed[0][grabbed[0].index("-ss") + 1] == "21.500" and "drawtext" in " ".join(grabbed[0])
        assert ask("/api/thumb?name=xqc_20261120_120000.mp4") == (200, b"jpg")
        assert ask("/api/review/upload", {"name": "xqc_20261120_120000.mp4", "title": "{x} my title",
                                          "description": "words", "privacy": "unlisted",
                                          "tags": "#xqc #gta6"})[0] == 200
        deadline = time.time() + 3
        while board.reviewed.get("xqc_20261120_120000.mp4", {}).get("state") != "uploaded":
            assert time.time() < deadline
            time.sleep(0.02)
        assert sent == [("xqc_20261120_120000.mp4", "twitch.tv/xqc", "unlisted", "{{x}} my title", "words", "#xqc #gta6",
                         "xqc_20261120_120000.jpg")], sent
        kept = gui.CLIPS / "uploaded"
        assert sorted(p.name for p in kept.iterdir()) == ["xqc_20261120_120000.jpg", "xqc_20261120_120000.mp4",
                                                          "xqc_20261120_120000.srt"], "an uploaded clip is kept apart"
        listed = {c["name"]: c for c in ask("/api/clips")[1]["clips"]}
        assert listed["xqc_20261120_120000.mp4"]["state"] == "uploaded" and listed["xqc_20261120_120000.mp4"]["thumb"]
        assert ask("/api/clip?name=xqc_20261120_120000.mp4") == (200, b"video one"), "it can still be watched"
        assert ask("/api/review/upload", {"name": "xqc_20261120_120000.mp4", "title": "t", "privacy": "private"})[0] == 400
        assert gui.Board().reviewed["xqc_20261120_120000.mp4"] == {"state": "uploaded", "privacy": "unlisted", "id": "abc123"}

        def refuse(*args, **kw):
            raise RuntimeError("YouTube refused the upload: 403 quotaExceeded")

        upload.publish = refuse
        assert ask("/api/review/upload", {"name": "dinah-kick_20261120_130000.mp4", "title": "t", "privacy": "private"})[0] == 200
        deadline = time.time() + 3
        while board.reviewed.get("dinah-kick_20261120_130000.mp4", {}).get("state") != "failed":
            assert time.time() < deadline
            time.sleep(0.02)
        assert "quotaExceeded" in ask("/api/clips")[1]["clips"][0]["error"] + ask("/api/clips")[1]["clips"][1].get("error", "")
    finally:
        upload.publish = real_publish
    # the teaser of a clip in review is cut again from the moment picked, or taken out
    (gui.CLIPS / "dinah-kick_20261120_130000.json").write_text(json.dumps(
        {"teaser": 2.5, "teaser_from": 19.5, "spike": 20.0, "main": 30.0, "intro": 1.2}), encoding="utf-8")
    remade, real_run = [], watcher.run

    def fake_ffmpeg(*command):
        remade.append([str(c) for c in command])
        Path(str(command[-1])).write_bytes(b"remade")
        return ""

    watcher.run = fake_ffmpeg
    try:
        assert {c["name"]: c["parts"] for c in ask("/api/clips")[1]["clips"]}["dinah-kick_20261120_130000.mp4"]["spike"] == 20.0
        for bad in ({"start": 29.5, "seconds": 2}, {"start": 5, "seconds": 0.5}, {"start": -1, "seconds": 2},
                    {"start": "x", "seconds": 2}):
            assert ask("/api/review/teaser", {"name": "dinah-kick_20261120_130000.mp4", **bad})[0] == 400, bad
        assert ask("/api/review/teaser", {"name": "xqc_20261120_120000.mp4", "start": 1, "seconds": 2})[0] == 400, \
            "an uploaded clip is not remade"
        assert ask("/api/review/teaser", {"name": "dinah-kick_20261120_130000.mp4", "start": 10, "seconds": 3})[0] == 200
        deadline = time.time() + 3
        while board.reviewed.get("dinah-kick_20261120_130000.mp4", {}).get("state") == "editing":
            assert time.time() < deadline
            time.sleep(0.02)
        graph = remade[-1][remade[-1].index("-filter_complex") + 1]
        assert "trim=start=13.700:duration=3.000" in graph and "trim=start=2.500," in graph, graph
        assert (gui.CLIPS / "dinah-kick_20261120_130000.mp4").read_bytes() == b"remade"
        assert json.loads((gui.CLIPS / "dinah-kick_20261120_130000.json").read_text())["teaser"] == 3.0
        assert ask("/api/review/teaser", {"name": "dinah-kick_20261120_130000.mp4", "start": 0, "seconds": 0})[0] == 200
        deadline = time.time() + 3
        while board.reviewed.get("dinah-kick_20261120_130000.mp4", {}).get("state") == "editing":
            assert time.time() < deadline
            time.sleep(0.02)
        graph = remade[-1][remade[-1].index("-filter_complex") + 1]
        assert "concat" not in graph and "trim=start=3.000," in graph, "the 3 s teaser is taken out again"
    finally:
        watcher.run = real_run
    assert ask("/api/review/delete", {"name": "dinah-kick_20261120_130000.mp4"})[0] == 200
    assert not any(gui.CLIPS.glob("dinah-kick_20261120_130000.*")), "the clip and its files are gone"
    assert ask("/api/review/delete", {"name": "dinah-kick_20261120_130000.mp4"})[0] == 400
    assert ask("/api/review/delete", {"name": "xqc_20261120_120000.mp4"})[0] == 400, "an uploaded clip is not deleted here"
    assert ask("/api/review/delete", {"name": "../streams.json"})[0] == 400
    # clearing out deletes what waits for review, never what was uploaded
    (gui.CLIPS / "ohne_20261120_140000.mp4").write_bytes(b"x")
    (gui.CLIPS / "ohne_20261120_140000.srt").write_text("x", encoding="utf-8")
    assert ask("/api/review/clear", {})[0] == 200
    assert not list(gui.CLIPS.glob("*.mp4")) and (gui.CLIPS / "uploaded" / "xqc_20261120_120000.mp4").exists()

    # a promo, intro, outro or logo picked on the page is copied into brand/
    real_here, gui.HERE = gui.HERE, Path(folder)
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/media?kind=intro&name=My%20Intro!.mp4",
                                         data=b"promo bytes", headers={"X-Token": token})
        with urllib.request.urlopen(request, timeout=5) as r:
            assert json.loads(r.read()) == {"path": "brand/intro-My-Intro.mp4"}
        assert (Path(folder) / "brand" / "intro-My-Intro.mp4").read_bytes() == b"promo bytes"
        for query in ("kind=evil&name=x.mp4", "kind=intro&name=x.exe", "kind=logo&name=x.mp4", "kind=intro&name=..%2F..%2Fx.mp4"):
            request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/media?{query}", data=b"x",
                                             headers={"X-Token": token})
            try:
                urllib.request.urlopen(request, timeout=5)
                assert query.endswith("x.mp4") and "..%2F" in query, query
            except urllib.error.HTTPError as e:
                assert e.code == 400, query
        assert not any(Path(folder).glob("x.mp4")) and [f.name for f in (Path(folder) / "brand").iterdir()] == [
            "intro-My-Intro.mp4", "intro-x.mp4"], list((Path(folder) / "brand").iterdir())
        request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/api/media?kind=intro&name=x.mp4",
                                         data=b"x", headers={"X-Token": "wrong"})
        try:
            urllib.request.urlopen(request, timeout=5)
            raise AssertionError("another website cannot drop files here")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        gui.HERE = real_here
    # a stream started from the board never uploads on its own, even with the upload setting on
    board.add("xqc", 15, 2.5)
    board.shared = ["--upload", "--privacy=public"]
    real_popen, subprocess.Popen = subprocess.Popen, FakeChild
    try:
        board.start("twitch-xqc", None)
    finally:
        subprocess.Popen = real_popen
    assert "--upload" not in FakeChild.made[-1].command and "--privacy=public" in FakeChild.made[-1].command
    FakeChild.made[-1].gone.set()
    httpd.shutdown()

# the two lines of watcher.py that the board reads must stay in the form it expects
stat = gui.STAT.match("[xqc] 1805s | Msgs: 130 | Median: 128.5 | Threshold: 325 | recording")
assert stat and stat.groups() == ("1805", "130", None, "128.5", "325", "recording"), stat
stat = gui.STAT.match("[xqc] 15s | Msgs: 108 | warming up | no video")
assert stat and stat.groups() == ("15", "108", "warming up", None, None, "no video")
assert gui.STAT.match("[xqc-kick] 40s | Msgs: 2 | chat not counted | recording")[3] == "chat not counted"
assert gui.CLIP.search(r"[xqc] saved clips\xqc_20261118_172259.mp4 and xqc_20261118_172259.srt")[1].endswith(".mp4")

print("ok")
