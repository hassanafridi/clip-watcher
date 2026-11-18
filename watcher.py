#!/usr/bin/env python3
"""Record a live Twitch or Kick stream and cut a vertical clip every time chat spikes."""
import _thread
import argparse
import re
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

import upload

RETRY = 5  # first chat reconnect delay, doubles up to 60 s
TWITCH_IRC = ("irc.chat.twitch.tv", 443, True)  # host, port, TLS; 443 gets through firewalls that block 6667
KICK_API = "https://kick.com/api/v2/channels"
KICK_POLL = 2  # seconds between Kick chat polls
GOP_PAD = 4  # extra seconds cut ahead of a clip so it can start on a whole frame

FACE_SCORE = 0.8
FACE_MODEL = Path(__file__).with_name("face_detection_yunet_2023mar.onnx")
FACE_MODEL_URL = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + FACE_MODEL.name

heavy = threading.Lock()  # one encode + transcription at a time


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
    if not r.ok:
        sys.exit(f"Kick channel '{channel}' not found (HTTP {r.status_code})")
    return r.json()["id"]


def kick_session(channel_id):
    import requests

    # polls the public history (newest 25 messages), so counts flatten above
    # 25 messages per KICK_POLL; move to Kick's chat websocket if that ceiling matters
    seen = deque(maxlen=500)
    while True:
        r = requests.get(f"{KICK_API}/{channel_id}/messages", timeout=10)
        if not r.ok:
            raise OSError(f"Kick answered HTTP {r.status_code}")
        new = [m["id"] for m in r.json()["data"]["messages"] if m["id"] not in seen]
        yield len(new) if seen else 0
        seen.extend(new)
        time.sleep(KICK_POLL)


def chat(session, *args):
    """Yield counts of new chat messages forever (0 when idle), reconnecting with backoff."""
    delay = RETRY
    while True:
        try:
            for n in session(*args):
                delay = RETRY
                yield n
        except (OSError, ValueError, KeyError) as e:
            print(f"chat lost ({e}); retrying in {delay} s", file=sys.stderr)
        yield 0
        time.sleep(delay)
        delay = min(delay * 2, 60)


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


def find_face(path):
    """Frame width, frame height and the streamer's face box (or None), sampling one frame per second."""
    import cv2

    if not FACE_MODEL.exists():
        urllib.request.urlretrieve(FACE_MODEL_URL, FACE_MODEL)
    video = cv2.VideoCapture(str(path))
    w, h = int(video.get(cv2.CAP_PROP_FRAME_WIDTH)), int(video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = video.get(cv2.CAP_PROP_FPS)
    step = round(fps) if 1 <= fps <= 120 else 30
    detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (w, h), FACE_SCORE)
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


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", *map(str, args)], check=True)


def make_clip(rec, out, source, args):
    time.sleep(args.after)
    raw = out.with_suffix(".ts")
    length = args.before + args.after
    # the copy starts mid-GOP, so take a few seconds extra and trim exactly while encoding
    ffmpeg("-sseof", -(length + GOP_PAD), "-i", rec, "-c", "copy", raw)
    with heavy:
        graph = layout(*find_face(raw), args)
        ffmpeg("-sseof", -length, "-i", raw, "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
               "-crf", args.crf, out)
        raw.unlink()
        subprocess.run(
            [sys.executable, "-m", "whisper", str(out), "--model", args.model, "--output_format", "srt",
             "--output_dir", str(out.parent), "--verbose", "False", "--fp16", "False",
             *(["--language", args.language] if args.language else [])],
            check=True, stdin=subprocess.DEVNULL,
        )
        print(f"clip ready: {out}")
        if args.upload:
            upload.publish(out, source, args.privacy, args.title, args.description)


def ratio(text):
    m = re.fullmatch(r"(\d+):(\d+)", text)
    if not m or "0" in m.groups():
        raise argparse.ArgumentTypeError("use WIDTH:HEIGHT, for example 9:16")
    return int(m[1]) / int(m[2])


