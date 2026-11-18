# 🎬 clip-watcher

An automated Twitch stream hype-detector, clip creator, and subtitle generator.

`clip-watcher` listens to Twitch chat activity in real-time. When a chat message spike occurs (e.g., laughter, hype, clutch plays), it automatically cuts a 30-second video clip, crops it into **9:16 vertical video** (ideal for TikTok, YouTube Shorts, and Instagram Reels), and generates synchronized subtitle files (`.srt`) using **OpenAI Whisper**.

---

## ✨ Features

- **Real-Time Hype Detection**: Connects anonymously to Twitch IRC over SSL (port 443) to monitor message velocity in 10-second buckets against a dynamic rolling median (2.5x threshold).
- **Auto-Recording**: Captures live stream audio/video in source quality via `streamlink` (with ad-skipping enabled).
- **Vertical Video Formatting**: Automatically crops 16:9 widescreen video to **9:16 vertical video** (`crop=ih*9/16:ih`) using `ffmpeg`.
- **AI Speech-to-Text Transcription**: Generates subtitle files (`.srt`) for every cut clip via local OpenAI Whisper models.
- **Cross-Platform**: Ready-to-use setup and launch scripts for **Linux**, **macOS**, and **Windows**.

---

## 📁 Project Structure

```text
clip-watcher/
├── watcher.py          # Core watcher, chat monitor, clip cutter & Whisper worker
├── requirements.txt    # Python dependencies (Streamlink, Whisper, PyTorch, etc.)
├── setup.sh            # 1-click installer for Linux & macOS
├── watch.sh            # Launcher script for Linux & macOS
├── setup.bat           # 1-click installer for Windows PC (winget + venv)
├── watch.bat           # Launcher script for Windows PC
├── rec/                # Raw stream recordings (.ts) [git-ignored]
└── clips/              # Output 9:16 video clips (.mp4) and subtitles (.srt) [git-ignored]
```

---

## 🚀 Quick Start

### Linux & macOS

1. **Run Setup** (Installs `ffmpeg` via system package manager, creates `venv`, installs Python dependencies):
   ```bash
   chmod +x setup.sh watch.sh
   ./setup.sh
   ```

2. **Start Watching a Channel**:
   ```bash
   ./watch.sh <twitch_channel>
   ```

---

### Windows (Command Prompt / PowerShell)

1. **Run Setup** (Installs `ffmpeg` via `winget`, creates `venv`, installs Python dependencies):
   ```cmd
   setup.bat
   ```

2. **Start Watching a Channel**:
   ```cmd
   watch.bat <twitch_channel>
   ```

---

## ⚙️ Usage & CLI Options

You can invoke `watch.sh`, `watch.bat`, or `python watcher.py` directly with custom parameters:

```bash
python watcher.py <channel> [--min MIN_MESSAGES] [--model WHISPER_MODEL]
```

### Options:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `channel` | `string` | *(required)* | Twitch channel username (e.g. `gorgc`, `shroud`, `tarik`). |
| `--min` | `int` | `15` | Minimum number of messages in a 10-second window required to qualify as a spike. |
| `--model` | `string` | `base` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large`. Use `tiny` for faster generation on low-end hardware. |

### Examples:

- **Monitor with higher hype threshold for busy chats:**
  ```bash
  ./watch.sh xqc --min 40
  ```

- **Use a lighter transcription model for faster processing:**
  ```bash
  ./watch.sh shroud --min 20 --model tiny
  ```

- **Run directly on Windows:**
  ```cmd
  watch.bat tarik --min 25 --model base
  ```

---

## 🧠 How It Works

1. **Stream Capture**: Launches `streamlink` in the background to record the stream into `rec/<channel>_<timestamp>.ts`.
2. **Chat Sampling**: Groups incoming chat messages into 10-second windows and keeps a rolling 3-minute history (18 buckets).
3. **Spike Trigger**: A hype moment is triggered when:
   $$\text{Messages} \ge \max(\text{min\_messages}, 2.5 \times \text{median})$$
   A 30-second cooldown prevents duplicate clips for the same event.
4. **Video Clipping**: Extracts 30 seconds (-20s before the spike up to +10s after) and crops to 9:16 aspect ratio:
   ```text
   clips/<channel>_<HHMMSS>.mp4
   ```
5. **Transcription**: Runs Whisper on the clipped video to produce:
   ```text
   clips/<channel>_<HHMMSS>.srt
   ```

---

## 🛠️ Manual Installation (Optional)

If you prefer setting up manually without `setup.sh` or `setup.bat`:

1. Ensure **`ffmpeg`** is installed and in your system `PATH`:
   - **Ubuntu/Debian**: `sudo apt install ffmpeg`
   - **macOS**: `brew install ffmpeg`
   - **Windows**: `winget install Gyan.FFmpeg`
2. Create and activate a Python virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```
3. Install dependencies:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

---

## 📄 License

MIT

