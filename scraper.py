"""Collect comments (including replies) from YouTube videos and Reddit posts.

Usage from Python:
    from scraper import scrape
    rows = scrape(["https://www.youtube.com/watch?v=...", "https://www.reddit.com/r/.../comments/..."])
"""
from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator
from urllib.parse import parse_qs, urlparse

import requests

USER_AGENT = "comment-scraper/1.0 (+https://github.com/SoSamb0/social-comment-scraper)"


@dataclass
class Comment:
    platform: str
    source_url: str
    post_title: str
    comment_id: str
    parent_id: str
    is_reply: bool
    author: str
    text: str
    likes: int
    published: str  # ISO 8601, UTC when known
    permalink: str


COLUMNS = [f.name for f in fields(Comment)]
Progress = Callable[[str], None]


def _iso(ts: float | None) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------- Reddit

REDDIT_POST_RE = re.compile(r"/comments/([a-z0-9]+)", re.I)


class RedditClient:
    """Reddit API client.

    Reddit blocks most anonymous script traffic, so in practice you pass the
    client id/secret of a free "script" app from https://www.reddit.com/prefs/apps
    and the client uses the official OAuth API (oauth.reddit.com).
    """

    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self.base = "https://www.reddit.com"
        if client_id and client_secret:
            r = self.s.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(client_id, client_secret),
                data={"grant_type": "client_credentials"},
                timeout=30,
            )
            r.raise_for_status()
            self.s.headers["Authorization"] = f"bearer {r.json()['access_token']}"
            self.base = "https://oauth.reddit.com"

    def get(self, path: str, **params) -> dict | list:
        params.setdefault("raw_json", 1)
        if self.base.startswith("https://www."):
            path += ".json"  # the public site needs the suffix; oauth.reddit.com does not
        for attempt in range(5):
            r = self.s.get(self.base + path, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            if r.status_code == 403 and "Authorization" not in self.s.headers:
                raise PermissionError(
                    "Reddit blocked anonymous access. Add a free Reddit API key "
                    "(client id + secret from https://www.reddit.com/prefs/apps).")
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}


def _walk_reddit(children: list, post_id: str, url: str, title: str, out: list, more: list) -> None:
    for child in children:
        kind, data = child.get("kind"), child.get("data", {})
        if kind == "more":
            more.extend(data.get("children", []))
            continue
        if kind != "t1":
            continue
        parent = data.get("parent_id", "")
        out.append(Comment(
            platform="reddit",
            source_url=url,
            post_title=title,
            comment_id=data.get("id", ""),
            parent_id=parent.split("_", 1)[-1] if parent.startswith("t1_") else "",
            is_reply=parent.startswith("t1_"),
            author=data.get("author") or "[deleted]",
            text=data.get("body", ""),
            likes=int(data.get("score") or 0),
            published=_iso(data.get("created_utc")),
            permalink="https://www.reddit.com" + data.get("permalink", ""),
        ))
        replies = data.get("replies")
        if isinstance(replies, dict):
            _walk_reddit(replies["data"]["children"], post_id, url, title, out, more)


def reddit_post_comments(client: RedditClient, url: str, progress: Progress = print) -> list[Comment]:
    m = REDDIT_POST_RE.search(url)
    if not m:
        raise ValueError(f"Not a Reddit post link: {url}")
    post_id = m.group(1)
    listing = client.get(f"/comments/{post_id}", limit=500, depth=50, sort="top")
    post = listing[0]["data"]["children"][0]["data"]
    title = post.get("title", "")
    out: list[Comment] = []
    more: list[str] = []
    _walk_reddit(listing[1]["data"]["children"], post_id, url, title, out, more)
    # Expand "load more comments" stubs, 100 ids per call.
    seen = {c.comment_id for c in out}
    while more:
        batch, more = more[:100], more[100:]
        res = client.get(
            "/api/morechildren", api_type="json", link_id=f"t3_{post_id}",
            children=",".join(batch), sort="top",
        )
        things = res.get("json", {}).get("data", {}).get("things", [])
        new: list[Comment] = []
        _walk_reddit(things, post_id, url, title, new, more)
        for c in new:
            if c.comment_id not in seen:
                seen.add(c.comment_id)
                out.append(c)
        progress(f"  Reddit: {len(out)} comments so far")
    progress(f"Reddit post '{title[:60]}': {len(out)} comments")
    return out


