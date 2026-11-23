#!/usr/bin/env python3
"""Installer for clip-watcher. Needs only Python 3.10 or newer; uses nothing outside the standard library.

    python install.py                 install into ~/clip-watcher (asks first), then open the board in your browser
    python install.py --dir D:\\clips  install anywhere
    python install.py                 run again in an installed folder or with --dir to update it; your clips,
                                      channels, streams, sign-ins and settings are never touched
    python install.py --check         test an install without changing it

The board itself is a page in your browser, served by the program from this computer only.
"""
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ZIP = "https://github.com/hassanafridi/clip-watcher/archive/refs/heads/main.zip"
PROGRAM = ("watcher.py", "gui.py", "gui.html", "upload.py", "live.py", "brand.py", "podcast.py", "install.py", "watch.bat",
           "watch.sh", "requirements.txt", "README.md", "fonts/Anton-Regular.ttf", "fonts/OFL.txt")  # replaced on every update
SEED = ("chat_filter.txt",)  # copied once; after that it is the user's own list
MIN_PYTHON = (3, 10)
FFMPEG_ZIP = ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
              "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip")
FFMPEG_TAR = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{arch}-static.tar.xz"
TORCH_CPU = "https://download.pytorch.org/whl/cpu"
WINDOWS = os.name == "nt"
PY_HELP = {"nt": "Install it from https://www.python.org/downloads/ or with: winget install Python.Python.3.12",
           "posix": "Install it with your package manager (for example: sudo apt install python3 python3-venv) or from "
                    "https://www.python.org/downloads/"}


class Stop(Exception):
    """A problem the person can fix, said in plain words."""


def say(text=""):
    print(text, flush=True)


def run(command, cwd=None, quiet=False):
    """Run a program, showing its output unless quiet; Stop with its last words when it fails."""
    done = subprocess.run([str(c) for c in command], cwd=cwd, text=True, errors="replace",
                          stdout=subprocess.PIPE if quiet else None, stderr=subprocess.STDOUT if quiet else None)
    if done.returncode:
        last = (done.stdout or "").strip().splitlines()[-1:] or [f"exit code {done.returncode}"]
        raise Stop(f"{Path(str(command[0])).name} failed: {last[0][:200]}")


def ask(question, default, args):
    """A yes/no question; the default when unattended or asked not to ask."""
    if args.yes or not sys.stdin.isatty():
        return default
    answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def fetch(urls, target, tries=2):
    """Download a file over https from the first of the links that works, each tried `tries` times, showing progress."""
    urls = [urls] if isinstance(urls, str) else list(urls)
    for url in urls:
        for attempt in range(tries):
            try:
                return download(url, target)
            except OSError as e:
                say(f"  failed ({getattr(e, 'reason', e)})" + ("; trying again" if attempt + 1 < tries else ""))
    raise Stop(f"Could not download {urls[0]}. Check the connection and run this again.")


def download(url, target):
    shown = [-1]

    def hook(blocks, size, total):
        if total > 0 and blocks * size * 10 // total != shown[0]:
            shown[0] = min(10, blocks * size * 10 // total)
            say(f"  {shown[0] * 10}% of {total / 1e6:.0f} MB")

    urllib.request.urlretrieve(url, target, hook)


def check_python():
    if sys.version_info < MIN_PYTHON:
        raise Stop(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is needed; this is {platform.python_version()}. "
                   + PY_HELP[os.name])
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        raise Stop("Run this with the system Python, not from inside another virtual environment")


def source_folder(source, work, dest):
    """A folder holding the program files: the one this file sits in (unless that is the install itself, which then
    updates from GitHub), a folder, or a zip (a path or an https link)."""
    source = source or (str(HERE) if (HERE / "watcher.py").is_file() and HERE != dest else REPO_ZIP)
    if source.startswith("https://"):
        say("Downloading clip-watcher")
        fetch(source, work / "source.zip")
        source = str(work / "source.zip")
    elif source.startswith("http://"):
        raise Stop("A source link must be https://")
    path = Path(source)
    if path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            z.extractall(work / "source")
        found = next((p.parent for p in (work / "source").rglob("watcher.py")), None)
        if not found:
            raise Stop("That zip holds no clip-watcher")
        return found
    if (path / "watcher.py").is_file():
        return path.resolve()
    raise Stop(f"No clip-watcher found at {source}")


def copy_program(src, dest):
    if src.resolve() == dest.resolve():
        return
    for rel in PROGRAM:
        if not (src / rel).is_file():
            raise Stop(f"The source lacks {rel}; it is not a complete clip-watcher")
    for rel in PROGRAM:
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, dest / rel)
    for rel in SEED:
        if (src / rel).is_file() and not (dest / rel).exists():
            shutil.copy2(src / rel, dest / rel)
    if not WINDOWS:
        (dest / "watch.sh").chmod(0o755)


