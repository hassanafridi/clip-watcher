#!/usr/bin/env python3
"""Podcast clips: get a video, and find the moments in it that are worth clipping.

Moments come from loud spots in the sound (laughter, raised voices) and, when a model is set up, from a model
reading the transcript. The model is the user's own: an API key, or a model running on this computer.
"""
import html
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
FOLDER = HERE / "podcasts"  # one folder per video: source.mp4, meta.json, transcript.json, moments.json
LLM_FILE = HERE / "llm.json"
SHORTEST, LONGEST = 8, 90  # seconds a moment may last
WINDOW, STEP = 12 * 60, 11 * 60  # the transcript goes to the model in pieces of this many seconds, this far apart
PRESETS = {  # kind: how the API is spoken; the model names are only defaults, the page lets people change them
    "claude": {"kind": "anthropic", "base": "https://api.anthropic.com", "model": "claude-sonnet-5-5"},
    "openai": {"kind": "openai", "base": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
    "gemini": {"kind": "openai", "base": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.0-flash"},
    "qwen": {"kind": "openai", "base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    "ollama": {"kind": "openai", "base": "http://localhost:11434/v1", "model": "llama3.1"},
    "llamacpp": {"kind": "openai", "base": "http://localhost:8080/v1", "model": "local"},
    "custom": {"kind": "openai", "base": "", "model": ""},
}
PROMPT = """You choose short clips from a podcast for YouTube Shorts and other vertical video. Below is a piece of the \
transcript with time marks as minutes:seconds. Pick up to {n} moments that work on their own with no context: a strong \
claim, a funny exchange, a story with a payoff, a surprising fact, a heated disagreement. Each clip lasts 20 to 60 \
seconds, starts where a thought begins and ends where it lands.

Answer with a JSON array and nothing else. Each item: {{"start": seconds, "end": seconds, "peak": seconds, \
"title": "a hook under 70 characters", "why": "one short sentence", "score": a number from 1 to 10}}. Times are seconds \
from the start of the video, so 12:30 is 750. peak is the second the moment lands.

Transcript:
{text}"""
TIME = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{3})")


class Problem(Exception):
    """Something the person can fix or should know, in plain words."""


def tool(name):
    if not shutil.which(name):
        raise Problem(f"{name} was not found; install ffmpeg and make sure it is on PATH")


# ---- the model

def load_llm():
    """The saved model settings, or None."""
    try:
        cfg = json.loads(LLM_FILE.read_text(encoding="utf-8"))
        assert isinstance(cfg, dict) and cfg.get("preset") in PRESETS and cfg.get("base") and cfg.get("model") is not None
        return {"preset": cfg["preset"], "base": str(cfg["base"]), "model": str(cfg["model"]), "key": str(cfg.get("key") or "")}
    except (OSError, ValueError, AssertionError):
        return None


def public_llm():
    """What the page may see: never the key itself."""
    cfg = load_llm()
    return {"preset": cfg["preset"], "base": cfg["base"], "model": cfg["model"], "has_key": bool(cfg["key"])} if cfg else None


def save_llm(preset, base, model, key):
    """Keep the settings in llm.json; an empty key keeps the one already saved, "-" removes it."""
    if preset not in PRESETS:
        raise Problem("Pick one of the listed providers")
    base, model, key = str(base or "").strip().rstrip("/"), str(model or "").strip(), str(key or "").strip()
    if not re.fullmatch(r"https?://[^\s]+", base):
        raise Problem("The address must start with http:// or https://")
    if not model:
        raise Problem("Enter the model name")
    old = load_llm()
    key = (old["key"] if old else "") if not key else "" if key == "-" else key
    fresh = LLM_FILE.with_name(LLM_FILE.name + ".tmp")
    fresh.write_text(json.dumps({"preset": preset, "base": base, "model": model, "key": key}, indent=1), encoding="utf-8")
    os.replace(fresh, LLM_FILE)


def forget_llm():
    LLM_FILE.unlink(missing_ok=True)


def is_local(cfg):
    """True for a model on this computer or this network: loading and answering can take minutes."""
    return cfg["preset"] in ("ollama", "llamacpp") or re.match(r"https?://(localhost|127\.|10\.|192\.168\.)", cfg["base"]) is not None


def ask(cfg, prompt, timeout=None):
    """One question to the model; returns its answer as text. Waits up to 15 minutes for a local model, 4 for a service."""
    timeout = timeout or (900 if is_local(cfg) else 240)
    anthropic = PRESETS[cfg["preset"]]["kind"] == "anthropic"
    if anthropic:
        url = cfg["base"].rstrip("/") + "/v1/messages"
        headers = {"x-api-key": cfg["key"], "anthropic-version": "2023-06-01"}
        body = {"model": cfg["model"], "max_tokens": 4096, "messages": [{"role": "user", "content": prompt}]}
    else:
        url = cfg["base"].rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {}
        body = {"model": cfg["model"], "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]}
    request = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            data = json.load(answer)
        return data["content"][0]["text"] if anthropic else data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        raise Problem(f"The model service answered {e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Problem(f"Could not reach {cfg['base']}: {getattr(e, 'reason', e)}") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise Problem("The model service answered in a form this program does not know") from None


def check_llm(cfg):
    """Ask something small; returns the model's reply, or raises Problem."""
    return ask(cfg, "Reply with the single word ready.", timeout=180 if is_local(cfg) else 60).strip()[:80]


# ---- the transcript

def clean(line):
    return html.unescape(re.sub(r"<[^>]+>", "", line)).strip()


def seconds(text):
    m = TIME.search(text)
    return int(m[1] or 0) * 3600 + int(m[2]) * 60 + int(m[3]) + int(m[4]) / 1000 if m else None


def parse_cues(text):
    """Segments {start, end, text} out of WebVTT or SRT text. YouTube's automatic captions repeat each line in the next
    cue as they roll up the screen; every line is kept once."""
    out, before = [], set()
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = block.strip().split("\n")
        at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if at is None:
            continue
        a, _, b = lines[at].partition("-->")
        start, end = seconds(a), seconds(b)
        if start is None or end is None:
            continue
        said = [x for x in (clean(line) for line in lines[at + 1:]) if x]
        fresh = [x for x in said if x not in before]
        before = set(said)
        if fresh:
            out.append({"start": round(start, 2), "end": round(end, 2), "text": " ".join(fresh)})
    return out


def transcribe(media, folder, model="base", language=None, say=print):
    """Speech to text with whisper, for a video that has no captions of its own."""
    wav = folder / "speech.wav"
    say("No captions with the video; listening to it (this takes a while)")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    command = [sys.executable, "-m", "whisper", str(wav), "--model", model, "--output_format", "srt", "--output_dir", str(folder),
               "--verbose", "False", "--fp16", "False", "--condition_on_previous_text", "False"]
    if language:
        command += ["--language", language]
    done = subprocess.run(command, capture_output=True, text=True, errors="replace")
    wav.unlink(missing_ok=True)
    srt = folder / "speech.srt"
    if done.returncode or not srt.exists():
        said = [line for line in done.stderr.strip().splitlines() if line.strip() and "%|" not in line]
        raise Problem("The speech could not be turned into text: " + (said[-1][:200] if said else "whisper stopped early"))
    try:
        return parse_cues(srt.read_text(encoding="utf-8"))
    finally:
        srt.unlink(missing_ok=True)


def get_transcript(folder, media, model="base", language=None, say=print):
    """The transcript: from the captions that came with the video, else by listening. Kept in transcript.json."""
    kept = folder / "transcript.json"
    try:
        return json.loads(kept.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    subs = sorted(folder.glob("source*.vtt"), key=lambda p: (".en." not in p.name, p.name))
    segments = parse_cues(subs[0].read_text(encoding="utf-8", errors="replace")) if subs else []
    if not segments:
        segments = transcribe(media, folder, model, language, say)
    kept.write_text(json.dumps(segments), encoding="utf-8")
    return segments


# ---- loud moments

def loud_moments(media, duration, step=5, factor=1.8, floor=0.02, want=None):
    """Spots where the sound is much louder than around them: {peak, score}, the loudest first.

    The sound is measured in windows of `step` seconds; a window counts when its level is `factor` times the middle
    level of the five minutes around it and above `floor` (of full scale).
    """
    import numpy as np

    rate = 8000
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(media), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    levels = []
    try:
        while True:
            data = proc.stdout.read(step * rate * 2)
            if len(data) < rate:  # less than a second left: not worth a window
                break
            levels.append(float(np.sqrt(np.mean(np.square(np.frombuffer(data[:len(data) // 2 * 2], dtype="<i2") / 32768.0)))))
    finally:
        proc.stdout.close()
        proc.wait()
    around = max(3, 300 // step)
    found = []
    for i, level in enumerate(levels):
        usual = statistics.median(levels[max(0, i - around // 2):i + around // 2 + 1])
        if level >= floor and level >= factor * max(usual, 1e-4):
            found.append({"peak": i * step + step / 2, "score": level / max(usual, 1e-4)})
    # neighbouring windows are one moment
    merged = []
    for f in found:
        if merged and f["peak"] - merged[-1]["last"] <= step:
            merged[-1]["last"] = f["peak"]
            if f["score"] > merged[-1]["score"]:
                merged[-1].update(peak=f["peak"], score=f["score"])
        else:
            merged.append({**f, "last": f["peak"]})
    merged.sort(key=lambda f: f["score"], reverse=True)
    return [{"peak": m["peak"], "score": round(m["score"], 2)} for m in merged[:want or max(5, int(duration // 300))]]


# ---- picking moments

def clamp(start, end, peak, duration):
    """A moment inside the video and within the allowed length, or None."""
    start, end = max(0.0, float(start)), min(float(end), float(duration) if duration else float(end))
    if end - start > LONGEST:
        end = start + LONGEST
    if end - start < SHORTEST:
        return None
    peak = min(max(float(peak), start), end)
    return round(start, 1), round(end, 1), round(peak, 1)


def overlap(a, b):
    """How much of the shorter moment lies inside the other, 0 to 1."""
    both = min(a["end"], b["end"]) - max(a["start"], b["start"])
    return max(0.0, both) / max(1.0, min(a["end"] - a["start"], b["end"] - b["start"]))


def distinct(moments):
    """The best-scored first, dropping any that mostly repeats one already kept."""
    kept = []
    for m in sorted(moments, key=lambda m: m.get("score", 0), reverse=True):
        if all(overlap(m, k) < 0.5 for k in kept):
            kept.append(m)
    return sorted(kept, key=lambda m: m["start"])


def mmss(t):
    return f"{int(t) // 60}:{int(t) % 60:02d}"


def read_answer(text):
    """The list of moments in a model's answer; Problem when there is none."""
    try:
        items = json.loads(text[text.index("["):text.rindex("]") + 1])
        assert isinstance(items, list)
    except (ValueError, AssertionError):
        raise Problem("The model did not answer with a list of moments") from None
    return [i for i in items if isinstance(i, dict)]


def ai_moments(cfg, segments, duration, say=print):
    """Ask the model, one transcript window at a time, for moments worth clipping."""
    out, begin = [], 0.0
    end_of_talk = max((s["end"] for s in segments), default=0.0)
    while begin < max(end_of_talk, 1):
        piece = [s for s in segments if begin <= s["start"] < begin + WINDOW]
        if piece:
            say(f"Asking the model about {mmss(begin)} to {mmss(min(begin + WINDOW, end_of_talk))}")
            text = "\n".join(f"[{mmss(s['start'])}] {s['text']}" for s in piece)
            for item in read_answer(ask(cfg, PROMPT.format(n=max(2, WINDOW // 60 // 4), text=text))):
                try:
                    got = clamp(item["start"], item["end"], item.get("peak", item["start"]), duration)
                    if got:
                        out.append({"start": got[0], "end": got[1], "peak": got[2], "by": "ai",
                                    "title": str(item.get("title") or "")[:100], "why": str(item.get("why") or "")[:200],
                                    "score": min(max(float(item.get("score", 5)), 0), 10)})
                except (KeyError, TypeError, ValueError):
                    continue
        begin += STEP
    return distinct(out)


def find_moments(folder, media, duration, cfg=None, model="base", language=None, say=print):
    """Every moment worth a look, kept in moments.json: the model's picks first, then the loud spots they miss."""
    segments = get_transcript(folder, media, model, language, say)
    picks, problem = [], ""
    if cfg:
        try:
            picks = ai_moments(cfg, segments, duration, say)
        except Problem as e:
            problem = str(e)
            say(f"The model could not help: {e}")
    say("Listening for loud moments")
    loud = []
    for l in loud_moments(media, duration):
        got = clamp(l["peak"] - 20, l["peak"] + 10, l["peak"], duration)
        if got:
            near = " ".join(s["text"] for s in segments if got[0] <= s["start"] <= got[1])
            loud.append({"start": got[0], "end": got[1], "peak": got[2], "by": "loud", "score": min(10.0, l["score"]),
                         "title": " ".join(near.split()[:9]), "why": "much louder than the talk around it"})
    mixed = picks + [l for l in loud if all(overlap(l, p) < 0.5 for p in picks)]
    moments = [{**m, "id": i + 1} for i, m in enumerate(mixed)]
    (folder / "moments.json").write_text(json.dumps(moments), encoding="utf-8")
    return moments, problem


# ---- getting the video

def fetch(url, folder, say=print):
    """Download a video page's video, then its captions if it has them. Returns the info yt-dlp kept."""
    out = ["-o", str(folder / "source.%(ext)s"), "--no-playlist"]
    command = [sys.executable, "-m", "yt_dlp", *out, "--newline", "--write-info-json", "--merge-output-format", "mp4",
               "-f", "bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[vcodec^=avc1][height<=1080]/b[height<=1080]/b", url]
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    tail, shown = [], -10
    for line in proc.stdout:
        tail = (tail + [line.strip()])[-6:]
        got = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if got and float(got[1]) >= shown + 10:
            shown = float(got[1])
            say(f"Downloading {shown:.0f}%")
    proc.wait()
    media = folder / "source.mp4"
    if not media.is_file():
        reason = next((t for t in reversed(tail) if t.startswith("ERROR")), tail[-1] if tail else "yt-dlp gave no reason")
        raise Problem(f"The video could not be downloaded: {reason[:240]}")
    try:
        info = json.loads((folder / "source.info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    lang = re.sub(r"[^A-Za-z-]", "", str(info.get("language") or "en")) or "en"
    # the captions are a separate, best-effort run: a service that refuses them must not cost the video
    subprocess.run([sys.executable, "-m", "yt_dlp", *out, "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs",
                    f"{lang},{lang}-orig", "--sub-format", "vtt", url], capture_output=True, text=True, errors="replace")
    return info


def length_of(media):
    """Length of a media file in seconds."""
    tool("ffprobe")
    done = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(media)],
                          capture_output=True, text=True)
    try:
        return float(done.stdout.strip())
    except ValueError:
        raise Problem("That file is not a video this program can read") from None
