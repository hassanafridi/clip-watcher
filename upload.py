#!/usr/bin/env python3
"""Upload a clip to YouTube Shorts with a title and description filled in from its transcript."""
import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
THUMB_URL = "https://www.googleapis.com/upload/youtube/v3/thumbnails/set"
HASHTAGS = "#{channel} #{game} #{site} #clips"
# the short names people search for; any other game becomes its name run together, e.g. #EldenRing
GAME_TAGS = {"grand theft auto vi": "GTA6", "grand theft auto v": "GTA5", "counter-strike": "CS2", "counter-strike 2": "CS2",
             "league of legends": "LeagueOfLegends", "call of duty: warzone": "Warzone", "just chatting": "JustChatting",
             "ea sports fc 25": "EAFC25", "ea sports fc 26": "EAFC26", "rocket league": "RocketLeague"}
TITLE = "{text} | {channel}"
DESCRIPTION = "{text}"


def transcript(srt):
    if not srt.exists():
        return ""
    lines = (line.strip() for line in srt.read_text(encoding="utf-8").splitlines())
    return " ".join(line for line in lines if line and not line.isdigit() and "-->" not in line)


def describe(text, channel, title=TITLE, description=DESCRIPTION):
    """Fill the templates: {channel}, and {text} = what was said (first words only in the title)."""

    def clean(s):
        return s.replace("<", "").replace(">", "").strip()

    opening = text if len(text) <= 60 else text[:60].rsplit(" ", 1)[0]
    return (
        clean(title.format(channel=channel, text=opening or "live moment"))[:100],
        clean(description.format(channel=channel, text=text[:300])),
    )


def game_tag(game):
    """A game's name as a hashtag word: its usual short name, or the name run together."""
    game = (game or "").strip()
    return GAME_TAGS.get(game.lower()) or "".join(w[:1].upper() + w[1:] for w in re.sub(r"[^\w ]", " ", game).split())


def hashtags(template, source, game="", channel=None):
    """The hashtags of an upload: the template filled in ({channel}, {game}, {site}), each once, #Shorts last.

    YouTube ignores every hashtag of a video that has more than 60, and counts piles of them as spam, so at most
    15 are kept.
    """
    site, _, named = source.partition("/")
    words = (template or "").format(channel=re.sub(r"\W", "", channel or named), site=site.split(".")[0],
                                     game=game_tag(game)).split()
    tags = []
    for word in words + ["#Shorts"]:
        tag = "#" + re.sub(r"\W", "", word.lstrip("#"))
        if len(tag) > 1 and tag.lower() not in (t.lower() for t in tags):
            tags.append(tag)
    return " ".join(tags[:15])


def unlinked(text):
    """The text without http:// or https://: YouTube blocks links in descriptions until the channel is eligible, so
    twitch.tv/name is written bare."""
    return re.sub(r"https?://", "", text)


def profile_dir(profile=""):
    """Where a channel keeps its YouTube sign-in: its own folder, or this one for the default channel."""
    return HERE / "profiles" / profile if profile else HERE


def signed_in(profile=""):
    return (profile_dir(profile) / "token.json").exists()


def sign_in(profile="", wait=300):
    """Open the browser once for the channel's Google account and keep the login in its token.json.

    A channel uses its own client_secret.json when it has one, else the one in this folder.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    folder = profile_dir(profile)
    secret = next((p for p in (folder / "client_secret.json", HERE / "client_secret.json") if p.exists()), None)
    if not secret:
        sys.exit("client_secret.json not found; see README, YouTube setup")
    creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0, timeout_seconds=wait)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "token.json").write_text(creds.to_json())
    return creds


def youtube(profile=""):
    """Authorized HTTP session for a channel; the first call opens a browser for consent."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials

    token = profile_dir(profile) / "token.json"
    if token.exists():
        return AuthorizedSession(Credentials.from_authorized_user_file(str(token), SCOPES))
    return AuthorizedSession(sign_in(profile))


def publish(clip, source, privacy="private", title=TITLE, description=DESCRIPTION, say=print, tags=HASHTAGS,
            thumbnail=None, game="", profile="", channel=None):
    """Upload clip (an .mp4 with an .srt beside it) and return the YouTube video id.

    thumbnail: an image to set as the video's thumbnail afterwards; YouTube may not use it on a Short.
    """
    clip = Path(clip)
    title, description = describe(transcript(clip.with_suffix(".srt")), channel or source.rsplit("/", 1)[-1], title, description)
    body = {
        "snippet": {
            "title": title,
            "description": unlinked(f"{description}\n\nClipped from {source}\n{hashtags(tags, source, game, channel)}")[:4900],
            "categoryId": "20",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    http = youtube(profile)
    start = http.post(UPLOAD_URL, json=body, headers={"X-Upload-Content-Type": "video/mp4"})
    if not start.ok:
        raise RuntimeError(f"YouTube refused the upload: {start.status_code} {start.text[:300]}")
    # one PUT, no chunked resume; fine for 30 s clips, chunk it if uploads get large
    with clip.open("rb") as f:
        done = http.put(start.headers["Location"], data=f, headers={"Content-Type": "video/mp4"})
    if not done.ok:
        raise RuntimeError(f"YouTube upload failed: {done.status_code} {done.text[:300]}")
    video = done.json()["id"]
    say(f"uploaded https://youtube.com/shorts/{video} ({privacy}): {title}")
    if thumbnail and Path(thumbnail).is_file():
        say(set_thumbnail(video, thumbnail, http))  # a refused thumbnail does not undo the upload
    return video


def set_thumbnail(video, picture, http=None, profile=""):
    """Give an uploaded video its thumbnail. Returns "thumbnail set", or why not in plain words."""
    answer = (http or youtube(profile)).post(THUMB_URL, params={"videoId": video}, data=Path(picture).read_bytes(),
                                      headers={"Content-Type": "image/jpeg"})
    if answer.ok:
        return "thumbnail set"
    if answer.status_code == 403 and "permissions to upload and set custom video thumbnails" in answer.text:
        return ("thumbnail not set: YouTube allows custom thumbnails only on verified channels; verify yours at "
                "youtube.com/verify, then send it again")
    return f"thumbnail not set: YouTube answered {answer.status_code} {answer.text[:200]}"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("clip", help="path to an .mp4 in clips/")
    p.add_argument("source", help="where it was clipped from, e.g. twitch.tv/somechannel")
    p.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    p.add_argument("--title", default=TITLE, help="title template; {text} and {channel} are filled in")
    p.add_argument("--description", default=DESCRIPTION, help="description template, same fields")
    args = p.parse_args()
    publish(args.clip, args.source, args.privacy, args.title, args.description)
