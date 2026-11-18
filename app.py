"""Web UI: streamlit run app.py"""
import io
import os
import tempfile
from dataclasses import asdict

import pandas as pd
import streamlit as st

from scraper import COLUMNS, apply_filters, export, scrape

st.set_page_config(page_title="Comment Scraper", page_icon="💬", layout="wide")
st.title("💬 YouTube & Reddit Comment Scraper")
st.caption("Paste links, one per line. Collects every comment including replies, then export to Excel, CSV or JSON.")

with st.sidebar:
    st.header("Keys (optional)")
    yt_key = st.text_input("YouTube Data API key", os.getenv("YOUTUBE_API_KEY", ""), type="password",
                           help="Leave empty to use no-key mode.")
    rd_id = st.text_input("Reddit client id", os.getenv("REDDIT_CLIENT_ID", ""))
    rd_secret = st.text_input("Reddit client secret", os.getenv("REDDIT_CLIENT_SECRET", ""), type="password",
                              help="Free 'script' app from reddit.com/prefs/apps")
    st.header("Filters")
    keywords = st.text_input("Keywords (comma-separated)")
    min_likes = st.number_input("Minimum likes / score", 0, value=0)
    include_replies = st.checkbox("Include replies", True)
    c1, c2 = st.columns(2)
    d_from = c1.date_input("From", value=None)
    d_to = c2.date_input("To", value=None)
    sub_posts = st.slider("Posts per subreddit link", 1, 100, 10)
    max_per_link = st.number_input("Max comments per link (0 = all)", 0, value=0, step=100)

links = st.text_area("Links", height=140, placeholder=(
    "https://www.youtube.com/watch?v=...\nhttps://www.reddit.com/r/somesub/comments/abc123/...\n"
    "https://www.reddit.com/r/somesub/  (reads the latest posts)"))

if st.button("Collect comments", type="primary", disabled=not links.strip()):
    log = st.empty()
    with st.spinner("Collecting..."):
        rows, errors = scrape(links.splitlines(), yt_key or None, rd_id or None, rd_secret or None,
                              sub_posts, progress=lambda m: log.text(m),
                              max_per_link=int(max_per_link) or None)
    rows = apply_filters(rows, keywords.split(","), int(min_likes),
                         d_from.isoformat() if d_from else None, d_to.isoformat() if d_to else None,
                         include_replies)
    st.session_state["rows"], st.session_state["errors"] = rows, errors
    log.empty()

rows = st.session_state.get("rows")
if rows is not None:
    for e in st.session_state.get("errors", []):
        st.error(e)
    df = pd.DataFrame([asdict(r) for r in rows], columns=COLUMNS)
    m1, m2, m3 = st.columns(3)
    m1.metric("Comments", len(df))
    m2.metric("Replies", int(df["is_reply"].sum()) if len(df) else 0)
    m3.metric("Sources", df["source_url"].nunique() if len(df) else 0)
    st.dataframe(df[["platform", "author", "text", "likes", "published", "is_reply", "post_title"]],
                 use_container_width=True, height=420)
    cols = st.columns(3)
    for col, ext, mime in zip(cols, ("xlsx", "csv", "json"), (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/csv", "application/json")):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, f"comments.{ext}")
            export(rows, path)
            col.download_button(f"Download .{ext}", open(path, "rb").read(), f"comments.{ext}", mime)