def build_parser():
    p = argparse.ArgumentParser(
        description=__doc__,
        epilog="Options can be kept in a preset file, one per line (--ratio=1:1), and loaded with @file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        fromfile_prefix_chars="@",
    )
    p.add_argument("channel")
    p.add_argument("--stdin-stop", action="store_true", help=argparse.SUPPRESS)  # the GUI stops us by closing stdin
    stream = p.add_argument_group("stream")
    stream.add_argument("--kick", action="store_true", help="the channel is on Kick, not Twitch")
    stream.add_argument("--quality", default="best", help="stream quality to record, e.g. 720p60")
    spike = p.add_argument_group("spike detection")
    spike.add_argument("--min", type=int, default=15, help="fewest messages per bucket that count as a spike")
    spike.add_argument("--factor", type=float, default=2.5, help="spike = this many times the median bucket")
    spike.add_argument("--bucket", type=int, default=10, help="seconds of chat per bucket")
    spike.add_argument("--window", type=int, default=180, help="seconds of history the median is taken over")
    clip = p.add_argument_group("clip")
    clip.add_argument("--before", type=float, default=20, help="seconds kept before the spike")
    clip.add_argument("--after", type=float, default=10, help="seconds kept after the spike")
    clip.add_argument("--cooldown", type=float, help="seconds between clips (default: the clip length)")
    clip.add_argument("--ratio", type=ratio, default="9:16", help="aspect ratio, WIDTH:HEIGHT")
    clip.add_argument("--height", type=int, default=1920, help="clip height in pixels; width follows from --ratio")
    clip.add_argument("--crf", type=int, default=23, help="x264 quality, lower is better and bigger")
    clip.add_argument("--cam", default="top", choices=["top", "bottom", "off"],
                      help="where the streamer's facecam panel goes; off never stacks")
    clip.add_argument("--face-share", type=float, default=1 / 3,
                      help="share of the clip height given to the facecam panel")
    clip.add_argument("--face-zoom", type=float, default=4, help="facecam crop width, in face widths")
    clip.add_argument("--big-face", type=float, default=0.12,
                      help="a face wider than this share of the frame fills the clip instead of getting a panel")
    clip.add_argument("--model", default="base", help="Whisper model for the subtitles")
    clip.add_argument("--language", help="subtitle language, e.g. en (default: detect)")
    yt = p.add_argument_group("YouTube")
    yt.add_argument("--upload", action="store_true", help="upload every clip to YouTube Shorts")
    yt.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    yt.add_argument("--title", default=upload.TITLE, help="title template; {text} = what was said, {channel}")
    yt.add_argument("--description", default=upload.DESCRIPTION, help="description template, same fields")
    return p


def main():
    p = build_parser()
    args = p.parse_args()
    channel = args.channel.lower()
    if not re.fullmatch(r"[a-z0-9_-]{1,30}", channel):
        p.error("channel must be a plain channel name, not a URL")
    if min(args.bucket, args.factor, args.before + args.after, args.face_zoom) <= 0 or args.before < 0 or args.after < 0:
        p.error("--bucket, --factor, --face-zoom and the clip length must be positive")
    if args.window < args.bucket or args.height < 16 or not 0 <= args.face_share < 1:
        p.error("need --window >= --bucket, --height >= 16 and 0 <= --face-share < 1")
    args.height = even(args.height)
    args.width = max(2, even(args.height * args.ratio))
    args.face_h = 0 if args.cam == "off" else even(args.height * args.face_share)
    length, buckets = args.before + args.after, args.window // args.bucket
    cooldown = length if args.cooldown is None else args.cooldown

    try:
        upload.describe("x", channel, args.title, args.description)
    except (KeyError, IndexError, ValueError):
        p.error("--title and --description may only use {text} and {channel}")
    if args.upload:
        upload.youtube()  # sign in now, not in the middle of the stream
    if args.kick:
        source, messages = f"kick.com/{channel}", chat(kick_session, kick_channel_id(channel))
    else:
        source, messages = f"twitch.tv/{channel}", chat(twitch_session, channel)

    Path("rec").mkdir(exist_ok=True)
    Path("clips").mkdir(exist_ok=True)
    rec = Path("rec") / f"{channel}_{time.strftime('%Y%m%d_%H%M%S')}.ts"
    recorder = subprocess.Popen(
        [sys.executable, "-m", "streamlink", f"https://{source}", args.quality, "-O"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
    )
    # remux to MPEG-TS, the one container ffmpeg can cut from while the file is still growing
    subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", "-", "-c", "copy", "-f", "mpegts", str(rec)],
        stdin=recorder.stdout,
    )
    recorder.stdout.close()
    print(
        f"watching {source}: spike = {args.factor:g}x the median of the last {args.window} s and at least "
        f"{args.min} messages per {args.bucket} s; clips are {length:g} s at {args.width}x{args.height}"
    )

    if args.stdin_stop:
        threading.Thread(target=lambda: (sys.stdin.read(), _thread.interrupt_main()), daemon=True).start()

    history, count, last_clip = deque(maxlen=buckets), 0, 0.0
    bucket_end = time.time() + args.bucket
    try:
        for n in messages:
            count += n
            now = time.time()
            if now < bucket_end:
                continue
            if recorder.poll() is not None:
                print("recording stopped (stream offline or ended)")
                break
            if len(history) == buckets and now - last_clip >= cooldown and is_spike(count, history, args.min, args.factor):
                last_clip = now
                out = Path("clips") / f"{channel}_{time.strftime('%Y%m%d_%H%M%S')}.mp4"
                print(f"spike: {count} messages (median {statistics.median(history):g}), cutting {out}")
                threading.Thread(target=make_clip, args=(rec, out, source, args)).start()
            history.append(count)
            count, bucket_end = 0, now + args.bucket
    except KeyboardInterrupt:
        pass
    finally:
        recorder.terminate()


if __name__ == "__main__":
    main()