def reddit_subreddit_posts(client: RedditClient, url: str, limit: int) -> list[str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "r":
        raise ValueError(f"Not a subreddit link: {url}")
    sub = parts[1]
    sort = parts[2].lower() if len(parts) > 2 and parts[2].lower() in {"hot", "new", "top", "rising"} else "hot"
    data = client.get(f"/r/{sub}/{sort}", limit=min(limit, 100))
    return ["https://www.reddit.com" + c["data"]["permalink"] for c in data["data"]["children"]]


# --------------------------------------------------------------------------- YouTube

def youtube_video_id(url: str) -> str:
    u = urlparse(url)
    if u.hostname and "youtu.be" in u.hostname:
        return u.path.strip("/").split("/")[0]
    if "v" in parse_qs(u.query):
        return parse_qs(u.query)["v"][0]
    m = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", u.path)
    if m:
        return m.group(1)
    raise ValueError(f"Not a YouTube video link: {url}")


def youtube_comments_api(url: str, api_key: str, progress: Progress = print,
                         limit: int | None = None) -> list[Comment]:
    """Official YouTube Data API v3 (needs a free API key)."""
    vid = youtube_video_id(url)
    s = requests.Session()
    title = ""
    meta = s.get("https://www.googleapis.com/youtube/v3/videos",
                 params={"part": "snippet", "id": vid, "key": api_key}, timeout=30).json()
    if meta.get("items"):
        title = meta["items"][0]["snippet"]["title"]
    out: list[Comment] = []
    token = None
    while True:
        r = s.get("https://www.googleapis.com/youtube/v3/commentThreads", params={
            "part": "snippet,replies", "videoId": vid, "maxResults": 100,
            "textFormat": "plainText", "pageToken": token, "key": api_key,
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        for item in data.get("items", []):
            top = item["snippet"]["topLevelComment"]
            out.append(_yt_api_row(top, url, title, "", vid))
            total_replies = item["snippet"].get("totalReplyCount", 0)
            if total_replies:
                out.extend(_yt_api_replies(s, top["id"], url, title, vid, api_key))
        progress(f"  YouTube: {len(out)} comments so far")
        token = data.get("nextPageToken")
        if not token or (limit and len(out) >= limit):
            out = out[:limit] if limit else out
            break
    progress(f"YouTube video '{title[:60]}': {len(out)} comments")
    return out


def _yt_api_replies(s, parent_id, url, title, vid, api_key) -> list[Comment]:
    out, token = [], None
    while True:
        r = s.get("https://www.googleapis.com/youtube/v3/comments", params={
            "part": "snippet", "parentId": parent_id, "maxResults": 100,
            "textFormat": "plainText", "pageToken": token, "key": api_key,
        }, timeout=30)
        r.raise_for_status()
        data = r.json()
        out += [_yt_api_row(c, url, title, parent_id, vid) for c in data.get("items", [])]
        token = data.get("nextPageToken")
        if not token:
            return out


def _yt_api_row(c: dict, url: str, title: str, parent: str, vid: str) -> Comment:
    sn = c["snippet"]
    return Comment(
        platform="youtube", source_url=url, post_title=title, comment_id=c["id"],
        parent_id=parent, is_reply=bool(parent), author=sn.get("authorDisplayName", ""),
        text=sn.get("textDisplay", ""), likes=int(sn.get("likeCount") or 0),
        published=sn.get("publishedAt", "").replace("T", " ").replace("Z", ""),
        permalink=f"https://www.youtube.com/watch?v={vid}&lc={c['id']}",
    )


_SUFFIX = {"k": 1e3, "th": 1e3, "tuh": 1e3, "tuhat": 1e3, "тыс": 1e3, "m": 1e6, "mln": 1e6, "млн": 1e6,
           "b": 1e9, "mld": 1e9, "млрд": 1e9}


def parse_count(value) -> int:
    """'4.8M' / '422K' / '1,234' / localized '4,8 mln' -> int."""
    s = str(value or "0").replace("\xa0", " ").strip().lower()
    m = re.match(r"([\d.,\s]+)\s*([^\d\s.]*)", s)
    if not m:
        return 0
    num, suffix = m.group(1).replace(" ", ""), m.group(2).rstrip(".")
    mult = _SUFFIX.get(suffix, 1)
    if mult > 1:
        num = num.replace(",", ".")  # decimal comma with a suffix: "4,8 mln"
    else:
        num = num.replace(",", "").replace(".", "")  # thousands separators: "1,234"
    try:
        return int(round(float(num) * mult))
    except ValueError:
        return 0


def youtube_title(vid: str) -> str:
    try:
        r = requests.get("https://www.youtube.com/oembed", timeout=15, params={
            "url": f"https://www.youtube.com/watch?v={vid}", "format": "json"})
        return r.json().get("title", "") if r.ok else ""
    except (requests.RequestException, ValueError):
        return ""


def youtube_comments_nokey(url: str, progress: Progress = print, limit: int | None = None) -> list[Comment]:
    """No API key needed: reads the same data the YouTube web page loads."""
    from youtube_comment_downloader import YoutubeCommentDownloader, SORT_BY_RECENT

    vid = youtube_video_id(url)
    title = youtube_title(vid)
    dl = YoutubeCommentDownloader()
    out: list[Comment] = []
    for c in dl.get_comments_from_url(f"https://www.youtube.com/watch?v={vid}", sort_by=SORT_BY_RECENT,
                                      language="en"):
        cid = c.get("cid", "")
        parent = cid.split(".", 1)[0] if c.get("reply") else ""
        likes = parse_count(c.get("votes", "0"))
        out.append(Comment(
            platform="youtube", source_url=url, post_title=title, comment_id=cid,
            parent_id=parent, is_reply=bool(c.get("reply")), author=c.get("author", ""),
            text=c.get("text", ""), likes=likes, published=_iso(c.get("time_parsed")),
            permalink=f"https://www.youtube.com/watch?v={vid}&lc={cid}",
        ))
        if len(out) % 200 == 0:
            progress(f"  YouTube: {len(out)} comments so far")
        if limit and len(out) >= limit:
            break
    progress(f"YouTube video '{(title or vid)[:60]}': {len(out)} comments")
    return out


# --------------------------------------------------------------------------- main entry

def scrape(
    urls: Iterable[str],
    youtube_api_key: str | None = None,
    reddit_client_id: str | None = None,
    reddit_client_secret: str | None = None,
    subreddit_posts: int = 10,
    progress: Progress = print,
    max_per_link: int | None = None,
) -> tuple[list[Comment], list[str]]:
    """Return (comments, errors). One bad link never stops the whole run."""
    rows: list[Comment] = []
    errors: list[str] = []
    reddit: RedditClient | None = None
    for raw in urls:
        url = raw.strip()
        if not url:
            continue
        try:
            host = (urlparse(url).hostname or "").lower()
            if "youtube.com" in host or "youtu.be" in host:
                got = (youtube_comments_api(url, youtube_api_key, progress, max_per_link) if youtube_api_key
                       else youtube_comments_nokey(url, progress, max_per_link))
                rows += got
            elif "reddit.com" in host or "redd.it" in host:
                reddit = reddit or RedditClient(reddit_client_id, reddit_client_secret)
                posts = [url] if REDDIT_POST_RE.search(url) else reddit_subreddit_posts(reddit, url, subreddit_posts)
                for post in posts:
                    rows += reddit_post_comments(reddit, post, progress)[:max_per_link]
            else:
                raise ValueError("Unsupported link (use YouTube or Reddit)")
        except Exception as e:  # noqa: BLE001 - report and continue
            errors.append(f"{url}: {e}")
            progress(f"ERROR {url}: {e}")
    return rows, errors


def apply_filters(rows: list[Comment], keywords: list[str] | None = None, min_likes: int = 0,
                  date_from: str | None = None, date_to: str | None = None,
                  include_replies: bool = True) -> list[Comment]:
    kws = [k.lower() for k in (keywords or []) if k.strip()]
    out = []
    for c in rows:
        if c.likes < min_likes:
            continue
        if not include_replies and c.is_reply:
            continue
        if kws and not any(k in c.text.lower() for k in kws):
            continue
        day = c.published[:10]
        if date_from and day and day < date_from:
            continue
        if date_to and day and day > date_to:
            continue
        out.append(c)
    return out


# --------------------------------------------------------------------------- export

def to_csv(rows: list[Comment], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel shows Unicode
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(asdict(r) for r in rows)


def to_json(rows: list[Comment], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def to_xlsx(rows: list[Comment], path: str) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Comments"
    ws.append(COLUMNS)
    for r in rows:
        d = asdict(r)
        ws.append([d[c] for c in COLUMNS])
        # A comment starting with '=' must stay text, never become a live formula.
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"
    head = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head
    widths = {"text": 80, "post_title": 40, "permalink": 45, "source_url": 40, "author": 22, "published": 20}
    for i, col in enumerate(COLUMNS, 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(col, 14)
    for row in ws.iter_rows(min_row=2, min_col=COLUMNS.index("text") + 1, max_col=COLUMNS.index("text") + 1):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    summary = wb.create_sheet("Summary")
    summary.append(["source_url", "platform", "comments", "replies"])
    stats: dict[str, list] = {}
    for r in rows:
        s = stats.setdefault(r.source_url, [r.platform, 0, 0])
        s[1] += 1
        s[2] += int(r.is_reply)
    for url, (plat, n, rep) in stats.items():
        summary.append([url, plat, n, rep])
    summary.column_dimensions["A"].width = 70
    wb.save(path)


def export(rows: list[Comment], path: str) -> None:
    ext = path.rsplit(".", 1)[-1].lower()
    {"csv": to_csv, "json": to_json, "xlsx": to_xlsx}.get(ext, to_xlsx)(rows, path)
