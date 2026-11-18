# clip-watcher

Records a live Twitch or Kick stream, watches chat, and cuts a vertical clip with subtitles every time chat spikes. Optionally uploads each clip to YouTube Shorts. Everything except that upload runs on your own machine.

## Install

Linux and macOS:

    chmod +x setup.sh watch.sh
    ./setup.sh

Windows:

    setup.bat

Both install ffmpeg, create a virtual environment in `venv/`, and install `requirements.txt`. To do it by hand you need Python 3.10+, ffmpeg on PATH, and `pip install -r requirements.txt`.

## Use

Settings page in your browser:

    ./watch.sh          (Windows: watch.bat)

It opens a page served from this machine only, at `http://127.0.0.1:8791/`. Every setting is on it, with a live preview of the clip layout, start and stop buttons, the log, and presets you can save and load. Keep the window `watch.bat` or `watch.sh` opened; closing it stops clip-watcher.

The channel box takes a name or a pasted `twitch.tv` / `kick.com` link. Hover over a setting (or click into it) for a longer explanation of what it is for. The Theme button in the top bar switches between light, dark and following the system.

A browser page cannot start a recording by itself, so opening `gui.html` on its own only works while clip-watcher is running: then it jumps straight to the live page, otherwise it tells you to start `watch.bat` first.

Terminal:

    ./watch.sh <channel>                 # Twitch
    ./watch.sh <channel> --kick          # Kick
    ./watch.sh https://kick.com/<channel>   # a pasted link works too
    ./watch.sh <channel> --min 5         # quiet chat
    ./watch.sh <channel> --upload        # also upload each clip (private)
    ./watch.sh <channel> @square.txt     # settings from a preset file

The stream is recorded to `rec/`. Clips (`.mp4`) and subtitles (`.srt`) land in `clips/`. It runs until the stream ends or you stop it.

A spike is a 10 second chat bucket with at least `--min` messages and at least 2.5x the median of the last 3 minutes, so the first clip can come no sooner than 3 minutes in. The clip covers 20 seconds before the spike was detected and 10 seconds after. Spikes within one clip length of the last clip are skipped so clips do not overlap.

## Options

`./watch.sh --help` lists every option with its default. The settings page shows the same list.

| Option | Default | What it changes |
| --- | --- | --- |
| `--kick` | off | the channel is on Kick, not Twitch |
| `--quality` | 1080p | highest quality to record: `1080p`, `720p`, `480p`, `360p` or `160p`; the best the stream offers up to this is used |
| `--min` | 15 | fewest messages per bucket that count as a spike |
| `--factor` | 2.5 | spike = this many times the median bucket |
| `--bucket` | 10 | seconds of chat per bucket |
| `--window` | 180 | seconds of history the median is taken over |
| `--before`, `--after` | 20, 10 | clip length: seconds before and after the spike |
| `--cooldown` | clip length | seconds between clips |
| `--ratio` | 9:16 | aspect ratio, e.g. `1:1`, `4:5`, `16:9` |
| `--height` | 1920 | clip height in pixels; the width follows from the ratio |
| `--crf` | 23 | video quality, lower is better and bigger |
| `--cam` | top | facecam panel on `top`, at the `bottom`, or `off` |
| `--face-share` | 0.33 | share of the clip height the facecam panel gets |
| `--face-zoom` | 4 | how tight the facecam crop is, in face widths |
| `--big-face` | 0.12 | a face wider than this share of the frame fills the clip instead |
| `--model`, `--language` | base, detect | Whisper model and subtitle language |
| `--upload`, `--privacy` | off, private | YouTube Shorts upload |
| `--title`, `--description` | `{text} \| {channel}`, `{text}` | upload title and description templates |

A preset is a text file with one option per line in `--name=value` form. Save one from the settings page or write it by hand, then load it with `@`.

## Clip layout

A face detector looks for the streamer in each clip:

- small facecam on a game: the facecam is zoomed in on its own panel, the game is centre-cropped in the rest
- streamer fills the frame: one crop centred on the face
- no face found, `--cam off`, or a landscape ratio: plain centre crop

## YouTube setup (once)

1. In Google Cloud Console create a project and enable "YouTube Data API v3".
2. Create an OAuth client of type "Desktop app", download it, and save it as `client_secret.json` next to `upload.py`.
3. Run with `--upload`. A browser opens once for consent; the login is kept in `token.json`.

Keep `client_secret.json` and `token.json` private (both are git-ignored). Google may hold uploads from API projects it has not audited as private no matter what `--privacy` says, and logins of projects in "Testing" status expire after a few days (delete `token.json` and sign in again).

Titles and descriptions are filled in from templates, with no outside service involved. `{text}` is what was said in the clip (its first words in the title) and `{channel}` is the channel name:

    ./watch.sh <channel> --upload --title "{channel}: {text}" --description "{text} #gaming"

Every description also credits the source channel.

To upload a clip by hand:

    python upload.py clips/<file>.mp4 twitch.tv/<channel> --privacy unlisted

## Notes

- It only works while the channel is live.
- Clip streamers who allow it.
- The subtitles (Whisper) and the face detector are small models that run locally; nothing is sent anywhere.
- Twitch chat is read anonymously over TLS on port 443.
- Kick chat is read from the public message history, which Kick refreshes every 10 seconds. Messages are counted by their own timestamps about 20 seconds late, and clips are timed to match, so Kick clips appear a little later than Twitch ones.
- Once a minute the log says where chat stands, for example `chat: 52 messages in the last 10 s, usual 48, a spike needs 120`. If clips never come, lower the spike multiplier until that last number is within reach.
- The settings page only answers requests from this machine that carry a secret built into the page it serves, so other websites cannot start or stop recordings.
- `python test_watch.py` runs the self-check.
