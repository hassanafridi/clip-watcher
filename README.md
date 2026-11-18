# clip-watcher

Records live Twitch and Kick streams, watches their chats, and cuts a vertical clip with subtitles every time a chat spikes. Several streams can run at once, each with its own threshold. Optionally uploads each clip to YouTube Shorts. Everything except that upload, and the optional category lookup, runs on your own machine.

## Install

Linux and macOS:

    chmod +x setup.sh watch.sh
    ./setup.sh

Windows:

    setup.bat

Both install ffmpeg, create a virtual environment in `venv/`, and install `requirements.txt`. To do it by hand you need Python 3.10+, ffmpeg and ffprobe on PATH, and `pip install -r requirements.txt`.

## Use

### Streams board

    ./watch.sh          (Windows: watch.bat)

With no arguments it opens the streams board in your browser, served from this machine only at `http://127.0.0.1:8791/`. Keep the window `watch.bat` or `watch.sh` opened; closing it stops clip-watcher.

- Add streams by name or pasted `twitch.tv` / `kick.com` link, several at once if you like. Each gets its own row. A link says which site it is; plain names are Twitch unless "Plain names are Kick channels" is ticked in the settings.
- "Who is live in" lists the most watched live channels of a category on Twitch or Kick. Pick the category from the dropdown, which holds the site's most watched categories right now, or choose "Another category..." and type its name. "Add and start" puts the channels on the board and starts clipping. This needs your own app keys, see below.
- Every row shows that chat's Msgs, Median and Threshold, refreshed with each count, and a small chart of the last counts with the threshold marked.
- Every row has its own Minimum and Multiplier. Change them while the stream runs and the threshold follows at the next count; nothing restarts.
- Start and stop each stream on its own, or all together. Stop lets a stream finish the clips it has already cut. While it does, its button reads "Quit now"; after asking, that drops those clips and leaves at once. "Stop all" never does that.
- "Settings for every stream" holds everything else: clip length, shape, cam placement, quality, subtitles, upload. A stream uses them from its next start. Hover over a setting for a longer explanation.
- The board, the per-stream values and the settings are remembered in `streams.json`.

A browser page cannot start a recording by itself, so opening `gui.html` on its own only works while clip-watcher is running: then it jumps straight to the board, otherwise it tells you to start `watch.bat` first.

### Terminal

    ./watch.sh xqc --min 170                # one stream, as before
    ./watch.sh xqc:170 dinah alanzoka:100   # several in one command, each with its own minimum
    ./watch.sh https://kick.com/xqc         # a pasted link works; Kick is recognised from it
    ./watch.sh xqc --kick                   # a plain name on Kick
    ./watch.sh --category "Just Chatting" --top 3   # the 3 most watched live channels of a category
    ./watch.sh xqc --upload                 # also upload each clip (private)
    ./watch.sh xqc @square.txt              # settings from a preset file

One line is printed per stream and count:

    [xqc] 1805s | Msgs: 130 | Median: 128.5 | Threshold: 325 | recording

Running one terminal per stream works too. However the streams are started, at most `--jobs` clips are encoded and transcribed at the same time across all of them, so a burst of spikes on four streams does not swamp the machine. When 6 clips of one run are still being finished, further spikes are skipped and the log says so.

Ctrl+C stops in order: clips that are waiting take their cut, recording stops, and the clips already cut are finished. Ctrl+C again quits at once.

A spike is a count of at least the stream's minimum and at least 2.5x the median of the last 3 minutes. The first clip can come after 30 seconds of chat. The clip covers 20 seconds before the spike was noticed and 10 seconds after, and spikes within one clip length of the last clip are skipped. While chat is reconnecting, counts are shown as "chat not counted"; they are neither judged nor taken into the median.

A stream that is not live yet, or that drops, is not given up: clip-watcher keeps checking, every minute or two, and starts recording when it is back.

### Finding streams by category

Listing who is live in a category is only offered through the sites' official APIs, and those want an app of your own. It is free and takes a few minutes per site:

- Twitch: sign in at `dev.twitch.tv/console/apps`, register an application (any name, redirect URL `http://localhost`, category "Other"), then copy its Client ID and create a Client Secret.
- Kick: turn on two-factor sign-in for your account, then open Account Settings, Developer tab (`kick.com/settings/developer`), create an app (redirect URL `http://localhost`), and copy its Client ID and Client Secret.

Save them next to `watcher.py` in a file named `apps.json` (git-ignored; keep it private):

    {
      "twitch": {"client_id": "...", "client_secret": "..."},
      "kick": {"client_id": "...", "client_secret": "..."}
    }

One site is enough if you only use one. The category must be the site's own name for it, for example `Just Chatting`; when it is not found, close names are suggested. To only look, without recording:

    python live.py "Just Chatting" --top 10
    python live.py "Just Chatting" --kick
    python live.py                           # the most watched categories right now

Kick lists its live channels oldest first and has no "most watched" order, so clip-watcher reads a category's whole list to rank it. For the category dropdown it adds up every live channel on Kick, which takes a few seconds; the list is then kept for five minutes.

## Where things go

