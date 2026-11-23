"""Self-check for podcast.py: python test_podcast.py prints ok when everything holds."""
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import podcast

VTT = """WEBVTT
Kind: captions

00:00:01.000 --> 00:00:03.000
so the first thing is

00:00:03.000 --> 00:00:03.010
so the first thing is

00:00:03.010 --> 00:00:05.000
so the first thing is
<c>that nobody</c> &amp; nothing

00:00:05.000 --> 00:00:05.010
that nobody &amp; nothing

00:00:05.010 --> 00:01:06.500
that nobody &amp; nothing
and then it ended
"""
cues = podcast.parse_cues(VTT)
assert [c["text"] for c in cues] == ["so the first thing is", "that nobody & nothing", "and then it ended"], cues
assert cues[2]["start"] == 5.01 and cues[2]["end"] == 66.5
srt = podcast.parse_cues("1\n00:00:01,500 --> 00:00:03,000\nhello there\n\n2\n01:00:00,000 --> 01:00:02,000\nlate\n")
assert [(c["start"], c["text"]) for c in srt] == [(1.5, "hello there"), (3600.0, "late")], srt
assert podcast.parse_cues("") == [] and podcast.parse_cues("nothing like captions") == []

# --- moments: lengths, overlaps, the model's answer
assert podcast.clamp(10, 200, 50, 300) == (10.0, 100.0, 50.0), "a moment is cut to 90 s"
assert podcast.clamp(10, 15, 12, 300) is None, "too short"
assert podcast.clamp(290, 400, 295, 300) is None or podcast.clamp(290, 400, 295, 300)[1] == 300.0
assert podcast.clamp(-5, 30, 100, 300) == (0.0, 30.0, 30.0), "inside the video"
a, b, c = ({"start": 0, "end": 30, "score": 5}, {"start": 5, "end": 33, "score": 9}, {"start": 100, "end": 130, "score": 1})
assert podcast.distinct([a, b, c]) == [b, c], "the better of two that mostly repeat each other"
assert podcast.mmss(754) == "12:34"
assert podcast.read_answer('Here you go:\n```json\n[{"start": 1}]\n```')[0]["start"] == 1
for bad in ("no list here", "[not json]", "{}"):
    try:
        podcast.read_answer(bad)
        raise AssertionError(bad)
    except podcast.Problem:
        pass

# --- the model, spoken to over HTTP: both kinds of service, and the ways they fail
seen = []
ANSWER = json.dumps([
    {"start": 40, "end": 80, "peak": 60, "title": "He says the quiet part", "why": "a claim", "score": 9},
    {"start": 45, "end": 78, "peak": 70, "title": "same moment", "why": "repeat", "score": 4},
    {"start": 5, "end": 9, "peak": 6, "title": "too short", "why": "x", "score": 10},
    {"start": 400, "end": 440, "peak": 420, "title": "second piece", "why": "story", "score": 7},
    {"start": "x"}, "junk"])


