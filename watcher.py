#!/usr/bin/env python3
"""Record live Twitch or Kick streams and cut a vertical clip every time a chat spikes."""
import argparse
import json
import math
import os
import re
import shutil
import signal
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path

import live
import upload

os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")  # OpenCV's own chatter would land untagged in the log
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")

HERE = Path(__file__).resolve().parent
RETRY = 5  # first chat reconnect delay, doubles up to 60 s
TWITCH_IRC = ("irc.chat.twitch.tv", 443, True)  # host, port, TLS; 443 gets through firewalls that block 6667
KICK_API = "https://kick.com/api/v2/channels"
KICK_POLL = 5  # seconds between Kick chat polls; Kick refreshes its history every 10 s
KICK_PAGES = 8  # pages of 25 messages fetched per poll at most
KICK_LAG = 20  # Kick chat is counted this many seconds late, see kick_session
GOP_PAD = 4  # extra seconds cut ahead of a clip so it can start on a whole frame
SETTLE = 2  # seconds a clip waits beyond its end, so the recorder has written all of it
CATCH_UP = 6  # seconds more it waits when the recording is behind: video arrives in bursts
START_TIMEOUT = 240  # a new recording with no video after this long is given up (an ad break can hold one up)
GAP = 10  # a recording that stood still this long has a hole in it: an ad break, or a stall
BACKLOG = 6  # clips of this run still to be finished before new spikes are skipped
REC, CLIPS = Path("rec"), Path("clips")
RUN = REC  # this run's own folder for temporary files; main() makes it

FACE_SCORE = 0.8
FACE_MODEL = HERE / "face_detection_yunet_2023mar.onnx"
FACE_MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + FACE_MODEL.name
FACE_CODE = (f"import json, sys; sys.path.insert(0, {str(HERE)!r}); import watcher; "
             "print(json.dumps(watcher.find_face(sys.argv[1])))")

# helper programs run out of reach of the terminal's Ctrl+C: stopping is done in order, recorders last,
# and clips being finished are not cut down with the rest
APART = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
# Linux: run a command so that the kernel ends it when the thread that started it is gone
PDEATH = ("import ctypes, os, signal, sys\n"
          "try:\n    ctypes.CDLL(None).prctl(1, signal.SIGTERM)\n"
          "except (OSError, AttributeError):\n    pass\n"
          "os.execvp(sys.argv[1], sys.argv[1:])")

STOP = threading.Event()  # set once to wind every channel down
TUNE = {}  # "min" and "factor" changed while running (--stdin-control); they override the command line
printing, shared = threading.Lock(), threading.Lock()
children = set()  # every helper program now running, so a forced quit can take them all down
backlog = 0  # clips of this run cut or about to be, and not finished yet
quitting = False  # set by quit_now: a clip that dies with us then is not a failure worth a line


def log(tag, text):
    with printing:
        try:
            print(f"[{tag}] {text}", flush=True)
        except (OSError, ValueError):
            pass  # nobody is listening any more: the terminal or the settings page is gone


def lock_file(path):
    """Open path holding an exclusive lock, or None when another process or thread already holds it.

    The lock goes when the file is closed or its process dies, however it dies.
    """
    f = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def kill_tree(proc):
    """End a helper program and what it started.

    On Windows ffmpeg can be a launcher whose real program outlives a plain kill. Elsewhere a helper that
    leads its own process group takes the group with it.
    """
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
            return
        try:
            if os.getpgid(proc.pid) == proc.pid:
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except OSError:
            pass  # it went on its own in the meantime


def tied(command):
    """The command, arranged so that it cannot outlive this program, however this program ends.

    A recorder left behind would keep filling the disk. Linux asks the kernel; on Windows the job object
    from die_together() covers every helper; macOS has neither, so there a hard kill of us can leave it running.
    """
    return [sys.executable, "-c", PDEATH, *command] if sys.platform.startswith("linux") else command


