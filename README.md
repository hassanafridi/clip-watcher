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

It opens a page served from this machine only. Every setting is on it, with a live preview of the clip layout, start and stop buttons, the log, and presets you can save and load. Keep the terminal window open while you use it.

Terminal:

    ./watch.sh <channel>                 # Twitch
    ./watch.sh <channel> --kick          # Kick
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
| `--quality` | best | stream quality to record, e.g. `720p60` |
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
- Kick chat is polled from the public message history every 2 seconds. That sees at most 25 new messages per poll, so spikes in very fast chats flatten out.
- The settings page only answers requests from this machine that carry the secret in the link `gui.py` prints.
- `python test_watch.py` runs the self-check.