def venv_python(dest):
    return dest / "venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def make_venv(dest):
    """The program's own Python in venv/; rebuilt when the old one no longer runs or is another Python version."""
    vp = venv_python(dest)
    if vp.exists():
        try:
            same = subprocess.run([str(vp), "-c", "import sys; print(sys.version_info[:2])"], capture_output=True, text=True,
                                  timeout=60).stdout.strip() == str(sys.version_info[:2])
        except (OSError, subprocess.SubprocessError):
            same = False
        if same:
            return
        say("The old venv does not match this Python; building it again")
        shutil.rmtree(dest / "venv", ignore_errors=True)
    try:
        run([sys.executable, "-m", "venv", dest / "venv"], quiet=True)
    except Stop as e:
        hint = " On Debian and Ubuntu: sudo apt install python3-venv" if not WINDOWS else ""
        raise Stop(f"Could not create the virtual environment ({e}).{hint}") from None


def install_packages(dest, gpu):
    vp = venv_python(dest)
    run([vp, "-m", "pip", "install", "--upgrade", "pip"], quiet=True)
    if sys.platform.startswith("linux") and not gpu:  # the default Linux wheel brings gigabytes of GPU libraries
        say("Installing torch (CPU build)")
        run([vp, "-m", "pip", "install", "torch>=2.1.0", "torchaudio>=2.1.0", "--index-url", TORCH_CPU])
    say("Installing the program's packages; the first time this takes several minutes")
    run([vp, "-m", "pip", "install", "-r", dest / "requirements.txt"])
    run([vp, "-m", "pip", "install", "--upgrade", "yt-dlp"], quiet=True)  # video sites change often; this one is kept fresh


def ffmpeg_dir(dest):
    return dest / "tools" / "ffmpeg" / "bin"


def have_ffmpeg(dest):
    path = os.pathsep.join([str(ffmpeg_dir(dest)), os.environ.get("PATH", "")])
    return all(shutil.which(tool, path=path) for tool in ("ffmpeg", "ffprobe"))


def get_ffmpeg(dest, force):
    """ffmpeg and ffprobe: the ones on PATH, else a copy kept inside the install folder."""
    if have_ffmpeg(dest) and not force:
        return say("ffmpeg: found")
    out = ffmpeg_dir(dest)
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        if not shutil.which("brew"):
            raise Stop("ffmpeg is needed. Install it with Homebrew (https://brew.sh) then: brew install ffmpeg. Then run this again.")
        run(["brew", "install", "ffmpeg"])
        return
    if not WINDOWS and machine not in ("x86_64", "amd64", "aarch64", "arm64"):
        raise Stop(f"ffmpeg is needed; install it with your package manager, then run this again ({machine} has no ready copy)")
    say("Getting ffmpeg")
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        archive = work / "ffmpeg.pkg"
        fetch(FFMPEG_ZIP if WINDOWS else FFMPEG_TAR.format(arch="arm64" if machine in ("aarch64", "arm64") else "amd64"), archive)
        if WINDOWS:
            with zipfile.ZipFile(archive) as z:
                z.extractall(work / "x")
        else:
            with tarfile.open(archive) as t:
                t.extractall(work / "x", filter="data")
        out.mkdir(parents=True, exist_ok=True)
        for tool in ("ffmpeg", "ffprobe"):
            found = next((p for p in (work / "x").rglob(tool + (".exe" if WINDOWS else "")) if p.is_file()), None)
            if not found:
                raise Stop(f"The ffmpeg download holds no {tool}")
            shutil.copy2(found, out / found.name)
            if not WINDOWS:
                (out / found.name).chmod(0o755)
    if not have_ffmpeg(dest):
        raise Stop("ffmpeg was fetched but does not run on this computer; install it with your package manager")