def die_together():
    """Windows: put this process in a job that ends every helper program when this process is gone."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32")
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = Extended()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if job and k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k32.AssignProcessToJobObject(job, k32.GetCurrentProcess())
    return job  # the job lives as long as this handle, and the handle as long as this process


def quit_now():
    """Leave at once. Helper programs are stopped first, or they would carry on alone; then our own folder goes."""
    global quitting
    quitting = True
    for proc in list(children):
        kill_tree(proc)
    if RUN.name.startswith("run-"):  # never the rec folder itself
        shutil.rmtree(RUN, ignore_errors=True)  # what is still held open is removed by the next start
    os._exit(1)


def relay(tag, pipe):
    """Pass a recorder's messages on under the channel's tag."""
    for line in pipe:
        text = re.sub(r"^(\[[\w.]+\])+ ?", "", line.decode(errors="replace").strip())  # streamlink's own [cli][info]
        # start-up chatter, and the offline error that Recorder reports once in its own words
        if text and not any(dull in text for dull in ("Found matching plugin", "Available streams", "No playable streams", "Error opening input")):
            log(tag, text)


def run(*command):
    """Run a helper program to its end and return what it printed; raise with its last line when it fails."""
    proc = subprocess.Popen(
        [str(part) for part in command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors="replace", **APART,
    )
    with shared:
        children.add(proc)
    try:
        printed = proc.communicate()[0]
    finally:
        with shared:
            children.discard(proc)
    if proc.returncode:
        raise RuntimeError(f"{Path(str(command[0])).stem} failed: {(printed.strip().splitlines() or ['no output'])[-1]}")
    return printed


def ffmpeg(*args):
    run("ffmpeg", "-nostdin", "-y", "-loglevel", "error", *args)


def take_slot(jobs):
    """Wait for one of the `jobs` places in line that every clip-watcher running in this folder shares."""
    while True:
        for n in range(jobs):
            held = lock_file(REC / f"job{n}.lock")
            if held:
                return held
        time.sleep(0.5)


def is_spike(count, history, minimum, factor):
    return count >= minimum and count >= factor * statistics.median(history)


def twitch_session(channel):
    host, port, tls = TWITCH_IRC
    with socket.create_connection((host, port), timeout=10) as sock:
        if tls:
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.sendall(f"NICK justinfan12345\r\nJOIN #{channel}\r\n".encode())
        sock.settimeout(1)
        buf, heard = b"", time.time()
        while time.time() - heard < 360:  # Twitch pings every ~5 min, longer silence is a dead link
            try:
                data = sock.recv(4096)
            except TimeoutError:
                yield 0
                continue
            if not data:
                raise OSError("server closed the connection")
            heard = time.time()
            *lines, buf = (buf + data).split(b"\r\n")
            for line in lines:
                if line.startswith(b"PING"):
                    sock.sendall(b"PONG" + line[4:] + b"\r\n")
            yield sum(b" PRIVMSG #" in line for line in lines)
        raise OSError("no data for 6 min")


def kick_channel_id(channel):
    import requests

    r = requests.get(f"{KICK_API}/{channel}", timeout=15)
    if r.status_code == 404:
        raise LookupError(f"there is no Kick channel '{channel}'")
    if not r.ok:
        raise OSError(f"Kick answered HTTP {r.status_code}")
    return r.json()["id"]


def kick_session(channel):
    """Chat counts for Kick, replayed KICK_LAG seconds late by each message's own timestamp.

    Kick caches its public message history for 10 s, so messages reach us in lumps. Counted on arrival
    every lump would look like a spike; replayed by timestamp they give the real rate.
    """
    import requests

    channel_id = kick_channel_id(channel)
    # at most KICK_PAGES x 25 messages per poll; raise KICK_PAGES for chats faster than that
    seen, due, offset, primed, counting_from = deque(maxlen=5000), [], None, False, None
    while True:
        now, cursor, stamps = time.time(), None, []
        for _ in range(KICK_PAGES if primed else 1):
            params = {"cursor": cursor} if cursor else {}
            r = requests.get(f"{KICK_API}/{channel_id}/messages", params=params, timeout=10)
            if not r.ok:
                raise OSError(f"Kick answered HTTP {r.status_code}")
            data = r.json()["data"]
            fresh = [m for m in data["messages"] if m["id"] not in seen]
            seen.extend(m["id"] for m in fresh)
            stamps += [datetime.fromisoformat(m["created_at"].replace("Z", "+00:00")).timestamp() for m in fresh]
            cursor = data.get("cursor")
            if len(fresh) < len(data["messages"]) or not fresh or not cursor:
                break  # reached messages we already had
        if stamps:
            lag = now - max(stamps)  # smallest value seen = clock difference between Kick and this machine
            offset = lag if offset is None else min(offset, lag)
            if primed:
                due += [stamp + offset + KICK_LAG for stamp in stamps]
        if not primed:  # messages older than this moment were skipped, so the replay is short until it has passed
            primed, counting_from = True, now + KICK_LAG + KICK_POLL
        wake = now + KICK_POLL
        while time.time() < wake:
            ready = sum(stamp <= time.time() for stamp in due)
            due = [stamp for stamp in due if stamp > time.time()]
            yield ready if time.time() >= counting_from else None
            time.sleep(1)


def chat(tag, session, channel):
    """Yield counts of new chat messages until STOP is set, reconnecting with backoff.

    0 means a second without messages. None means chat was not being counted (not connected, or not caught
    up yet), which the caller must not take for a quiet chat.
    """
    delay = RETRY
    while True:
        try:
            joined = False
            for n in session(channel):
                if not joined:
                    joined = True
                    log(tag, "chat connected")
                delay = RETRY
                yield n
        except Exception as e:  # whatever a chat server sends back, the channel keeps going
            log(tag, f"chat lost ({e}); retrying in {delay} s")
        wake = time.time() + delay
        while time.time() < wake:  # keep ticking for the caller while we wait
            yield None
            if STOP.wait(1):
                return
        delay = min(delay * 2, 60)


class Segment:
    """One recording file and the two programs writing it."""

    def __init__(self, path, procs, started):
        self.path, self.procs, self.started = path, procs, started
        self.size, self.born, self.grew = 0, None, None  # bytes seen; wall time of its first and its newest video
        # wall time its newest unbroken stretch of video began: from there to `grew` the file runs in step
        # with the clock, across a hole (an ad break) it does not
        self.since = None
        self.ended = None  # when its recorder stopped; None while it records
        self.refs = 0  # clip cuts reading the file right now


class Recorder:
    """The rolling recording of one channel.

    Every `rotate` seconds a new segment starts. Old and new record side by side for `overlap` seconds, so a
    clip around the switch is whole in one of them; then the old one stops and its file is deleted once no
    clip can still want it. Disk use stays near one segment. A recorder that dies, or a stream that is not
    live, is tried again with growing pauses for as long as the channel is watched.
    """

    def __init__(self, tag, source, args, lag, index=0):
        self.tag, self.source, self.args = tag, source, args
        self.reach = args.before + args.after + GOP_PAD + max(SETTLE, lag - args.after)  # how far back a cut reads
        self.overlap = max(args.overlap_secs, self.reach + 15)
        self.rotate = max(args.rotate_mins * 60, self.overlap + 60)
        if (self.overlap, self.rotate) != (args.overlap_secs, args.rotate_mins * 60):
            log(tag, f"using {self.overlap:g} s of overlap and a new recording file every {self.rotate / 60:g} min")
        self.stagger = index * (self.overlap + 30)  # channels started together do not all switch files at once
        self.lock = threading.Lock()
        self.segments, self.made, self.pending = [], 0, 0
        self.pause, self.next_try, self.offline = 0, 0.0, False
        self.closing = self.done = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def spawn(self, path):
        """streamlink piped into an MPEG-TS remux, the one container ffmpeg can cut from while it still grows."""
        stream = subprocess.Popen(
            # streamlink ranks 1080p60 above 1080p, so the cap is written against the 60 fps name
            tied([sys.executable, "-m", "streamlink", "--stream-sorting-excludes", f">{self.args.quality}60",
                  f"https://{self.source}", "best", "-O"]),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **APART,
        )
        try:
            remux = subprocess.Popen(
                tied(["ffmpeg", "-y", "-loglevel", "error", "-i", "-", "-c", "copy", "-f", "mpegts", str(path)]),
                stdin=stream.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **APART,
            )
        except BaseException:
            kill_tree(stream)  # half a recorder would download the stream into nothing
            raise
        finally:
            stream.stdout.close()
        with shared:
            children.update((stream, remux))
        for proc in (stream, remux):
            threading.Thread(target=relay, args=(self.tag, proc.stderr), daemon=True).start()
        return stream, remux

    def _start(self, now):
        stamp, n = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)), self.made + 1
        # kept recordings get a name and a place that the clean-up of temporary files never touches
        name = f"{self.tag}_{stamp}_part{n}.ts" if self.args.keep_rec else f"{self.tag}_seg{n}_{stamp}.ts"
        path = (REC if self.args.keep_rec else RUN) / name
        try:
            procs = self.spawn(path)
        except OSError as e:  # the recorder itself cannot be run: wait as for a stream that is not live
            self.pause = min(max(self.pause * 2, 10), 120)
            self.next_try = now + self.pause
            log(self.tag, f"the recorder cannot be started ({e}); trying again in {self.pause} s")
            return
        self.made = n
        self.segments.append(Segment(path, procs, now))

    def _end(self, seg, now):
        if seg.procs[0].poll() is None:
            seg.procs[0].terminate()  # the remux then reaches the end of its input and closes the file itself
        seg.ended = now

    def _sweep(self, now, final=False):
        """Drop recordings that are over, once their programs are gone and no clip can still want them."""
        for seg in [s for s in self.segments if s.ended is not None and not s.refs]:
            busy = [p for p in seg.procs if p.poll() is None]
            if busy:
                if final or now - seg.ended > 10:
                    for proc in busy:
                        kill_tree(proc)
                continue
            with shared:
                children.difference_update(seg.procs)
            if self.pending or not final and now - seg.ended < self.reach + 10:
                continue  # a spike just after a break still finds its lead-up here
            if not self.args.keep_rec:
                try:
                    seg.path.unlink(missing_ok=True)
                except OSError:
                    continue  # still open somewhere; try again on the next tick
                if seg.size and not final:
                    log(self.tag, f"deleted {seg.path.name} ({seg.size / 1e9:.1f} GB)")
            self.segments.remove(seg)

    def tick(self, now):
        """Look at every segment and move the rotation along. Runs about once a second."""
        with self.lock:
            had_video = any(s.born for s in self.segments if s.ended is None)
            for seg in self.segments:
                try:
                    size = seg.path.stat().st_size
                except OSError:
                    size = 0
                if size > seg.size:
                    if seg.grew is None or now - seg.grew > GAP:
                        seg.since = now
                    seg.size, seg.grew = size, now
                    if seg.born is None:
                        seg.born, self.pause = now, 0
                        if not had_video:
                            log(self.tag, "live again, recording" if self.offline else f"recording to {seg.path.name}")
                            self.offline = False
                if seg.ended is None and (
                        seg.procs[0].poll() is not None or seg.born is None and now - seg.started >= START_TIMEOUT):
                    self._end(seg, now)
                    self.next_try = now + 2
                    if seg.born is None:  # it never had video: pause longer before every further try
                        self.pause = min(max(self.pause * 2, 10), 120)
                        self.next_try = now + self.pause
                        if any(s.ended is None for s in self.segments):
                            log(self.tag, f"a new recording file could not start; trying again in {self.pause} s")
            live = [s for s in self.segments if s.ended is None]
            winding_down = STOP.is_set() or self.closing
            if not live and had_video and not winding_down:
                log(self.tag, "the recording stopped; starting it again")
            elif not live and self.segments and not self.offline and self.segments[-1].born is None:
                self.offline = True
                log(self.tag, "not live, or the recording cannot start; checking again every minute or two")
            if not winding_down and now >= self.next_try:
                if not live:
                    self._start(now)
                elif len(live) == 1 and live[0].born and (
                        now - live[0].born >= self.rotate + (self.stagger if self.made == 1 else 0)):
                    self._start(now)
                    log(self.tag, f"new recording file; the old one stops after {self.overlap:g} s side by side")
            if len(live) > 1 and live[-1].born and now - live[-1].born >= self.overlap:
                for seg in live[:-1]:
                    self._end(seg, now)
            self._sweep(now)

    def _run(self):
        while not self.done:
            try:
                self.tick(time.time())
            except Exception as e:  # a recorder hiccup must not end the recording thread
                log(self.tag, f"recorder problem: {type(e).__name__}: {e}")
            time.sleep(1)

    def recording(self, now):
        """True while video is arriving."""
        with self.lock:
            return any(s.ended is None and s.grew and now - s.grew < 15 for s in self.segments)

    def acquire(self, first, last):
        """The segment holding most of the moment from `first` to `last` (wall-clock times).

        Returns it with one more reference, how many seconds before its end the cut starts, how long the
        cut is, and the wall-clock time the cut starts at. None when nothing recorded that moment.
        """
        with self.lock:
            best, most = None, 0
            for seg in self.segments:
                if seg.born is not None:
                    held = min(last, seg.grew) - max(first, seg.since)
                    if held > 0 and held >= most:  # on a tie the newer segment wins
                        best, most = seg, held
            if best is None:
                return None
            best.refs += 1
            start = max(first, best.since)
            return best, best.grew - start, min(last, best.grew) - start, start

    def release(self, seg, cut_done=True):
        with self.lock:
            if seg:
                seg.refs -= 1
            if cut_done:
                self.pending -= 1

    def close(self):
        """Let clips that are waiting take their cut, stop recording, and delete what was recorded."""
        self.closing = True
        deadline = time.time() + self.args.after + SETTLE + 30
        while time.time() < deadline:
            with self.lock:
                if not self.pending and not any(s.refs for s in self.segments):
                    break
            time.sleep(0.2)
        self.done = True
        if self.thread.is_alive():
            self.thread.join(5)
        with self.lock:
            for seg in self.segments:
                if seg.ended is None:
                    self._end(seg, time.time())
        for seg in list(self.segments):
            for proc in seg.procs:
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    kill_tree(proc)
        with self.lock:
            self._sweep(time.time(), final=True)
            self._sweep(time.time(), final=True)  # a second pass for programs the first one had to end


def steady_face(boxes, frames):
    """Median box of the biggest group of overlapping detections; None unless it shows in a third of the frames."""
    best = []
    for x, y, w, h in boxes:
        group = [b for b in boxes if abs(b[0] + b[2] / 2 - x - w / 2) < w and abs(b[1] + b[3] / 2 - y - h / 2) < h]
        if len(group) > len(best):
            best = group
    if not best or len(best) * 3 < frames:
        return None
    return tuple(int(statistics.median(v)) for v in zip(*best))


def face_model():
    """Path of the face detector, fetched on first use."""
    if not FACE_MODEL.exists():
        part = FACE_MODEL.with_name(f"{FACE_MODEL.name}.{os.getpid()}.part")
        urllib.request.urlretrieve(FACE_MODEL_URL, part)
        os.replace(part, FACE_MODEL)  # a half-finished download never looks like the model
    return str(FACE_MODEL)


def find_face(path):
    """Frame width, frame height and the streamer's face box (or None), sampling one frame per second."""
    import cv2

    video = cv2.VideoCapture(str(path))
    w, h = int(video.get(cv2.CAP_PROP_FRAME_WIDTH)), int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = video.get(cv2.CAP_PROP_FPS)
    step = round(fps) if 1 <= fps <= 120 else 30
    try:
        detector = cv2.FaceDetectorYN.create(face_model(), "", (w, h), FACE_SCORE)
    except Exception:  # no detector (the model could not be fetched): the clip gets the plain centre crop
        return w, h, None
    boxes, frames, n = [], 0, 0
    while video.grab():
        if n % step == 0:
            faces = detector.detect(video.retrieve()[1])[1]
            boxes += [] if faces is None else [tuple(int(v) for v in face[:4]) for face in faces]
            frames += 1
        n += 1
    return w, h, steady_face(boxes, frames)


