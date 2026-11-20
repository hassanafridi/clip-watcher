#!/usr/bin/env python3
"""Draw a short intro and outro, a profile picture and a banner for your channel with ffmpeg, into brand/."""
import argparse
import subprocess
import tempfile
from pathlib import Path

from watcher import quote

HERE = Path(__file__).resolve().parent
W, H, FPS = 1080, 1920, 30
BG, TEXT, MUTED, SPIKE, CAM = "0x141415", "0xededed", "0x85858b", "0xe5484d", "0x4fb3c6"


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


def render(out, seconds, video, audio):
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"color=c={BG}:s={W}x{H}:r={FPS}:d={seconds}",
                    "-f", "lavfi", "-i", audio,
                    "-filter_complex", f"[0:v]{video},format=yuv420p[v]", "-map", "[v]", "-map", "1:a",
                    "-t", str(seconds), "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-ar", "48000", "-ac", "2",
                    "-map_metadata", "-1", str(out)], check=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("name", nargs="?", default="Chat Went Off", help="channel name shown in both")
    p.add_argument("--handle", default="@chatwentoff", help="handle shown in the outro")
    args = p.parse_args()
    folder = HERE / "brand"
    folder.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # intro, 1.2 s: a chat meter fills bar by bar, the last bar spikes red, then the name lands
        bars = []
        for i, height in enumerate([40, 70, 55, 90, 60, 110, 80, 130, 100, 150, 120]):
            x = 184 + i * 56
            bars.append(f"drawbox=x={x}:y={1150 - height}:w=40:h={height}:color={MUTED}:t=fill:enable='gte(t,{i * 0.03:.2f})'")
        for step, height in enumerate([180, 320, 460, 560]):
            bars.append(f"drawbox=x={184 + 11 * 56}:y={1150 - height}:w=40:h={height}:color={SPIKE}:t=fill:"
                        f"enable='between(t,{0.36 + step * 0.05:.2f},{0.41 + step * 0.05 if step < 3 else 9:.2f})'")
        intro = ",".join(bars + [text(tmp, "name", args.name.upper(), 118, TEXT, 1210, 0.55, 0.12),
                                 text(tmp, "line", "the moment chat exploded", 46, MUTED, 1360, 0.7)])
        render(folder / "intro.mp4", 1.2, intro,
               "anoisesrc=d=1.2:c=pink:a=0.3,highpass=f=300,lowpass=f=5000,"
               "afade=t=in:d=0.45,afade=t=out:st=0.5:d=0.7,volume=0.6")
        # outro, 2 s: ask for the follow, and say plainly where the clip is from
        outro = ",".join([
            f"drawbox=x=500:y=640:w=80:h=12:color={SPIKE}:t=fill",
            text(tmp, "name", args.name.upper(), 64, MUTED, 700),
            text(tmp, "ask", "Follow for the next spike", 78, TEXT, 836, 0.1),
            text(tmp, "handle", args.handle, 76, CAM, 960, 0.25),
            text(tmp, "credit", "Moments from live streams.", 42, MUTED, 1120, 0.4),
            text(tmp, "credit2", "The streamer is linked in the description.", 42, MUTED, 1176, 0.4),
        ])
        render(folder / "outro.mp4", 2.0, outro, "anullsrc=r=48000:cl=stereo")
        # profile picture, 800 x 800: the chat meter alone, kept inside the circle YouTube crops it to
        meter = [f"drawbox=x={164 + i * 100}:y={590 - height}:w=72:h={height}:color={SPIKE if i == 4 else MUTED}:t=fill"
                 for i, height in enumerate([90, 150, 110, 190, 380])]
        picture(folder / "avatar.png", 800, 800, ",".join(meter))
        # banner, 2560 x 1440: everything inside the 1546 x 423 middle that shows on every screen
        meter = [f"drawbox=x={560 + i * 40}:y={830 - height}:w=28:h={height}:color={SPIKE if i == 4 else MUTED}:t=fill"
                 for i, height in enumerate([40, 66, 48, 84, 170])]
        picture(folder / "banner.png", 2560, 1440, ",".join(meter + [
            text(tmp, "bname", args.name.upper(), 120, TEXT, 640, -1).replace("x=(w-text_w)/2", "x=800"),
            text(tmp, "bline", "Live stream moments, cut the second chat exploded", 44, MUTED, 790, -1)
            .replace("x=(w-text_w)/2", "x=804")]))
    print(f"made intro.mp4, outro.mp4, avatar.png and banner.png in {folder}")


def picture(out, width, height, video):
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={BG}:s={width}x{height}",
                    "-vf", video, "-frames:v", "1", "-map_metadata", "-1", str(out)], check=True)


if __name__ == "__main__":
    main()
