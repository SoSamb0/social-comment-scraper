"""Offline tests: Reddit tree parsing (incl. "load more" stubs), filters and exports."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openpyxl import load_workbook  # noqa: E402

import scraper  # noqa: E402


def t1(cid, parent, body, score=1, replies=None):
    return {"kind": "t1", "data": {
        "id": cid, "parent_id": parent, "author": f"user_{cid}", "body": body, "score": score,
        "created_utc": 1763500000, "permalink": f"/r/test/comments/p1/x/{cid}/",
        "replies": {"kind": "Listing", "data": {"children": replies}} if replies else "",
    }}


POST = {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": {"id": "p1", "title": "Test post"}}]}}
TREE = {"kind": "Listing", "data": {"children": [
    t1("a", "t3_p1", "top comment about price", 10, [
        t1("b", "t1_a", "reply one", 3, [t1("c", "t1_b", "nested reply", 1)]),
        {"kind": "more", "data": {"children": ["d", "e"]}},
    ]),
    t1("f", "t3_p1", "=SUM(A1) formula-looking comment", 0),
    {"kind": "more", "data": {"children": ["g"]}},
]}}
MORE = {"json": {"data": {"things": [
    t1("d", "t1_a", "loaded later d", 2),
    t1("e", "t1_a", "loaded later e", 5),
    t1("g", "t3_p1", "loaded later top-level g", 7),
]}}}


class FakeReddit:
    def __init__(self):
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path.startswith("/comments/"):
            return [POST, TREE]
        if path == "/api/morechildren":
            ids = params["children"].split(",")
            things = [t for t in MORE["json"]["data"]["things"] if t["data"]["id"] in ids]
            return {"json": {"data": {"things": things}}}
        raise AssertionError(path)


def test_reddit_tree_and_more_children():
    rows = scraper.reddit_post_comments(FakeReddit(), "https://www.reddit.com/r/test/comments/p1/x/", lambda m: None)
    ids = sorted(r.comment_id for r in rows)
    assert ids == ["a", "b", "c", "d", "e", "f", "g"]
    by = {r.comment_id: r for r in rows}
    assert by["c"].is_reply and by["c"].parent_id == "b"
    assert not by["g"].is_reply and by["g"].parent_id == ""
    assert by["a"].post_title == "Test post"
    assert by["a"].permalink.startswith("https://www.reddit.com/r/test/")


def test_filters():
    rows = scraper.reddit_post_comments(FakeReddit(), "https://reddit.com/r/test/comments/p1/", lambda m: None)
    assert [r.comment_id for r in scraper.apply_filters(rows, keywords=["price"])] == ["a"]
    assert {r.comment_id for r in scraper.apply_filters(rows, min_likes=5)} == {"a", "e", "g"}
    assert {r.comment_id for r in scraper.apply_filters(rows, include_replies=False)} == {"a", "f", "g"}


def test_exports_roundtrip():
    rows = scraper.reddit_post_comments(FakeReddit(), "https://reddit.com/r/test/comments/p1/", lambda m: None)
    with tempfile.TemporaryDirectory() as d:
        for ext in ("xlsx", "csv", "json"):
            scraper.export(rows, os.path.join(d, f"out.{ext}"))
        wb = load_workbook(os.path.join(d, "out.xlsx"))
        ws = wb["Comments"]
        assert ws.max_row == len(rows) + 1
        cells = {c.value: c for c in ws["H"]}  # 'text' column
        f = cells["=SUM(A1) formula-looking comment"]
        assert f.data_type == "s"  # stored as text, not as a formula
        assert ws["G2"].value == "user_a" and ws["E2"].value in (None, "")  # no stray quote prefixes
        assert wb["Summary"]["C2"].value == len(rows)
        data = json.load(open(os.path.join(d, "out.json"), encoding="utf-8"))
        assert len(data) == len(rows)
        with open(os.path.join(d, "out.csv"), encoding="utf-8-sig") as f:
            assert f.readline().strip().split(",")[:3] == ["platform", "source_url", "post_title"]


def test_parse_count():
    p = scraper.parse_count
    assert p("4.8M") == 4_800_000 and p("422K") == 422_000 and p("1,234") == 1234 and p("23") == 23
    assert p("4,8\xa0mln") == 4_800_000 and p("422\xa0tuh") == 422_000 and p("") == 0


def test_youtube_ids():
    f = scraper.youtube_video_id
    assert f("https://www.youtube.com/watch?v=jNQXAC9IVRw&t=3") == "jNQXAC9IVRw"
    assert f("https://youtu.be/jNQXAC9IVRw") == "jNQXAC9IVRw"
    assert f("https://www.youtube.com/shorts/jNQXAC9IVRw") == "jNQXAC9IVRw"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
