# jd_scraper

Pull LinkedIn job postings via the [TheirStack](https://theirstack.com) API, filtered by
job title, post time, location and more. Results are deduplicated into SQLite so repeat
runs surface only what's new.

## ⚠️ Read this first: the API schema is unverified

This tool was built without network access to `api.theirstack.com`. The endpoint path,
auth scheme and **every request filter field name are educated guesses**, not confirmed
facts. They are documented in `src/jd_scraper/filters.py` (`BODY_FIELD_NOTES`).

The design absorbs being wrong about them:

- Unknown response fields are preserved, never dropped, and every field is optional.
- The full raw payload of each posting is stored in SQLite, so a bad field mapping is a
  local re-parse rather than a re-fetch that costs credits again.
- `extra: {}` in any profile merges raw keys straight into the request body, so you can
  use a filter this tool got wrong without waiting on a code change.

**Run `jd probe` first** (see below). It captures the real schema so the mappings can be
corrected.

## ⚠️ TheirStack bills per job returned

Every posting a search returns consumes credits. Guards built in:

- `limit` in each profile caps total results fetched per run.
- `--max-results N` overrides it per invocation.
- `--dry-run` prints the exact request body and exits — no call, no credits.
- Runs above `JD_CONFIRM_THRESHOLD` (default 100) prompt for confirmation; `--yes` skips
  it for cron.
- Non-429 `4xx` responses are never retried, so a bad filter fails once instead of looping.

Start small. Trust the filters before raising `limit`.

## Setup

```bash
uv sync
cp .env.example .env
# then put your key in .env
```

## Usage

```bash
# 1. Capture the real API schema. Do this before anything else.
uv run jd probe

# 2. Inspect what a profile would send, without calling the API.
uv run jd search --profile profiles/example.yml --dry-run

# 3. Smallest real search.
uv run jd search --profile profiles/example.yml --max-results 5

# 4. Re-run: should report 0 new.
uv run jd search --profile profiles/example.yml --max-results 5

# Browse and export what's stored.
uv run jd list --limit 20
uv run jd export --format csv --out exports/jobs.csv
uv run jd export --format jsonl --out exports/jobs.jsonl   # full raw payloads
```

Useful flags on `search`: `--only-new` (just this run's new postings), `--no-store`
(print without touching the DB), `--yes` (skip the credit prompt).

## Search profiles

Searches are YAML files in `profiles/`. See `profiles/example.yml` for every supported
key. The essentials:

```yaml
name: ml-engineer-us
titles: ["Machine Learning Engineer", "ML Engineer"]
title_exclude: ["Intern"]
posted_within_days: 7              # post time
locations:
  countries: ["US"]
  patterns: ["San Francisco"]
remote: true
sources:
  linkedin_only: true              # enforced client-side on the job URL
limit: 50                          # credit ceiling
extra: {}                          # raw pass-through into the request body
```

`linkedin_only` is enforced client-side by matching `linkedin.com` against each
posting's URL fields. That client-side check is what actually guarantees the filter,
since any request-side source filter is unverified.

## Layout

| Path | Role |
| --- | --- |
| `src/jd_scraper/config.py` | Settings from env / `.env` |
| `src/jd_scraper/models.py` | `SearchProfile` (input), `Job` (output) |
| `src/jd_scraper/filters.py` | Profile → request body; LinkedIn predicate |
| `src/jd_scraper/client.py` | HTTP, retries, pagination, credit cap |
| `src/jd_scraper/store.py` | SQLite schema, upsert, dedup |
| `src/jd_scraper/export.py` | CSV / JSONL writers |
| `src/jd_scraper/cli.py` | Typer commands |

## Tests

```bash
uv run pytest
```

The suite is fully offline, driven by `httpx.MockTransport` and a hand-written fixture.
It proves internal consistency — pagination, the result cap, retry policy, dedup, filter
mapping — but **not** agreement with the real API. Only `jd probe` establishes that.
