"""Command line: python cli.py LINK [LINK ...] -o comments.xlsx

Examples:
  python cli.py https://www.youtube.com/watch?v=dQw4w9WgXcQ -o yt.xlsx
  python cli.py https://www.reddit.com/r/python/comments/abc123/x/ -o reddit.csv
  python cli.py -f links.txt -o all.xlsx --keywords price,refund --min-likes 5
"""
import argparse
import os
import sys

from scraper import apply_filters, export, scrape


def main() -> int:
    p = argparse.ArgumentParser(description="Collect YouTube and Reddit comments to Excel/CSV/JSON")
    p.add_argument("links", nargs="*", help="YouTube video links, Reddit post or subreddit links")
    p.add_argument("-f", "--file", help="text file with one link per line")
    p.add_argument("-o", "--output", default="comments.xlsx", help=".xlsx, .csv or .json")
    p.add_argument("--keywords", default="", help="comma-separated; keep comments containing any")
    p.add_argument("--min-likes", type=int, default=0)
    p.add_argument("--from", dest="date_from", help="YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD")
    p.add_argument("--no-replies", action="store_true", help="top-level comments only")
    p.add_argument("--subreddit-posts", type=int, default=10, help="posts to read per subreddit link")
    p.add_argument("--max", type=int, default=None, help="max comments per link (default: all)")
    p.add_argument("--youtube-key", default=os.getenv("YOUTUBE_API_KEY"), help="optional Data API key")
    p.add_argument("--reddit-id", default=os.getenv("REDDIT_CLIENT_ID"))
    p.add_argument("--reddit-secret", default=os.getenv("REDDIT_CLIENT_SECRET"))
    a = p.parse_args()

    links = list(a.links)
    if a.file:
        with open(a.file, encoding="utf-8") as f:
            links += [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not links:
        p.error("give at least one link (or -f links.txt)")

    rows, errors = scrape(links, a.youtube_key, a.reddit_id, a.reddit_secret, a.subreddit_posts,
                          max_per_link=a.max)
    rows = apply_filters(rows, a.keywords.split(","), a.min_likes, a.date_from, a.date_to, not a.no_replies)
    export(rows, a.output)
    print(f"\nSaved {len(rows)} comments to {a.output}")
    if errors:
        print(f"{len(errors)} link(s) failed:", *errors, sep="\n  ")
    return 1 if errors and not rows else 0


if __name__ == "__main__":
    sys.exit(main())