- `clips/` holds the finished `.mp4` and `.srt` files. Nothing else is ever written there. Kick clips carry `-kick` after the channel name.
- `rec/` holds the working recordings, in one `run-...` folder per running clip-watcher. They are temporary: every `--rotate-mins` (60) a fresh file takes over, old and new run side by side for `--overlap-secs` (60) so a clip at the switch is still whole, and the old file is deleted as soon as no clip needs it. One stream holds roughly 3 to 4 GB at 1080p. When clip-watcher stops it removes its folder; if it was killed, the next start removes the folder it left.
- `--keep-rec` keeps every recording instead, directly in `rec/` under names the clean-up never touches. A long stream then fills the disk at roughly 3 to 4 GB per hour.
- Recordings made by earlier versions (single `.ts` files directly in `rec/`) are never touched. Delete them by hand when you no longer want them.

## Options

`./watch.sh --help` lists every option with its default. The board shows the same list.

| Option | Default | What it changes |
| --- | --- | --- |
| `--kick` | off | channels given by plain name are on Kick; with `--category`, look on Kick |
| `--category`, `--top` | none, 3 | also watch the most watched live channels of a category |
| `--quality` | 1080p | highest quality to record: `1080p`, `720p`, `480p`, `360p` or `160p` |
| `--rotate-mins` | 60 | minutes after which a fresh recording file replaces the old one |
| `--overlap-secs` | 60 | seconds old and new recording run side by side at a switch |
| `--keep-rec` | off | keep the recording files |
| `--min` | 15 | fewest messages per count that can be a spike; `name:170` sets it for one channel |
| `--factor` | 2.5 | spike = this many times the median count |
| `--bucket` | 10 | seconds of chat per count |
| `--window` | 180 | seconds of history the median is taken over |
| `--warmup` | 30 | seconds of chat to see before the first clip |
| `--before`, `--after` | 20, 10 | clip length: seconds before and after the spike |
| `--cooldown` | clip length | seconds between two clips of one stream |
| `--ratio` | 9:16 | shape of the clip, e.g. `1:1`, `4:5`, `16:9` |
| `--height` | 1920 | clip height in pixels; the width follows from the ratio |
| `--crf` | 23 | picture quality, lower is better and bigger |
| `--cam` | top | facecam panel on `top`, at the `bottom`, or `off` |
| `--face-share` | 0.33 | share of the clip height the facecam panel gets |
| `--face-zoom` | 4 | how tight the facecam crop is, in face widths |
| `--big-face` | 0.12 | a face wider than this share of the picture fills the clip instead |
| `--model`, `--language` | base, detect | Whisper model and subtitle language |
| `--jobs` | 1 | clips encoded and transcribed at once, over all streams |
| `--upload`, `--privacy` | off, private | YouTube Shorts upload |
| `--title`, `--description` | `{text} \| {channel}`, `{text}` | upload title and description templates |

A preset is a text file with one option per line in `--name=value` form. Save one from the board or write it by hand, then load it with `@`.

## Clip layout

Clips are H.264 video with AAC sound. A face detector looks for the streamer in each clip:

- small facecam on a game: the facecam is zoomed in on its own panel, the game is centre-cropped in the rest
- streamer fills the frame: one crop centred on the face
- no face found: plain centre crop
- `--cam off` or a landscape ratio: one crop, centred on the face when it fills the frame (see `--big-face`), otherwise the middle

## YouTube setup (once)

1. In Google Cloud Console create a project and enable "YouTube Data API v3".
2. Create an OAuth client of type "Desktop app", download it, and save it as `client_secret.json` next to `upload.py`.
3. Run with `--upload`. A browser opens once for consent; the login is kept in `token.json`.

Keep `client_secret.json` and `token.json` private (both are git-ignored). Google may hold uploads from API projects it has not audited as private no matter what `--privacy` says, and logins of projects in "Testing" status expire after a few days (delete `token.json` and sign in again).

Titles and descriptions are filled in from templates, with no outside service involved. `{text}` is what was said in the clip (its first words in the title) and `{channel}` is the channel name:

    ./watch.sh xqc --upload --title "{channel}: {text}" --description "{text} #gaming"

Every description also credits the source channel.

To upload a clip by hand:

    python upload.py clips/<file>.mp4 twitch.tv/<channel> --privacy unlisted

## Notes

- Clip streamers who allow it.
- The subtitles (Whisper) and the face detector are small models that run locally; nothing is sent anywhere.
- Twitch chat is read anonymously over TLS on port 443 and reconnects on its own.
- Kick chat is read from the public message history, which Kick refreshes every 10 seconds. Messages are counted by their own timestamps about 20 seconds late, and clips are timed to match, so Kick clips appear a little later than Twitch ones.
- During an ad break on your own connection nothing is recorded. A spike in that time is skipped, and a clip right after it starts where the recording picked up again.
- If clip-watcher is killed outright, its recorders are ended with it on Windows and Linux. On macOS they can run on until the stream ends.
- The board only answers requests from this machine that carry a secret built into the page it serves, so other websites cannot start or stop recordings.
- `python test_watch.py` runs the self-check.