def even(v):
    return int(v) // 2 * 2


def layout(w, h, face, o):
    """ffmpeg filter graph that turns a w x h frame into o.width x o.height, built around the face when there is one.

    o.face_h is the height of the facecam panel; 0 or a landscape output means the panels are never stacked.
    """
    out_w, out_h, face_h = o.width, o.height, o.face_h

    def clamp(v, top):
        return int(max(0, min(v, top)))

    def crop(aspect):
        return even(min(w, h * aspect)), even(min(h, w / aspect))

    def one(x, y):
        return f"[0:v]crop={cw}:{ch}:{x}:{y},scale={out_w}:{out_h},setsar=1[v]"

    cw, ch = crop(out_w / out_h)
    big = face is not None and face[2] >= w * o.big_face
    if face is None or not big and (face_h <= 0 or out_h < out_w):
        return one((w - cw) // 2, (h - ch) // 2)
    fx, fy, fw, fh = face
    if big:
        return one(clamp(fx + fw / 2 - cw / 2, w - cw), clamp(fy + fh / 2 - ch / 2, h - ch))
    tw = even(min(w, fw * o.face_zoom, h * out_w / face_h))
    th = even(tw * face_h / out_w)
    tx, ty = clamp(fx + fw / 2 - tw / 2, w - tw), clamp(fy + fh / 2 - th * 0.4, h - th)
    gw, gh = crop(out_w / (out_h - face_h))
    return (
        f"[0:v]split[a][b];[a]crop={tw}:{th}:{tx}:{ty},scale={out_w}:{face_h}[cam];"
        f"[b]crop={gw}:{gh}:{(w - gw) // 2}:{(h - gh) // 2},scale={out_w}:{out_h - face_h}[game];"
        f"{'[cam][game]' if o.cam == 'top' else '[game][cam]'}vstack,setsar=1[v]"
    )


def make_clip(recorder, out, source, args, lag, noticed):
    """Cut, crop, subtitle and maybe upload the clip around a spike noticed at wall-clock time `noticed`."""
    global backlog
    tag = recorder.tag
    moment = noticed - lag  # when chat really spiked: Kick chat is counted `lag` seconds late
    raw, part = RUN / f"{out.stem}.cut.ts", RUN / f"{out.stem}.part.mp4"
    try:
        segment = None
        try:
            last = moment + args.after
            time.sleep(max(0, last + SETTLE - time.time()))  # let the recording take in what follows
            late = time.time() + CATCH_UP
            while True:
                held = recorder.acquire(moment - args.before - GOP_PAD, last)
                if not held or held[2] + held[3] >= last or time.time() >= late:  # none, all of it, or waited enough
                    break
                recorder.release(held[0], cut_done=False)
                time.sleep(0.5)
            if held:
                segment, offset, duration, start = held
                keep = duration - max(0, moment - args.before - start)  # the cut without its lead-in
            if not held or keep <= 0:  # nothing, or nothing but the lead-in in front of the clip
                log(tag, f"no video was recorded around that spike; {out.name} skipped")
                return
            # a stream copy starts mid-GOP, hence the GOP_PAD extra in front that the encode trims off again
            ffmpeg("-sseof", -offset, "-i", segment.path, "-t", duration, "-c", "copy", raw)
        finally:
            recorder.release(segment)
        slot = take_slot(args.jobs)
        try:
            w, h, face = json.loads(run(sys.executable, "-c", FACE_CODE, raw).strip().splitlines()[-1])
            # the copy begins on a keyframe a little before the cut asked for, so its true length says how much
            # to drop in front; dropping after decoding is exact to the frame, seeking inside the file is not.
            # A cut from the very start of a recording has sound before its first picture; that goes as well
            probe = run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=start_time:format=start_time,duration", "-of", "csv=p=0", raw).split()
            # the last two words: where the picture starts, then where the cut starts and how long it is
            picture, (began, real) = float(probe[-2]), map(float, probe[-1].split(","))
            ffmpeg("-i", raw, "-ss", max(0, real - keep, picture - began), "-filter_complex", layout(w, h, face, args),
                   "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", args.crf, "-c:a", "aac", part)
            shutil.move(part, out)
            try:
                run(sys.executable, "-m", "whisper", out, "--model", args.model, "--output_format", "srt",
                    "--output_dir", out.parent, "--verbose", "False", "--fp16", "False",
                    *(["--language", args.language] if args.language else []))
                log(tag, f"saved {out} and {out.with_suffix('.srt').name}")
            except RuntimeError as e:
                log(tag, f"saved {out} without subtitles ({e})")
            if args.upload:
                upload.publish(out, source, args.privacy, args.title, args.description, lambda text: log(tag, text))
        finally:
            slot.close()
    except Exception as e:  # one bad clip must not take the channel down
        if not quitting:
            log(tag, f"clip {out.name} failed: {e}")
    finally:
        with shared:
            backlog -= 1
        for leftover in (raw, part):
            leftover.unlink(missing_ok=True)


def watch(tag, channel, kick, minimum, args, index):
    """Follow one channel until STOP is set."""
    global backlog
    source = f"kick.com/{channel}" if kick else f"twitch.tv/{channel}"
    lag = KICK_LAG if kick else 0
    recorder = Recorder(tag, source, args, lag, index)
    cooldown = args.before + args.after if args.cooldown is None else args.cooldown
    warm = max(1, math.ceil(args.warmup / args.bucket))
    history, seen, count, blind = deque(maxlen=max(1, args.window // args.bucket)), 0, 0, False
    last_clip, began = 0.0, time.time()
    try:
        recorder.thread.start()
        log(tag, f"watching {source}")
        bucket_end = began + args.bucket
        for n in chat(tag, kick_session if kick else twitch_session, channel):
            if n is None:
                blind = True  # part of this count went unseen
            else:
                count += n
            now = time.time()
            if STOP.is_set():
                break
            if now < bucket_end:
                continue
            floor, factor = max(1, TUNE.get("min", minimum)), TUNE.get("factor", args.factor)
            video = "recording" if recorder.recording(now) else "no video"
            line = f"{now - began:.0f}s | Msgs: {count}"
            if blind:  # a count with a hole in it is neither judged nor remembered as the usual rate
                log(tag, f"{line} | chat not counted | {video}")
            elif seen < warm:
                log(tag, f"{line} | warming up | {video}")
            else:
                usual = statistics.median(history)
                log(tag, f"{line} | Median: {usual:g} | Threshold: {max(floor, math.ceil(factor * usual))} | {video}")
                if now - last_clip >= cooldown and is_spike(count, history, floor, factor):
                    last_clip = now
                    out = CLIPS / f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
                    if backlog >= BACKLOG:
                        log(tag, f"SPIKE: {count} msgs, skipped: {backlog} clips are still being finished "
                                 "(raise --jobs or use a smaller --model)")
                    else:
                        # whether video of that moment exists is for the clip to find out: a recorder that
                        # stopped a second ago still holds it
                        log(tag, f"SPIKE: {count} msgs (median {usual:g}), cutting {out.name}")
                        with shared:
                            backlog += 1
                        with recorder.lock:
                            recorder.pending += 1
                        threading.Thread(target=make_clip, args=(recorder, out, source, args, lag, now),
                                         name=f"clip {out.stem}").start()
            if not blind:
                history.append(count)
                seen += 1
            count, blind, bucket_end = 0, False, now + args.bucket
    except Exception as e:  # one channel failing must not stop the others
        log(tag, f"stopped by an error: {type(e).__name__}: {e}")
    finally:
        recorder.close()


def parse_channel(text):
    """(channel, site) from a channel name or a twitch.tv / kick.com link; None if it is neither.

    site is "twitch" or "kick" for a link, and None for a plain name, whose site is the caller's to decide.
    """
    text = text.strip()
    link = re.fullmatch(r"(?:https?://)?(?:www\.)?(twitch\.tv|kick\.com)/([A-Za-z0-9_-]{1,30})/?", text, re.I)
    name = link[2] if link else text
    if re.fullmatch(r"[A-Za-z0-9_-]{1,30}", name):
        return name.lower(), link[1].lower().split(".")[0] if link else None


def parse_target(text):
    """(channel, site, own minimum or None) from a name or link, optionally followed by :<minimum>."""
    head, _, own = text.rpartition(":")
    if own.isdigit() and parse_channel(head):
        return (*parse_channel(head), int(own))
    parsed = parse_channel(text)
    return (*parsed, None) if parsed else None


def ratio(text):
    m = re.fullmatch(r"(\d+):(\d+)", text)
    if not m or "0" in m.groups():
        raise argparse.ArgumentTypeError("use WIDTH:HEIGHT, for example 9:16")
    return int(m[1]) / int(m[2])


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="A channel can carry its own minimum, as in xqc:170. Options can be kept in a preset file, "
               "one per line (--ratio=1:1), and loaded with @file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        fromfile_prefix_chars="@",
    )
    p.add_argument("channels", nargs="*", metavar="channel",
                   help="channel name or twitch.tv / kick.com link; several can be given")
    # the settings page drives us through stdin; --stdin-stop is what an older, still running page calls it
    p.add_argument("--stdin-control", "--stdin-stop", dest="stdin_control", action="store_true", help=argparse.SUPPRESS)
    stream = p.add_argument_group("stream")
    stream.add_argument("--kick", action="store_true", help="channels given by name are on Kick, not Twitch")
    stream.add_argument("--category",
                        help="also watch the most watched live channels of this category right now; needs apps.json")
    stream.add_argument("--top", type=int, default=3, help="how many channels --category brings in, 1 to 100")
    stream.add_argument("--quality", default="1080p", choices=["1080p", "720p", "480p", "360p", "160p"],
                        help="highest quality to record; the best the stream offers up to this is used")
    stream.add_argument("--rotate-mins", type=float, default=60,
                        help="minutes after which a fresh recording file replaces the old one")
    stream.add_argument("--overlap-secs", type=float, default=60,
                        help="seconds the old and the new recording file run side by side at a switch")
    stream.add_argument("--keep-rec", action="store_true", help="keep the recording files instead of deleting them")
    spike = p.add_argument_group("spike detection")
    spike.add_argument("--min", type=int, default=15,
                       help="fewest chat messages in one counting interval that can count as a spike")
    spike.add_argument("--factor", type=float, default=2.5,
                       help="how many times busier than usual chat must be to count as a spike")
    spike.add_argument("--bucket", type=int, default=10, help="length of one counting interval, in seconds")
    spike.add_argument("--window", type=int, default=180,
                       help="how far back the usual chat rate is measured, in seconds")
    spike.add_argument("--warmup", type=float, default=30,
                       help="seconds of chat to see before the first clip can be made")
    clip = p.add_argument_group("clip")
    clip.add_argument("--before", type=float, default=20, help="seconds kept before the spike")
    clip.add_argument("--after", type=float, default=10, help="seconds kept after the spike")
    clip.add_argument("--cooldown", type=float,
                      help="shortest wait between two clips of one channel, in seconds; empty means one clip length")
    clip.add_argument("--ratio", type=ratio, default="9:16", help="shape of the clip as width:height, for example 9:16")
    clip.add_argument("--height", type=int, default=1920,
                      help="clip height in pixels; the width follows from the aspect ratio")
    clip.add_argument("--crf", type=int, default=23,
                      help="picture quality from 0 to 51; lower looks better and makes bigger files")
    clip.add_argument("--cam", default="top", choices=["top", "bottom", "off"],
                      help="where the streamer's cam panel sits; off keeps one picture")
    clip.add_argument("--face-share", type=float, default=1 / 3,
                      help="part of the clip height the cam panel takes, from 0 to 1")
    clip.add_argument("--face-zoom", type=float, default=4,
                      help="room left around the face; a bigger number shows more of the cam")
    clip.add_argument("--big-face", type=float, default=0.12,
                      help="a face wider than this part of the picture gets the whole clip instead of a panel")
    clip.add_argument("--model", default="base",
                      help="speech model for the subtitles: tiny, base, small, medium or large; bigger is slower and more accurate")
    clip.add_argument("--language", help="language spoken on stream, for example en; empty detects it")
    clip.add_argument("--jobs", type=int, default=1,
                      help="clips encoded and transcribed at once, counted over every clip-watcher in this folder")
    yt = p.add_argument_group("YouTube")
    yt.add_argument("--upload", action="store_true", help="upload every clip to YouTube Shorts")
    yt.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"],
                    help="who can see uploaded clips")
    yt.add_argument("--title", default=upload.TITLE,
                    help="title of the upload; {text} is what was said, {channel} the channel name")
    yt.add_argument("--description", default=upload.DESCRIPTION,
                    help="description of the upload; same placeholders as the title")
    return p


def claim_run_folder():
    """Make this run's folder for temporary files, and delete the folders of earlier runs that were killed.

    Each run keeps a lock on its folder for as long as it lives, so a folder whose lock can be taken belongs
    to nobody. Closing the console window gives a run no chance to tidy up, which makes this the usual path.
    """
    global RUN
    REC.mkdir(exist_ok=True)
    CLIPS.mkdir(exist_ok=True)
    freed = 0
    for old in REC.glob("run-*"):
        try:
            abandoned = lock_file(old / "lock") if time.time() - old.stat().st_mtime > 10 else None
            if abandoned:
                size = sum(f.stat().st_size for f in old.iterdir())
                abandoned.close()
                shutil.rmtree(old)
                freed += size
        except OSError:
            pass
    RUN = REC / f"run-{os.getpid()}-{time.time():.0f}"
    RUN.mkdir()
    return lock_file(RUN / "lock"), freed


def control():
    """Commands from the settings page, one per line on stdin: stop, quit, min=<n>, factor=<x>.

    Losing stdin means the page is gone, which also stops us.
    """
    for line in sys.stdin:
        key, _, value = line.strip().partition("=")
        if key == "quit":
            quit_now()
        elif key == "stop":
            STOP.set()  # and keep listening: "quit" may follow
        elif key in ("min", "factor"):
            try:
                TUNE[key] = int(value) if key == "min" else float(value)
            except ValueError:
                pass
    STOP.set()


def main():
    p = build_parser()
    args = p.parse_intermixed_args()
    parsed = [parse_target(text) for text in args.channels]
    if None in parsed:
        p.error("every channel must be a channel name or a twitch.tv / kick.com link, optionally with :<minimum>")
    targets = {}
    for name, site, own in parsed:  # each channel once, in the order given; only a plain name follows --kick
        targets.setdefault((name, site == "kick" if site else args.kick), args.min if own is None else own)
    picked = None
    if args.category:
        if not 1 <= args.top <= 100:
            p.error("--top must be from 1 to 100")
        try:
            picked = live.live(args.category, "kick" if args.kick else "twitch", args.top)
        except live.Problem as e:
            p.error(str(e))
        for found in picked[1]:
            if parse_channel(found["channel"]):
                targets.setdefault((found["channel"].lower(), args.kick), args.min)
    if not targets:
        p.error(f"nobody is live in {picked[0]} right now" if picked else
                "give at least one channel, or a --category to pick the most watched live channels from")
    if min(args.min, *targets.values()) < 1:
        p.error("a minimum must be at least 1")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            p.error(f"{tool} was not found; install ffmpeg and make sure it is on PATH")
    if min(args.bucket, args.factor, args.before + args.after, args.face_zoom, args.jobs, args.rotate_mins) <= 0:
        p.error("--bucket, --factor, --face-zoom, --jobs, --rotate-mins and the clip length must be positive")
    if min(args.before, args.after, args.warmup, args.overlap_secs) < 0:
        p.error("--before, --after, --warmup and --overlap-secs cannot be negative")
    if args.window < args.bucket or args.height < 16 or not 0 <= args.face_share < 1:
        p.error("need --window >= --bucket, --height >= 16 and 0 <= --face-share < 1")
    args.height = even(args.height)
    args.width = max(2, even(args.height * args.ratio))
    args.face_h = 0 if args.cam == "off" else even(args.height * args.face_share)
    try:
        upload.describe("x", "x", args.title, args.description)
    except (KeyError, IndexError, ValueError):
        p.error("--title and --description may only use {text} and {channel}")
    if args.upload:
        upload.youtube()  # sign in now, not in the middle of a stream
    for name, kick in targets:
        if kick:
            try:
                kick_channel_id(name)
            except LookupError as e:
                sys.exit(str(e))
            except Exception:
                pass  # Kick cannot be reached right now; the channel keeps trying on its own

    sys.stdout.reconfigure(errors="replace")  # a helper's odd character must not stop a channel
    STOP.clear()
    TUNE.clear()
    job = die_together() if os.name == "nt" else None  # noqa: F841 - held for as long as the process lives
    held, freed = claim_run_folder()
    if freed:
        log("clip-watcher", f"removed {freed / 1e9:.1f} GB of temporary recordings an earlier run left behind")
    try:
        face_model()
    except OSError as e:
        log("clip-watcher", f"the face detector could not be fetched ({e}); clips get a plain centre crop for now")

    def stop(*_):
        if STOP.is_set():
            quit_now()  # asked twice
        STOP.set()

    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGBREAK", "SIGHUP"):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), stop)
    if args.stdin_control:
        threading.Thread(target=control, daemon=True).start()

    log("clip-watcher", (
        f"spike = {args.factor:g}x the usual chat rate and at least the channel's minimum per {args.bucket} s; "
        f"clips are {args.before + args.after:g} s at {args.width}x{args.height}"
    ))
    if picked:
        names = ", ".join(f"{c['channel']} ({c['viewers']:,})" for c in picked[1]) or "nobody"
        log("clip-watcher", f"live in {picked[0]} right now: {names}")
    workers = []
    for index, ((name, kick), minimum) in enumerate(targets.items()):
        # the tag names the channel in the log and in file names; the same name can be live on both sites
        tag = f"{name}-kick" if kick else name
        workers.append(threading.Thread(target=watch, args=(tag, name, kick, minimum, args, index)))
        workers[-1].start()
    # the main thread only sleeps, so it is always free to take Ctrl+C
    while any(worker.is_alive() for worker in workers):
        time.sleep(0.2)
    clips = [t for t in threading.enumerate() if t.name.startswith("clip ")]
    if clips:
        hint = "" if args.stdin_control else "; Ctrl+C again quits now"
        log("clip-watcher", f"finishing {len(clips)} clip{'s' * (len(clips) != 1)}{hint}")
    while any(t.is_alive() for t in clips):
        time.sleep(0.2)
    held.close()
    shutil.rmtree(RUN, ignore_errors=True)


if __name__ == "__main__":
    main()