class Fake(BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if Fake.mode == "401":
            data, code = b'{"error": "bad key"}', 401
        elif Fake.mode == "weird":
            data, code = b'{"unexpected": 1}', 200
        elif self.path.endswith("/v1/messages"):
            data, code = json.dumps({"content": [{"type": "text", "text": ANSWER}]}).encode(), 200
        else:
            data, code = json.dumps({"choices": [{"message": {"content": ANSWER}}]}).encode(), 200
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


server = HTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_port}"
segments = [{"start": float(t), "end": float(t) + 4, "text": f"line {t}"} for t in range(0, 1000, 10)]
try:
    folder = Path(tempfile.mkdtemp())
    podcast.LLM_FILE = folder / "llm.json"
    assert podcast.load_llm() is None and podcast.public_llm() is None
    for preset, kind_path in (("claude", "/v1/messages"), ("ollama", "/chat/completions")):
        seen.clear()
        cfg = {"preset": preset, "base": base, "model": "m1", "key": "sk-secret" if preset == "claude" else ""}
        moments = podcast.ai_moments(cfg, segments, 1000)
        assert [m["start"] for m in moments] == [40.0, 400.0], (preset, moments)
        assert moments[0]["title"] == "He says the quiet part" and moments[0]["by"] == "ai"
        assert seen[0][0] == kind_path and seen[0][2]["model"] == "m1"
        if preset == "claude":
            assert seen[0][1]["x-api-key"] == "sk-secret" and seen[0][1]["anthropic-version"], "Claude is asked with its own header"
        else:
            assert "authorization" not in seen[0][1], "a local model needs no key"
        assert "[1:00] line 60" in seen[0][2]["messages"][0]["content"], "the transcript goes to the model with time marks"
        assert len(seen) == 2, "1000 s of talk is two windows"
    cfg["key"] = "k"
    assert podcast.ask({**cfg, "preset": "openai"}, "x") and seen[-1][1]["authorization"] == "Bearer k"
    Fake.mode = "401"
    for fn in (lambda: podcast.ask(cfg, "x"), lambda: podcast.check_llm(cfg)):
        try:
            fn()
            raise AssertionError("a refused key is a problem")
        except podcast.Problem as e:
            assert "401" in str(e) and "bad key" in str(e) and "sk-" not in str(e)
    Fake.mode = "weird"
    try:
        podcast.ask(cfg, "x")
        raise AssertionError
    except podcast.Problem as e:
        assert "form" in str(e)
    try:
        podcast.ask({**cfg, "base": "http://127.0.0.1:9"}, "x", timeout=3)
        raise AssertionError
    except podcast.Problem as e:
        assert "Could not reach" in str(e)
    Fake.mode = "ok"

    # --- settings: the key stays on this computer and never goes back to the page
    podcast.save_llm("claude", base + "/", "m2", "sk-secret")
    assert podcast.load_llm() == {"preset": "claude", "base": base, "model": "m2", "key": "sk-secret"}
    assert podcast.public_llm() == {"preset": "claude", "base": base, "model": "m2", "has_key": True}
    assert "sk-secret" not in json.dumps(podcast.public_llm())
    podcast.save_llm("claude", base, "m3", "")
    assert podcast.load_llm()["key"] == "sk-secret", "an empty key keeps the saved one"
    podcast.save_llm("claude", base, "m3", "-")
    assert podcast.load_llm()["key"] == "" and podcast.public_llm()["has_key"] is False
    for args in (("nope", base, "m", ""), ("claude", "ftp://x", "m", ""), ("claude", base, "", "")):
        try:
            podcast.save_llm(*args)
            raise AssertionError(args)
        except podcast.Problem:
            pass
    podcast.LLM_FILE.write_text("{broken", encoding="utf-8")
    assert podcast.load_llm() is None, "a damaged file is no settings"
    podcast.forget_llm()
    assert not podcast.LLM_FILE.exists()

    # --- loud moments in a real sound file: quiet talk with one shout at 100 s
    talk = folder / "talk.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:r=10:d=180", "-f", "lavfi", "-i",
                    "anoisesrc=d=180:a=0.02:r=16000", "-af", "volume='if(between(t,100,108),25,1)':eval=frame",
                    "-c:v", "libx264", "-c:a", "aac", "-shortest", str(talk)], check=True)
    assert abs(podcast.length_of(talk) - 180) < 1
    loud = podcast.loud_moments(talk, 180)
    assert len(loud) == 1 and 100 <= loud[0]["peak"] <= 110, loud
    flat = folder / "flat.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=60:a=0.1:r=16000", str(flat)], check=True)
    assert podcast.loud_moments(flat, 60) == [], "steady sound has no peaks"

    # --- all of it together: captions in the folder, a model that answers, the loud spot
    work = folder / "one"
    work.mkdir()
    (work / "source.en.vtt").write_text("WEBVTT\n\n00:01:40.000 --> 00:01:45.000\nhe shouted something\n", encoding="utf-8")
    podcast.save_llm("ollama", base, "m", "")
    moments, problem = podcast.find_moments(work, talk, 180, podcast.load_llm())
    assert problem == "" and [m["by"] for m in moments] == ["ai", "loud"], moments
    assert moments[1]["start"] <= 105 <= moments[1]["end"] and "shouted" in moments[1]["title"], "the shout, with what was said"
    assert [m["id"] for m in moments] == list(range(1, len(moments) + 1))
    assert json.loads((work / "moments.json").read_text(encoding="utf-8")) == moments
    assert (work / "transcript.json").exists()
    Fake.mode = "401"
    moments, problem = podcast.find_moments(work, talk, 180, podcast.load_llm())
    assert "401" in problem and moments and all(m["by"] == "loud" for m in moments), "a failing model costs only its picks"
    moments, problem = podcast.find_moments(work, talk, 180, None)
    assert problem == "" and moments and all(m["by"] == "loud" for m in moments)
finally:
    server.shutdown()

print("ok")
