import re
from pathlib import Path

from types import SimpleNamespace

from upload import describe, transcript
from watcher import is_spike, layout, ratio, steady_face

BIG_FACE = 0.12

quiet = [4] * 18
assert is_spike(10, quiet, 10, 2.5)
assert not is_spike(9, quiet, 10, 2.5), "below --min"
assert not is_spike(20, [10] * 18, 5, 2.5), "under 2.5x the median"
assert is_spike(20, [10] * 18, 5, 2), "--factor lowers the bar"
assert is_spike(5, [0] * 18, 5, 2.5), "dead chat: --min alone decides"

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

assert transcript(Path("missing.srt")) == ""
assert describe("", "somechannel") == ("live moment | somechannel", "")
assert describe("that was <insane>", "abc") == ("that was insane | abc", "that was insane")
long = ("word " * 40).strip()
title, description = describe(long, "abc", "{channel}: {text}", "{text} #clips")
assert title == "abc: " + ("word " * 12).strip() and len(title) <= 100, title
assert description == long + " #clips"

import json
import threading
import urllib.error
import urllib.request

import gui
import watcher

httpd, token = gui.make_server()
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def call(path, body=None, sent_token=token, host=None):
    headers = {"X-Token": sent_token, **({"Host": host} if host else {})}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.loads(r.read()) if "json" in r.headers["Content-Type"] else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


status, page = call("/", sent_token="")
assert status == 200 and token.encode() in page and b"__TOKEN__" not in page, "the served page carries the token"
assert call("/", host="evil.example")[0] == 403, "the token is not handed to a rebound hostname"
assert call("/api/options", sent_token="wrong")[0] == 403, "another website cannot drive the server"
assert call("/api/options", host="evil.example")[0] == 403, "DNS rebinding"
assert call("/api/start", {"channel": "ok", "args": []}, sent_token="")[0] == 403
offered = {o["flag"] for g in call("/api/options")[1]["groups"] for o in g["options"]}
flags = {a.option_strings[0] for a in watcher.build_parser()._actions if a.option_strings}
assert offered == flags - {"-h", "--stdin-stop"}, "the page must offer every option"
for bad in ({"channel": "@secrets.txt", "args": []}, {"channel": "https://twitch.tv/x", "args": []},
            {"channel": "ok", "args": ["--stdin-stop"]}, {"channel": "ok", "args": ["--nope=1"]},
            {"channel": "ok", "args": ["@file"]}, {"channel": "ok", "args": [5]}):
    assert call("/api/start", bad)[0] == 400, bad
assert call("/api/state")[1]["running"] is False, "nothing above may have started a watcher"
httpd.shutdown()

print("ok")
