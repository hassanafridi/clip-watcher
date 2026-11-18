import argparse
import collections
import os
import re
import socket
import ssl
import statistics
import subprocess
import threading
import time
from datetime import datetime

# Configuration
IRC_SERVER = "irc.chat.twitch.tv"
IRC_PORT = 443
NICK = "justinfan12345"
BUCKET_SECONDS = 10
WINDOW_BUCKETS = 18  # 18 * 10s = 3 minutes
COOLDOWN_SECONDS = 30  # Prevent overlapping clips for the same hype moment


class ClipWatcher:
    def __init__(self, channel, min_messages, whisper_model):
        self.channel = channel.lower().lstrip("#")
        self.min_messages = min_messages
        self.whisper_model = whisper_model

        self.msg_count = 0
        self.lock = threading.Lock()
        self.history = collections.deque(maxlen=WINDOW_BUCKETS)
        self.start_time = None
        self.recording_file = None
        self.last_clip_time = 0
        self.running = True

        os.makedirs("rec", exist_ok=True)
        os.makedirs("clips", exist_ok=True)

    def start_recording(self):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.recording_file = f"rec/{self.channel}_{timestamp}.ts"
        url = f"https://twitch.tv/{self.channel}"

        print(f"[*] Starting streamlink recording to {self.recording_file}...")
        cmd = [
            "streamlink",
            "--twitch-disable-ads",
            url,
            "best",
            "-o",
            self.recording_file,
        ]
        self.start_time = time.time()
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def listen_chat(self):
        context = ssl.create_default_context()
        while self.running:
            try:
                raw_sock = socket.create_connection((IRC_SERVER, IRC_PORT), timeout=15)
                sock = context.wrap_socket(raw_sock, server_hostname=IRC_SERVER)
                sock.settimeout(60)
                sock.send(f"NICK {NICK}\r\n".encode("utf-8"))
                sock.send(f"JOIN #{self.channel}\r\n".encode("utf-8"))
                print(f"[*] Connected to #{self.channel} chat via SSL (port 443).")

                buffer = ""
                while self.running:
                    data = sock.recv(2048).decode("utf-8", errors="ignore")
                    if not data:
                        raise ConnectionError("IRC connection closed")
                    buffer += data
                    lines = buffer.split("\r\n")
                    buffer = lines.pop()

                    for line in lines:
                        if line.startswith("PING"):
                            sock.send("PONG :tmi.twitch.tv\r\n".encode("utf-8"))
                        elif "PRIVMSG" in line:
                            with self.lock:
                                self.msg_count += 1
            except Exception as e:
                print(f"[!] Chat connection error: {e} - retrying in 5s...")
                time.sleep(5)

    def process_clip(self, spike_offset_sec, count, median):
        # Wait 12 seconds so the stream buffer captures the +10s after the spike
        time.sleep(12)

        start_sec = max(0, spike_offset_sec - 20)
        duration = 30
        timestamp = datetime.now().strftime("%H%M%S")
        base_name = f"clips/{self.channel}_{timestamp}"
        video_out = f"{base_name}.mp4"

        print(f"\n[🔥 SPIKE] {count} msgs (median: {median:.1f}) -> Cutting clip {video_out}...")

        # Center-crop 16:9 to 9:16 (crop=ih*9/16:ih)
        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(start_sec),
            "-i",
            self.recording_file,
            "-t",
            str(duration),
            "-vf",
            "crop=ih*9/16:ih",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            video_out,
        ]
        subprocess.run(ffmpeg_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        if os.path.exists(video_out):
            print(f"[*] Running Whisper transcription for {video_out}...")
            whisper_cmd = [
                "whisper",
                video_out,
                "--model",
                self.whisper_model,
                "--output_format",
                "srt",
                "--output_dir",
                "clips",
            ]
            subprocess.run(whisper_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[✅ DONE] Saved {video_out} and {base_name}.srt\n")

    def run(self):
        rec_proc = self.start_recording()

        # Wait briefly for streamlink to initialize file
        time.sleep(5)
        if rec_proc.poll() is not None:
            print("[!] Streamlink exited early. Is the channel live?")
            return

        chat_thread = threading.Thread(target=self.listen_chat, daemon=True)
        chat_thread.start()

        print(f"[*] Monitoring chat (10s buckets | min={self.min_messages} | 2.5x median)...")
        try:
            while rec_proc.poll() is None:
                time.sleep(BUCKET_SECONDS)
                with self.lock:
                    count = self.msg_count
                    self.msg_count = 0

                now = time.time()
                elapsed = now - self.start_time

                if len(self.history) >= 3:
                    med = statistics.median(self.history)
                    threshold = max(self.min_messages, med * 2.5)

                    print(f"[{int(elapsed)}s] Msgs: {count} | Median: {med:.1f} | Threshold: {threshold:.1f}")

                    if count >= threshold and (now - self.last_clip_time) > COOLDOWN_SECONDS:
                        self.last_clip_time = now
                        threading.Thread(
                            target=self.process_clip,
                            args=(elapsed, count, med),
                            daemon=True,
                        ).start()
                else:
                    print(f"[{int(elapsed)}s] Warming up baseline... Msgs: {count}")

                self.history.append(count)

        except KeyboardInterrupt:
            print("\n[*] Stopping clip-watcher...")
        finally:
            self.running = False
            rec_proc.terminate()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-clip Twitch hype moments.")
    parser.add_argument("channel", help="Twitch channel name")
    parser.add_argument("--min", type=int, default=15, help="Minimum messages in 10s to trigger (default: 15)")
    parser.add_argument("--model", default="base", help="Whisper model size: tiny, base, small, medium (default: base)")
    args = parser.parse_args()

    watcher = ClipWatcher(args.channel, args.min, args.model)
    watcher.run()