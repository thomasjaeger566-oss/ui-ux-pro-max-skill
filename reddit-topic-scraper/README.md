# Topic Scraper (Reddit & LinkedIn) via Apify

A **topic engine** for the r/linkedinautomation-style workflow: take a thread
(the **post _and_ its comments**), mine the recurring **topics / pain points /
questions**, rank them by engagement, and export ready-to-use **content ideas**.

```
niche  ->  source thread  ->  scrape POST + COMMENTS  ->  mine topics
       ->  score by engagement + recency  ->  ranked content ideas + hooks
```

> **Why this exists.** In automation communities the highest-leverage move is
> not "post more" — it's to read what people in your niche actually ask and
> complain about (the post _and_ the comments), turn those into topics, and
> write to them. This tool automates that mining step.

---

## What it does

1. **Scrapes a thread including its comments** — via the
   [Apify Reddit Scraper](https://apify.com/trudax/reddit-scraper) actor
   (rotating proxies, no Reddit blocking/CAPTCHAs to fight).
2. **Groups comments under their post**, so a topic's weight reflects the post
   _and_ the discussion under it.
3. **Mines topics** — sentence-bounded 2/3-grams (so phrases stay coherent),
   filtered by document frequency, plus a harvest of concrete
   **question / pain sentences** ("how do I…", "best way to…", "anyone else…")
   that double as ready-made content hooks.
4. **Ranks** topics by `frequency × engagement` with a recency half-life and a
   question bonus.
5. **Exports** `*.topics.csv`, a human-readable `*.topics.md` brief, and the
   raw `*.raw.json` for reuse.

The miner is **source-agnostic**: anything shaped like *post + comments* works,
so a LinkedIn comment export feeds the same pipeline (see below).

---

## Setup

No third-party Python packages — standard library only (Python 3.9+).

Get an Apify token (free tier is enough for testing):
<https://console.apify.com/account/integrations>

```bash
export APIFY_TOKEN="apify_api_xxxxxxxxxxxxxxxxxxxx"
```

> **Note on connectors.** There is no Apify *MCP connector* wired into this
> environment, so the tool talks to Apify directly over its public REST API.
> If you later add an Apify MCP server, the same actor (`trudax~reddit-scraper`)
> and input below apply.

---

## Usage

**Whole subreddit (hot), mine topics:**

```bash
python3 reddit_topic_scraper.py \
  --subreddit linkedinautomation \
  --sort hot --limit 50 --comments 40 \
  --out topics
```

**One specific post + its comments:**

```bash
python3 reddit_topic_scraper.py \
  --post-url "https://www.reddit.com/r/linkedinautomation/comments/<id>/<slug>/" \
  --out single_post
```

**Offline / reuse (no Apify call) — feed a saved JSON array:**

```bash
python3 reddit_topic_scraper.py --from-file topics.raw.json --out topics
```

### Key flags

| Flag             | Default | Meaning                                   |
|------------------|---------|-------------------------------------------|
| `--subreddit`    | —       | Subreddit to scrape                       |
| `--post-url`     | —       | Single post URL (overrides `--subreddit`) |
| `--from-file`    | —       | Skip Apify, load a raw JSON array         |
| `--sort`         | `hot`   | `hot` / `new` / `top` / `rising`          |
| `--limit`        | `50`    | Max posts                                 |
| `--comments`     | `40`    | Max comments per post                     |
| `--min-mentions` | `2`     | Min posts a topic must appear in          |
| `--top`          | `40`    | Max topics kept                           |
| `--actor`        | `trudax~reddit-scraper` | Apify actor id           |
| `--out`          | `topics`| Output file prefix                        |

---

## Using it for LinkedIn instead of Reddit

The same "post + comments → topics" idea is the classic r/linkedinautomation
play, just on LinkedIn. Two ways to do it:

1. **Scrape LinkedIn comments with an Apify LinkedIn actor** (e.g. a
   "LinkedIn Post Comments" actor) or PhantomBuster/Browserflow, export the
   comments as a JSON array of objects with a `text` field.
2. **Feed that export into this tool** — each comment is treated as a document
   and mined identically:

```bash
# linkedin_comments.json: [{"text": "..."}, {"text": "..."}, ...]
python3 reddit_topic_scraper.py --from-file linkedin_comments.json --out li_topics
```

You get the same ranked topics + hook sentences, now sourced from your
LinkedIn audience's own words.

---

## Output example

`topics.topics.md`:

```
# Reddit Topic Brief
- Posts analysed: 50
- Comments analysed: 1 240
- Topics surfaced: 40

## Top content topics (ranked by frequency x engagement)

### 1. connection requests
*18 mentions · engagement 940 · score 552.7*
Ready-made hooks pulled from the threads:
- How do I avoid the LinkedIn ban with connection requests?
- Best way to warm up an account before sending requests?
```

Feed `topics.topics.md` straight into an LLM to draft posts, or use
`topics.topics.csv` in a spreadsheet / n8n / Make pipeline.

---

## Apify actor input (reference)

The tool builds this automatically; shown here for transparency / reuse in
n8n or the Apify console:

```json
{
  "startUrls": [{ "url": "https://www.reddit.com/r/linkedinautomation/hot/" }],
  "skipComments": false,
  "skipUserPosts": true,
  "skipCommunity": true,
  "searchPosts": true,
  "maxPostCount": 50,
  "maxComments": 40,
  "proxy": { "useApifyProxy": true }
}
```

---

## How ranking works

```
recency_weight = 0.1 + 0.9 * 0.5^(age_days / 14)      # fresh threads weigh more, old ones never drop to 0
engagement     = (post_score + num_comments + scraped_comments) * recency_weight
topic.score    = mentions * (1 + sqrt(engagement)) * (1 + 0.25 * num_question_hooks)
```

Tune `--min-mentions` up for tighter/broader-consensus topics, down to surface
niche-but-spicy ones.
