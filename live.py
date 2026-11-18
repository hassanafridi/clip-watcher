#!/usr/bin/env python3
"""Who is live in a category right now, asked from the official Twitch and Kick APIs with your own app keys."""
import argparse
import json
from pathlib import Path

KEYS = Path(__file__).with_name("apps.json")
TOKEN = {"twitch": "https://id.twitch.tv/oauth2/token", "kick": "https://id.kick.com/oauth/token"}
API = {"twitch": "https://api.twitch.tv/helix", "kick": "https://api.kick.com/public"}


class Problem(Exception):
    """Something the person can put right; the message says what."""


def session(site):
    """An HTTP session that carries an app token for the site, made from the keys in apps.json."""
    import requests

    try:
        keys = json.loads(KEYS.read_text(encoding="utf-8"))[site]
        client_id, secret = keys["client_id"], keys["client_secret"]
    except (OSError, ValueError, KeyError, TypeError):
        raise Problem(f"Looking up a category on {site.capitalize()} needs your own app keys in {KEYS.name}; "
                      "the README says how to get them (Finding streams by category)") from None
    try:
        r = requests.post(TOKEN[site], timeout=15,
                          data={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"})
    except OSError as e:
        raise Problem(f"{site.capitalize()} cannot be reached ({e})") from None
    if not r.ok:
        raise Problem(f"{site.capitalize()} did not accept the app keys in {KEYS.name} (HTTP {r.status_code})")
    http = requests.Session()
    http.headers.update({"Authorization": f"Bearer {r.json()['access_token']}", "Client-Id": client_id})
    return http


def fetch(http, url, **params):
    """The whole answer of one API call."""
    try:
        r = http.get(url, params=params, timeout=15)
    except OSError as e:
        raise Problem(f"{url.split('/')[2]} cannot be reached ({e})") from None
    if not r.ok:
        raise Problem(f"{url.split('/')[2]} answered HTTP {r.status_code}")
    return r.json()


def ask(http, url, **params):
    """The rows of one API call."""
    return fetch(http, url, **params).get("data") or []


def twitch(http, category, count):
    games = ask(http, f"{API['twitch']}/games", name=category)
    if not games:
        close = [g["name"] for g in ask(http, f"{API['twitch']}/search/categories", query=category, first=5)]
        raise Problem(f"Twitch has no category named '{category}'" + (f". Close ones: {', '.join(close)}" if close else ""))
    # Twitch lists streams with the most viewers first
    streams = ask(http, f"{API['twitch']}/streams", game_id=games[0]["id"], first=count)
    return games[0]["name"], [{"channel": s["user_login"], "viewers": s["viewer_count"], "title": s["title"]}
                              for s in streams]


def kick_streams(http, **where):
    """Kick's live channels, all of them or those matching `where`.

    Kick lists them oldest first and offers no other order, so the most watched are only known after reading
    the whole list.
    """
    rows, cursor = [], None
    # 20 pages of 1000 at most; with more live channels than that the newest are left out
    for _ in range(20):
        body = fetch(http, f"{API['kick']}/v2/livestreams", limit=1000, **where, **({"cursor": cursor} if cursor else {}))
        rows += body.get("data") or []
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return rows


def kick(http, category, count):
    found = ask(http, f"{API['kick']}/v2/categories", name=category)
    if not found:
        try:  # the older search endpoint finds by word, which gives names to suggest
            close = [c["name"] for c in ask(http, f"{API['kick']}/v1/categories", q=category)][:5]
        except Problem:
            close = []
        raise Problem(f"Kick has no category named '{category}'" + (f". Close ones: {', '.join(close)}" if close else ""))
    match = next((c for c in found if c["name"].lower() == category.lower()), found[0])
    streams = kick_streams(http, category_id=match["id"])
    streams.sort(key=lambda s: s.get("viewer_count") or 0, reverse=True)
    return match["name"], [{"channel": s["channel"]["slug"], "viewers": s.get("viewer_count") or 0,
                            "title": s.get("title") or ""} for s in streams[:count]]


def live(category, site="twitch", count=5):
    """The category's proper name and its `count` most watched live channels on `site` ("twitch" or "kick").

    Each channel is {"channel": name, "viewers": number, "title": text}. Raises Problem with a message meant
    for the person when the keys are missing or refused, the site cannot be reached, or the category is unknown.
    """
    category = category.strip()
    if not category:
        raise Problem("Enter a category, for example Just Chatting")
    if not 1 <= count <= 100:
        raise Problem("Ask for 1 to 100 channels")
    try:
        return (kick if site == "kick" else twitch)(session("kick" if site == "kick" else "twitch"), category, count)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Problem(f"{site.capitalize()} answered in a form this version does not understand") from None


def categories(site="twitch", count=50):
    """Names of the categories with the most viewers right now on `site`, most watched first."""
    site = "kick" if site == "kick" else "twitch"
    try:
        http = session(site)
        if site == "twitch":
            return [g["name"] for g in ask(http, f"{API['twitch']}/games/top", first=min(count, 100))]
        watched = {}  # Kick has no call for its top categories, so add up the viewers of its live channels
        for s in kick_streams(http):
            name = (s.get("category") or {}).get("name")
            if name:
                watched[name] = watched.get(name, 0) + (s.get("viewer_count") or 0)
        return sorted(watched, key=watched.get, reverse=True)[:count]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise Problem(f"{site.capitalize()} answered in a form this version does not understand") from None


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("category", nargs="?", help="category name, for example \"Just Chatting\"; leave out to list categories")
    p.add_argument("--kick", action="store_true", help="look on Kick instead of Twitch")
    p.add_argument("--top", type=int, default=5, help="how many channels to list (default 5)")
    args = p.parse_args()
    site = "kick" if args.kick else "twitch"
    try:
        if not args.category:
            print("Most watched categories right now:\n  " + "\n  ".join(categories(site)))
        else:
            name, channels = live(args.category, site, args.top)
            print(f"Live in {name}:")
            for c in channels:
                print(f"  {c['channel']:<26}{c['viewers']:>8}  {c['title'][:70]}")
    except Problem as e:
        raise SystemExit(str(e))