def prefetch(dest, models):
    """The face detector, and the speech model, so the first clip does not wait for downloads."""
    vp = venv_python(dest)
    env = {**os.environ, "PATH": os.pathsep.join([str(ffmpeg_dir(dest)), os.environ.get("PATH", "")])}
    done = subprocess.run([str(vp), "-c", "import watcher; watcher.face_model()"], cwd=dest, env=env, capture_output=True, text=True)
    say("Face detector: " + ("ready" if not done.returncode else "not fetched now; it is fetched on first use"))
    if models:
        say("Speech model: downloading (about 140 MB)")
        done = subprocess.run([str(vp), "-c", "import whisper; whisper.load_model('base')"], cwd=dest, env=env, capture_output=True, text=True)
        say("Speech model: " + ("ready" if not done.returncode else "not fetched now; it is fetched when the first clip needs it"))


def check(dest):
    """Everything the program needs, tried. Returns the list of problems."""
    problems = []
    vp = venv_python(dest)
    if not vp.exists():
        return ["the program's Python (venv) is missing"]
    probe = ("import sys; sys.path.insert(0, '.')\n"
             "for name in ('cv2', 'numpy', 'whisper', 'streamlink', 'yt_dlp', 'google_auth_oauthlib', 'watcher', 'gui', 'podcast'):\n"
             "    try:\n        __import__(name)\n    except Exception as e:\n        print(name + ': ' + str(e)[:120])\n")
    env = {**os.environ, "PATH": os.pathsep.join([str(ffmpeg_dir(dest)), os.environ.get("PATH", "")])}
    done = subprocess.run([str(vp), "-c", probe], cwd=dest, env=env, capture_output=True, text=True, errors="replace")
    problems += [f"cannot load {line}" for line in done.stdout.strip().splitlines() if line]
    if done.returncode:
        problems.append("the program's Python did not run: " + (done.stderr.strip().splitlines() or ["no reason given"])[-1][:160])
    if not have_ffmpeg(dest):
        problems.append("ffmpeg or ffprobe is missing")
    for rel in PROGRAM:
        if not (dest / rel).is_file():
            problems.append(f"{rel} is missing")
    return problems


def shortcuts(dest):
    """A way to open the board from the desktop or the menu."""
    try:
        if WINDOWS:
            script = ("$d = [Environment]::GetFolderPath('Desktop'); $s = (New-Object -ComObject WScript.Shell).CreateShortcut("
                      "(Join-Path $d 'clip-watcher.lnk')); $s.TargetPath = $env:CW_TARGET; $s.WorkingDirectory = $env:CW_DIR; $s.Save()")
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True,
                           env={**os.environ, "CW_TARGET": str(dest / "watch.bat"), "CW_DIR": str(dest)})
            return "a shortcut on the desktop"
        if sys.platform == "darwin":
            launcher = Path.home() / "Desktop" / "clip-watcher.command"
            launcher.write_text(f'#!/bin/bash\ncd "{dest}" && exec ./watch.sh\n', encoding="utf-8")
            launcher.chmod(0o755)
            return "clip-watcher.command on the desktop"
        apps = Path.home() / ".local" / "share" / "applications"
        apps.mkdir(parents=True, exist_ok=True)
        (apps / "clip-watcher.desktop").write_text(
            f"[Desktop Entry]\nType=Application\nName=clip-watcher\nExec={dest / 'watch.sh'}\nPath={dest}\nTerminal=true\n", encoding="utf-8")
        return "clip-watcher in the application menu"
    except (OSError, subprocess.SubprocessError) as e:
        say(f"No shortcut made ({e}); open the board with watch.bat or ./watch.sh in the install folder")
        return None


