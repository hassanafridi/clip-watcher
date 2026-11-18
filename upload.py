#!/usr/bin/env python3
"""Upload a clip to YouTube Shorts with a title and description filled in from its transcript."""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).parent
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
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


def youtube():
    """Authorized HTTP session; the first call opens a browser for consent and saves token.json."""
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token, secret = HERE / "token.json", HERE / "client_secret.json"
    if token.exists():
        return AuthorizedSession(Credentials.from_authorized_user_file(str(token), SCOPES))
    if not secret.exists():
        sys.exit("client_secret.json not found next to upload.py; see README, YouTube setup")
    creds = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES).run_local_server(port=0)
    token.write_text(creds.to_json())
    return AuthorizedSession(creds)


def publish(clip, source, privacy="private", title=TITLE, description=DESCRIPTION, say=print):
    """Upload clip (an .mp4 with an .srt beside it) and return the YouTube video id."""
    clip = Path(clip)
    title, description = describe(transcript(clip.with_suffix(".srt")), source.rsplit("/", 1)[-1], title, description)
    body = {
        "snippet": {
            "title": title,
            "description": f"{description}\n\nClipped from https://{source}\n#Shorts"[:4900],
            "categoryId": "20",
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    http = youtube()
    start = http.post(UPLOAD_URL, json=body, headers={"X-Upload-Content-Type": "video/mp4"})
    if not start.ok:
        raise RuntimeError(f"YouTube refused the upload: {start.status_code} {start.text[:300]}")
    # one PUT, no chunked resume; fine for 30 s clips, chunk it if uploads get large
    with clip.open("rb") as f:
        done = http.put(start.headers["Location"], data=f, headers={"Content-Type": "video/mp4"})
    if not done.ok:
        raise RuntimeError(f"YouTube upload failed: {done.status_code} {done.text[:300]}")
    say(f"uploaded https://youtube.com/shorts/{done.json()['id']} ({privacy}): {title}")
    return done.json()["id"]


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("clip", help="path to an .mp4 in clips/")
    p.add_argument("source", help="where it was clipped from, e.g. twitch.tv/somechannel")
    p.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    p.add_argument("--title", default=TITLE, help="title template; {text} and {channel} are filled in")
    p.add_argument("--description", default=DESCRIPTION, help="description template, same fields")
    args = p.parse_args()
    publish(args.clip, args.source, args.privacy, args.title, args.description)
