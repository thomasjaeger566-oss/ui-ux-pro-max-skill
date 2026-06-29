#!/usr/bin/env python3
"""
Reddit Topic Scraper
====================

Scrapes a subreddit (the post AND its comments) via the Apify "Reddit Scraper"
actor, mines recurring *topics* (pain points, questions, repeated phrases) from
both the post bodies and the comment threads, ranks them by engagement, and
exports a content-idea report.

This is the "topic engine" workflow popular in r/linkedinautomation:

    niche  ->  subreddits  ->  scrape posts + comments  ->  mine topics
           ->  score by engagement  ->  cluster  ->  ranked content ideas

Why Apify instead of raw requests?
    Reddit aggressively rate-limits / blocks direct scraping (403, CAPTCHAs).
    Apify's actor runs through rotating proxies and returns clean JSON for both
    the original post and the full comment tree, which is exactly what this
    workflow needs ("the first post AND the comments").

No third-party Python dependencies -- standard library only. Apify is reached
over its public REST API.

Usage
-----
    export APIFY_TOKEN="apify_api_xxx"

    # Scrape a whole subreddit (hot), mine topics, write a report
    python3 reddit_topic_scraper.py \
        --subreddit linkedinautomation \
        --sort hot --limit 50 --comments 40 \
        --out topics

    # Scrape one specific post + its comments
    python3 reddit_topic_scraper.py \
        --post-url "https://www.reddit.com/r/linkedinautomation/comments/abc123/title/" \
        --out single_post

    # Run fully offline against a previously saved Apify dataset (JSON array)
    python3 reddit_topic_scraper.py --from-file raw_items.json --out topics

Outputs (written next to --out):
    <out>.topics.csv    ranked topics
    <out>.topics.md     human-readable content-idea brief
    <out>.raw.json      raw scraped items (for reuse / --from-file)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Apify integration
# --------------------------------------------------------------------------- #

APIFY_BASE = "https://api.apify.com/v2"
# Public, well-maintained Reddit actor. Override with --actor if you prefer
# another one (e.g. "trudax~reddit-scraper-lite").
DEFAULT_ACTOR = "trudax~reddit-scraper"


class ApifyError(RuntimeError):
    pass


def _http_json(url: str, method: str = "GET", payload: dict | None = None,
               token: str | None = None, timeout: int = 120) -> Any:
    """Minimal JSON HTTP helper built on urllib (no requests dependency)."""
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        raise ApifyError(f"HTTP {e.code} for {url}: {detail}") from e
    except urllib.error.URLError as e:
        raise ApifyError(f"Network error for {url}: {e.reason}") from e


def run_apify_actor(actor: str, run_input: dict, token: str,
                    poll_seconds: int = 5, max_wait: int = 1800) -> list[dict]:
    """Start an actor run, wait for it to finish, return its dataset items."""
    start_url = f"{APIFY_BASE}/acts/{actor}/runs"
    print(f"[apify] starting actor {actor} ...", file=sys.stderr)
    run = _http_json(start_url, method="POST", payload=run_input, token=token)
    run_data = run["data"]
    run_id = run_data["id"]
    dataset_id = run_data["defaultDatasetId"]
    status_url = f"{APIFY_BASE}/actor-runs/{run_id}"

    waited = 0
    while True:
        status = _http_json(status_url, token=token)["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            print(f"[apify] run {run_id} finished: {status}", file=sys.stderr)
            if status != "SUCCEEDED":
                raise ApifyError(f"Actor run ended with status {status}")
            break
        if waited >= max_wait:
            raise ApifyError(f"Timed out waiting for run {run_id}")
        time.sleep(poll_seconds)
        waited += poll_seconds
        print(f"[apify] ...{status} ({waited}s)", file=sys.stderr)

    items_url = f"{APIFY_BASE}/datasets/{dataset_id}/items?clean=true&format=json"
    items = _http_json(items_url, token=token)
    return items or []


def build_actor_input(subreddit: str | None, post_url: str | None,
                      sort: str, limit: int, comments: int) -> dict:
    """Build the run input for trudax/reddit-scraper."""
    start_urls: list[dict] = []
    if post_url:
        start_urls.append({"url": post_url})
    elif subreddit:
        sub = subreddit.lstrip("r/").strip("/")
        start_urls.append({"url": f"https://www.reddit.com/r/{sub}/{sort}/"})
    else:
        raise ValueError("Provide either --subreddit or --post-url")

    return {
        "startUrls": start_urls,
        "skipComments": False,
        "skipUserPosts": True,
        "skipCommunity": True,
        "searchPosts": True,
        "searchComments": False,
        "maxItems": limit + limit * comments,
        "maxPostCount": limit,
        "maxComments": comments,
        "maxCommunitiesCount": 1,
        "proxy": {"useApifyProxy": True},
    }


# --------------------------------------------------------------------------- #
# Normalising scraped items
# --------------------------------------------------------------------------- #

@dataclass
class Post:
    id: str
    title: str
    body: str
    url: str
    score: int
    num_comments: int
    created: float
    author: str = ""
    comments: list[str] = field(default_factory=list)
    # author aligned by index with `comments` ("" if unknown)
    authors: list[str] = field(default_factory=list)


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _author_of(it: dict) -> str:
    a = (it.get("author") or it.get("username") or it.get("authorName")
         or it.get("user") or "")
    return str(a).lstrip("u/").strip()


def normalise_items(items: list[dict]) -> list[Post]:
    """
    The Apify Reddit actor emits a flat list mixing 'post' and 'comment' items.
    Group comments (with their author) under their parent post so we can mine
    post + comments together -- exactly the "first post AND its comments" model,
    while keeping track of *who* raised each point (prospects / voices).
    """
    posts: dict[str, Post] = {}
    loose: dict[str, list[tuple[str, str]]] = defaultdict(list)  # parent -> [(text, author)]

    for it in items:
        kind = (it.get("dataType") or it.get("type") or "").lower()
        is_comment = kind == "comment" or "body" in it and "title" not in it

        if not is_comment and (it.get("title") or it.get("postId") is None):
            pid = str(it.get("id") or it.get("postId") or it.get("url") or len(posts))
            posts[pid] = Post(
                id=pid,
                title=it.get("title", "") or "",
                body=it.get("body") or it.get("text") or it.get("selftext") or "",
                url=it.get("url", "") or "",
                score=_to_int(it.get("upVotes") or it.get("score") or it.get("upvotes")),
                num_comments=_to_int(it.get("numberOfComments") or it.get("numComments")),
                created=float(it.get("createdAt", 0) or 0) if str(it.get("createdAt", "")).replace(".", "").isdigit() else 0.0,
                author=_author_of(it),
            )
        else:
            parent = str(it.get("postId") or it.get("parentId") or it.get("threadId") or "")
            text = it.get("body") or it.get("text") or ""
            if text:
                loose[parent].append((text, _author_of(it)))

    for pid, pairs in loose.items():
        target = posts.get(pid)
        if target is None:
            # Comments whose parent post wasn't captured: keep as a synthetic post
            target = posts.setdefault(pid or f"orphan-{len(posts)}", Post(
                id=pid, title="(comments only)", body="", url="", score=0,
                num_comments=len(pairs), created=0.0,
            ))
        for text, author in pairs:
            target.comments.append(text)
            target.authors.append(author)

    return list(posts.values())


# --------------------------------------------------------------------------- #
# Topic mining
# --------------------------------------------------------------------------- #

STOPWORDS = set("""
a an the and or but if then than so to of in on at by for with without about
into over after before under again further is are was were be been being have
has had do does did doing this that these those i you he she it we they them my
your our their me him her us as not no yes can will just dont don't im i'm ive
i've cant can't get got like really very much many more most some any all you're
youre how what why when where which who whom whose there here out up down off
get getting want need know also even still
""".split())

PAIN_MARKERS = [
    r"\bhow (?:do|can|to|should) i\b",
    r"\bis there a way\b",
    r"\bany(?:one|body) else\b",
    r"\bstruggling with\b",
    r"\bi (?:keep|cant|can't|couldn't|couldnt)\b",
    r"\bthe problem (?:is|with)\b",
    r"\bwhy (?:does|do|is|are|cant|can't)\b",
    r"\bbest way to\b",
    r"\bdoes anyone\b",
    r"\bwhat(?:'s| is) the best\b",
    r"\bhelp me\b",
    r"\bhow much\b",
]
PAIN_RE = re.compile("|".join(PAIN_MARKERS), re.IGNORECASE)
WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]{2,}")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def tokens(text: str) -> list[str]:
    return [w.lower() for w in WORD_RE.findall(text) if w.lower() not in STOPWORDS]


def ngrams(words: list[str], n: int) -> Iterable[str]:
    for i in range(len(words) - n + 1):
        yield " ".join(words[i:i + n])


@dataclass
class Topic:
    phrase: str
    mentions: int = 0
    engagement: int = 0          # summed post score + comment volume behind it
    questions: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)
    voices: set[str] = field(default_factory=set)   # who raised this (prospects)

    def score(self) -> float:
        # frequency x engagement, with a question bonus (questions = ready hooks)
        return self.mentions * (1 + self.engagement ** 0.5) * (1 + 0.25 * len(self.questions))


def recency_weight(created: float, now: float, half_life_days: float = 14.0) -> float:
    if not created:
        return 1.0
    age_days = max(0.0, (now - created) / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def mine_topics(posts: list[Post], min_mentions: int = 2,
                top_n: int = 40, now: float | None = None) -> list[Topic]:
    now = now or time.time()
    topics: dict[str, Topic] = defaultdict(lambda: Topic(phrase=""))
    question_bank: list[tuple[str, int]] = []

    for post in posts:
        # Recency boosts fresh threads but never zeroes out an older one,
        # so engagement always reflects real upvotes/comment volume.
        weight = 0.1 + 0.9 * recency_weight(post.created, now)
        engagement = int((post.score + post.num_comments + len(post.comments)) * weight) + 1
        blob = " ".join([post.title, post.body, *post.comments])

        # 1) extract candidate phrases (2- and 3-grams), built *within* each
        #    sentence so phrases never bridge unrelated sentences/comments.
        seen_here: set[str] = set()
        for sent in SENT_SPLIT.split(blob):
            words = tokens(sent)
            for n in (2, 3):
                for g in ngrams(words, n):
                    seen_here.add(g)
        for g in seen_here:
            t = topics[g]
            t.phrase = g
            t.mentions += 1
            t.engagement += engagement
            t.sources.add(post.url or post.id)

        # 2) harvest concrete question/pain sentences as ready-made hooks
        for sent in SENT_SPLIT.split(blob):
            s = sent.strip()
            if 12 <= len(s) <= 200 and (s.endswith("?") or PAIN_RE.search(s)):
                question_bank.append((s, engagement))

    ranked = sorted(
        (t for t in topics.values() if t.mentions >= min_mentions),
        key=lambda t: t.score(), reverse=True,
    )[:top_n]

    # attach the strongest question hooks to the topics whose words they share
    qb = sorted(question_bank, key=lambda x: -x[1])
    for topic in ranked:
        key_words = set(topic.phrase.split())
        for q, _ in qb:
            if key_words & set(tokens(q)) and q not in topic.questions:
                topic.questions.append(q)
            if len(topic.questions) >= 3:
                break

    # attribute *voices*: which authors raised each topic (prospect shortlist).
    # A phrase counts for a comment/post when all its words appear in that text.
    for topic in ranked:
        key_words = set(topic.phrase.split())
        for post in posts:
            if post.author and key_words <= set(tokens(f"{post.title} {post.body}")):
                topic.voices.add(post.author)
            for text, author in zip(post.comments, post.authors):
                if author and key_words <= set(tokens(text)):
                    topic.voices.add(author)

    return ranked


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def write_outputs(topics: list[Topic], posts: list[Post], raw: list[dict],
                  out: str) -> None:
    # raw
    with open(f"{out}.raw.json", "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    # csv
    with open(f"{out}.topics.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "topic", "mentions", "engagement", "score",
                    "example_question", "sources", "voices", "voice_names"])
        for i, t in enumerate(topics, 1):
            w.writerow([i, t.phrase, t.mentions, t.engagement, round(t.score(), 1),
                        t.questions[0] if t.questions else "", len(t.sources),
                        len(t.voices), ", ".join(sorted(t.voices)[:10])])

    # markdown brief
    lines = [
        "# Reddit Topic Brief",
        "",
        f"- Posts analysed: **{len(posts)}**",
        f"- Comments analysed: **{sum(len(p.comments) for p in posts)}**",
        f"- Topics surfaced: **{len(topics)}**",
        "",
        "## Top content topics (ranked by frequency x engagement)",
        "",
    ]
    for i, t in enumerate(topics, 1):
        lines.append(f"### {i}. {t.phrase}")
        lines.append(f"*{t.mentions} mentions · engagement {t.engagement} · score {round(t.score(),1)}*")
        if t.questions:
            lines.append("")
            lines.append("Ready-made hooks pulled from the threads:")
            for q in t.questions:
                lines.append(f"- {q}")
        if t.voices:
            names = ", ".join(f"u/{v}" for v in sorted(t.voices)[:10])
            extra = "" if len(t.voices) <= 10 else f" (+{len(t.voices) - 10} more)"
            lines.append("")
            lines.append(f"Raised by: {names}{extra}")
        lines.append("")
    with open(f"{out}.topics.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nWrote:\n  {out}.topics.csv\n  {out}.topics.md\n  {out}.raw.json")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Scrape a subreddit's posts + comments and mine content topics.")
    src = p.add_argument_group("source")
    src.add_argument("--subreddit", help="Subreddit name, e.g. linkedinautomation")
    src.add_argument("--post-url", help="A single Reddit post URL to scrape instead")
    src.add_argument("--from-file", help="Skip Apify; load a previously saved raw.json array")
    src.add_argument("--actor", default=DEFAULT_ACTOR, help=f"Apify actor id (default {DEFAULT_ACTOR})")

    cfg = p.add_argument_group("scrape config")
    cfg.add_argument("--sort", default="hot", choices=["hot", "new", "top", "rising"])
    cfg.add_argument("--limit", type=int, default=50, help="Max posts to scrape")
    cfg.add_argument("--comments", type=int, default=40, help="Max comments per post")

    mine = p.add_argument_group("mining")
    mine.add_argument("--min-mentions", type=int, default=2)
    mine.add_argument("--top", type=int, default=40, help="Max topics to keep")

    p.add_argument("--out", default="topics", help="Output file prefix")
    args = p.parse_args(argv)

    # 1. get raw items
    if args.from_file:
        with open(args.from_file, encoding="utf-8") as f:
            raw = json.load(f)
        print(f"[load] {len(raw)} items from {args.from_file}", file=sys.stderr)
    else:
        token = os.environ.get("APIFY_TOKEN")
        if not token:
            print("ERROR: set APIFY_TOKEN env var (https://console.apify.com/account/integrations)",
                  file=sys.stderr)
            return 2
        run_input = build_actor_input(args.subreddit, args.post_url,
                                      args.sort, args.limit, args.comments)
        try:
            raw = run_apify_actor(args.actor, run_input, token)
        except ApifyError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"[apify] received {len(raw)} items", file=sys.stderr)

    # 2. normalise -> posts with comments
    posts = normalise_items(raw)
    if not posts:
        print("No posts found in scraped data.", file=sys.stderr)
        return 1

    # 3. mine topics
    topics = mine_topics(posts, min_mentions=args.min_mentions, top_n=args.top)

    # 4. write
    write_outputs(topics, posts, raw, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