def start(dest):
    if WINDOWS:
        subprocess.Popen(["cmd", "/c", "start", "clip-watcher", str(dest / "watch.bat")], cwd=dest)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-a", "Terminal", str(dest / "watch.sh")], cwd=dest)
    else:
        subprocess.Popen([str(dest / "watch.sh")], cwd=dest, start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def install(args):
    check_python()
    dest = Path(args.dir).expanduser().resolve() if args.dir else (
        HERE if (HERE / "installed.json").is_file() else Path.home() / "clip-watcher")
    if args.check:
        problems = check(dest)
        say("clip-watcher at " + str(dest) + (": every check passed" if not problems else ": problems found"))
        for p in problems:
            say("  - " + p)
        return 1 if problems else 0
    updating = (dest / "installed.json").is_file()
    if dest.exists() and not updating and any(dest.iterdir()) and dest != HERE:
        if not ask(f"{dest} already holds files that are not a clip-watcher install. Install into it anyway?", False, args):
            raise Stop("Nothing was changed. Pick another folder with --dir.")
    if not updating and not args.dir and not ask(f"Install clip-watcher into {dest}?", True, args):
        raise Stop("Nothing was changed. Run this again with --dir <folder> to pick another place.")
    say(("Updating " if updating else "Installing into ") + str(dest))
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        src = source_folder(args.source, Path(work), dest)
        copy_program(src, dest)
        origin = args.source or str(src)
    say("Program files: in place")
    make_venv(dest)
    install_packages(dest, args.gpu)
    get_ffmpeg(dest, args.ffmpeg == "download")
    prefetch(dest, not args.no_models and ask("Download the speech model now, so the first clip is quick?", True, args))
    (dest / "installed.json").write_text(json.dumps({"updated": time.strftime("%Y-%m-%d %H:%M:%S"), "python": platform.python_version(),
                                                     "source": origin}), encoding="utf-8")
    problems = check(dest)
    if problems:
        raise Stop("Installed, but the check found problems: " + "; ".join(problems))
    made = None if args.no_shortcut or updating else shortcuts(dest)
    say("")
    say("Done. " + ("Updated; your clips, channels and settings were left as they are." if updating else "clip-watcher is installed."))
    say(f"  Open the board: {'watch.bat' if WINDOWS else './watch.sh'} in {dest}" + (f", or {made}" if made else ""))
    say("  The board is a page in your browser at http://127.0.0.1:8791/ and runs on this computer only.")
    if not args.no_start and ask("Open the board now?", True, args):
        start(dest)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dir", help="where to install (default ~/clip-watcher, or this folder when it is already installed)")
    p.add_argument("--source", help="a clip-watcher folder, a zip, or an https link to a zip; default this folder, else GitHub")
    p.add_argument("--yes", action="store_true", help="never ask; take the defaults")
    p.add_argument("--check", action="store_true", help="test an install and change nothing")
    p.add_argument("--gpu", action="store_true", help="on Linux, install torch with GPU support instead of the small CPU build")
    p.add_argument("--ffmpeg", choices=("auto", "download"), default="auto", help="download: keep a private copy of ffmpeg even if one is installed")
    p.add_argument("--no-models", action="store_true", help="do not download the speech model now")
    p.add_argument("--no-shortcut", action="store_true")
    p.add_argument("--no-start", action="store_true", help="do not open the board at the end")
    args = p.parse_args()
    try:
        code = install(args)
    except Stop as e:
        say(f"\nProblem: {e}")
        code = 1
    except KeyboardInterrupt:
        say("\nStopped. Run this again to continue; nothing already installed is lost.")
        code = 130
    if WINDOWS and sys.stdin.isatty() and not args.yes and len(sys.argv) == 1:
        input("\nPress Enter to close this window ")
    sys.exit(code)


if __name__ == "__main__":
    main()
