#!/usr/bin/env python3
"""Draw a short intro and outro, a profile picture and a banner for a channel with ffmpeg."""
import argparse
import re
import subprocess
import tempfile
from pathlib import Path

from watcher import quote

HERE = Path(__file__).resolve().parent
W, H, FPS = 1080, 1920, 30
# ink: the background; light: the big words; accent: the spike; second: the handle
COLORS = {"ink": "#141415", "light": "#EDEDED", "accent": "#E5484D", "second": "#4FB3C6"}


def hexcolor(value, fallback):
    """#RRGGBB as the 0xRRGGBB ffmpeg wants; the fallback when it is not one."""
    return "0x" + (value if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(value)) else fallback).lstrip("#").upper()


def font():
    """A bold font: Arial Bold on Windows, otherwise whatever fontconfig calls a bold sans."""
    bold = Path("C:/Windows/Fonts/arialbd.ttf")
    return f"fontfile={quote(bold)}" if bold.exists() else "font='DejaVu Sans:style=Bold'"


def text(folder, name, words, size, color, y, start=0.0, fade=0.15):
    """drawtext for one centred line that fades in at `start`."""
    path = Path(folder) / f"{name}.txt"
    path.write_text(words, encoding="utf-8")
    alpha = f"min(1,max(0,(t-{start})/{fade}))"
    return (f"drawtext=textfile={quote(path)}:expansion=none:{font()}:fontsize={size}:fontcolor={color}:"
            f"x=(w-text_w)/2:y={y}:alpha='{alpha}'")


def render(out, seconds, video, audio, ink):
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={ink}:s={W}x{H}:r={FPS}:d={seconds}",
                    "-f", "lavfi", "-i", audio,
                    "-filter_complex", f"[0:v]{video},format=yuv420p[v]", "-map", "[v]", "-map", "1:a",
                    "-t", str(seconds), "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    "-map_metadata", "-1", str(out)], check=True)


def picture(out, width, height, video, ink):
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={ink}:s={width}x{height}",
                    "-vf", video, "-frames:v", "1", "-map_metadata", "-1", str(out)], check=True)


def make(name, handle, folder, colors=None, tagline="the best moments", credit="Clips of live moments."):
    """Draw intro.mp4, outro.mp4, avatar.png and banner.png for a channel into `folder`."""
    colors = {**COLORS, **(colors or {})}
    ink, light, accent, second = (hexcolor(colors[k], COLORS[k]) for k in ("ink", "light", "accent", "second"))
    muted = "0x85858B"
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # intro, 1.2 s: a meter fills bar by bar, the last bar spikes, then the name lands
        bars = []
        for i, height in enumerate([40, 70, 55, 90, 60, 110, 80, 130, 100, 150, 120]):
            x = 184 + i * 56
            bars.append(f"drawbox=x={x}:y={1150 - height}:w=40:h={height}:color={muted}:t=fill:enable='gte(t,{i * 0.03:.2f})'")
        for step, height in enumerate([180, 320, 460, 560]):
            bars.append(f"drawbox=x={184 + 11 * 56}:y={1150 - height}:w=40:h={height}:color={accent}:t=fill:"
                        f"enable='between(t,{0.36 + step * 0.05:.2f},{0.41 + step * 0.05 if step < 3 else 9:.2f})'")
        shown = name.upper()
        size = max(40, min(118, round(1000 / max(len(shown), 1) * 1.9)))
        intro = ",".join(bars + [text(tmp, "name", shown, size, light, 1210, 0.55, 0.12),
                                 text(tmp, "line", tagline, 46, muted, 1360, 0.7)])
        render(folder / "intro.mp4", 1.2, intro,
               "anoisesrc=d=1.2:c=pink:a=0.3,highpass=f=300,lowpass=f=5000,afade=t=in:d=0.45,afade=t=out:st=0.5:d=0.7,volume=0.6",
               ink)
        # outro, 2 s: ask for the follow, and say plainly where the clip is from
        outro = ",".join([
            f"drawbox=x=500:y=640:w=80:h=12:color={accent}:t=fill",
            text(tmp, "name", shown, min(64, size), muted, 700),
            text(tmp, "ask", "Follow for more", 78, light, 836, 0.1),
            text(tmp, "handle", handle, min(76, round(1000 / max(len(handle), 1) * 1.9)), second, 960, 0.25),
            text(tmp, "credit", credit, 42, muted, 1120, 0.4),
            text(tmp, "credit2", "Sources are linked in the description.", 42, muted, 1176, 0.4),
        ])
        render(folder / "outro.mp4", 2.0, outro, "anullsrc=r=48000:cl=stereo", ink)
        # profile picture, 800 x 800: the meter alone, kept inside the circle YouTube crops it to
        meter = [f"drawbox=x={164 + i * 100}:y={590 - height}:w=72:h={height}:color={accent if i == 4 else muted}:t=fill"
                 for i, height in enumerate([90, 150, 110, 190, 380])]
        picture(folder / "avatar.png", 800, 800, ",".join(meter), ink)
        # banner, 2560 x 1440: everything inside the 1546 x 423 middle that shows on every screen
        meter = [f"drawbox=x={560 + i * 40}:y={830 - height}:w=28:h={height}:color={accent if i == 4 else muted}:t=fill"
                 for i, height in enumerate([40, 66, 48, 84, 170])]
        picture(folder / "banner.png", 2560, 1440, ",".join(meter + [
            text(tmp, "bname", shown, min(120, round(1500 / max(len(shown), 1) * 1.9)), light, 640, -1)
            .replace("x=(w-text_w)/2", "x=800"),
            text(tmp, "bline", tagline, 44, muted, 790, -1).replace("x=(w-text_w)/2", "x=804")]), ink)
    return {kind: folder / f"{kind}{ext}" for kind, ext in (("intro", ".mp4"), ("outro", ".mp4"), ("avatar", ".png"), ("banner", ".png"))}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("name", help="channel name shown in all of them")
    p.add_argument("--handle", default="", help="handle shown in the outro, for example @mychannel")
    p.add_argument("--out", default="brand", help="folder the files are written to")
    p.add_argument("--tagline", default="the best moments", help="the line under the name in the intro and banner")
    for key, value in COLORS.items():
        p.add_argument(f"--{key}", default=value, help=f"{key} colour as #RRGGBB")
    args = p.parse_args()
    made = make(args.name, args.handle or "@" + re.sub(r"\W", "", args.name).lower(), HERE / args.out,
                {key: getattr(args, key) for key in COLORS}, args.tagline)
    print("made " + ", ".join(path.name for path in made.values()) + f" in {HERE / args.out}")


if __name__ == "__main__":
    main()
