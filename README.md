# jd_scraper

Pull LinkedIn job postings via the [TheirStack](https://theirstack.com) API, filtered by
job title, post time, location and more. Results are deduplicated into SQLite so repeat
runs surface only what's new.

Field mappings are written against the published reference for
[`POST /v1/jobs/search`](https://theirstack.com/en/docs/api-reference/jobs/search_jobs_v1).

## Credits: 1 per job returned

Every posting a search returns costs a credit, so the tool is built to avoid returning
things you didn't want:

- **`linkedin_only` filters server-side** via `url_domain_or`. Non-LinkedIn postings are
  never returned and never billed. A client-side URL check backstops it.
- **`--preview` is free.** Blurred results, no credits consumed — the right way to
  validate a new profile before paying for it.
- **Incremental by default.** After a profile's first run, subsequent searches ask only
  for postings *discovered since* the last run (`discovered_at_gte`) and exclude ids
  already stored (`job_id_not`). You don't pay again for jobs you already have. See below.
- `limit` in each profile caps results per run; `--max-results N` overrides per run.
- `--dry-run` prints the request body and exits. No call, no credits.
- Runs above `JD_CONFIRM_THRESHOLD` (default 100) prompt for confirmation; `--yes` skips
  it for cron.
- `400`/`422` are never retried — a bad filter fails once instead of looping. `402`
  reports out-of-credits distinctly.
- `truncated_results` is surfaced after each run, so you know when matches were withheld
  because the account ran short of credits.

## Incremental fetching

Deduplicating after the fetch would still cost a credit per duplicate, so already-seen
postings are excluded *server-side*:

- `discovered_at_gte` — only postings TheirStack discovered since this profile's last
  run. `discovered_at` is the right clock here; `date_posted` can predate discovery, so
  a job posted last week but indexed today still reaches you.
- `job_id_not` — ids already in the database, newest first and capped at 500 so the
  request body stays bounded.

The watermark is rewound by `incremental_overlap_minutes` (default 60) so a posting
indexed while the previous run was in flight isn't skipped. The id exclusion cleans up
the duplicates that overlap would otherwise let back in — the two work as a pair.

The first run for a profile has no watermark and fetches normally. To deliberately
re-fetch everything:

```bash
uv run jd search --profile profiles/atlanta-ml.yml --full
```

Turn it off permanently for a profile with `incremental: false`.

Worth knowing: the watermark is keyed on the **profile name** and read from the `runs`
table. Renaming a profile resets its history, and `--no-store` runs are not recorded, so
they don't advance it.

## "In this city OR remote"

The API ANDs every filter, and remote-ness is a boolean field while location is matched
against the job's location *text*. So `remote: true` alongside a location pattern means
"remote jobs whose location says Atlanta" — narrower, not wider.

Matching the literal word `"Remote"` in the location pattern doesn't work either: plenty
of genuinely remote postings list their location as `"United States"` or the company's
head office.

`include_remote: true` expresses the OR properly by issuing two searches and merging:

```yaml
locations:
  countries: ["US"]
  patterns: ["Atlanta"]
include_remote: true
```

1. the location search — `job_location_pattern_or: ["Atlanta"]`, no remote flag
2. the remote search — `remote: true`, location pattern dropped, country kept

Results are unioned and deduplicated by job id. The run's `limit` is split evenly between
the two so neither starves the other. **A job matching both is billed twice** — once per
search — which is the price of an OR the API won't do for you.

Setting `remote` explicitly disables the split: that's read as "I want the narrow AND".

## Every search needs a date or company filter

The API rejects a search unless at least one of these is set:

`posted_within_days` · `posted_after` · `posted_before` · `companies`

A title or location filter **alone is not enough**. This is validated locally before the
request is sent, so you get a readable error instead of a wasted round trip.

## Setup

```bash
uv sync
cp .env.example .env
# then put your key in .env
```

Without `uv`, use a plain venv instead — but don't mix the two in one `.venv`, which
leaves a half-installed environment behind:

```bash
python3 -m venv .venv \
  && source .venv/bin/activate \
  && pip install -e ".[dev]"
```

Then drop the `uv run` prefix from every command below.

**If you see `ModuleNotFoundError: No module named 'jd_scraper'`** while `.venv/bin/jd`
exists, the environment is half-built. Rebuild it:

```bash
rm -rf .venv \
  && uv sync
```

## Usage

```bash
# Build a profile by answering prompts (no API call, no credits).
uv run jd profile new

# See the exact request body it produces.
uv run jd search --profile profiles/example.yml --dry-run

# Validate the filters for free — blurred results, no credits.
uv run jd search --profile profiles/example.yml --preview

# Smallest real search.
uv run jd search --profile profiles/example.yml --max-results 5

# Re-run: should report 0 new.
uv run jd search --profile profiles/example.yml --max-results 5

# Browse and export.
uv run jd list --limit 20
uv run jd export --format csv --out exports/jobs.csv
uv run jd export --format jsonl --out exports/jobs.jsonl   # full raw payloads
```

Other `search` flags: `--only-new`, `--no-store`, `--totals` (ask for total match
counts — slower, it reads the whole dataset), `--yes`.

`jd probe` captures the live OpenAPI spec and a 1-result sample response to
`docs/api-snapshot.json` — useful if the API changes under you.

## Search profiles

Searches are YAML files in `profiles/`. Build and revise them interactively:

```bash
uv run jd profile new                      # answer prompts -> writes profiles/<name>.yml
uv run jd profile edit profiles/ml-us.yml  # current values are the defaults; Enter keeps them
uv run jd profile show profiles/ml-us.yml  # print the profile and the body it would send
```

The wizard never calls the API, so iterating on criteria is free. Each command prints the
resulting request body. Filters it doesn't prompt for (`description_pattern`, `posted_after`,
`extra`, …) are carried through untouched when editing, so hand-written YAML is never
silently dropped.

See `profiles/example.yml` for every supported key. The essentials:

```yaml
name: ml-engineer-us
titles: ["Machine Learning Engineer"]   # keyword match, all words in any order
title_exclude: ["Intern"]
posted_within_days: 7                   # required (or posted_after/before/companies)
locations:
  countries: ["US"]
  patterns: ["San Francisco"]
remote: true
seniority: [mid_level, senior]          # c_level|staff|senior|junior|mid_level
exclude_recruiting_agencies: true
sources:
  linkedin_only: true                   # server-side url_domain_or
limit: 50                               # credit ceiling
extra: {}                               # raw pass-through into the request body
```

**Title matching is keyword-based, not exact.** Every word in a pattern must appear in the
title, in any order, case-insensitively — so `"machine learning engineer"` also matches
`"Senior Machine Learning Engineer, Platform"`. Use `title_pattern` for regex instead.

## Layout

| Path | Role |
| --- | --- |
| `src/jd_scraper/config.py` | Settings from env / `.env` |
| `src/jd_scraper/models.py` | `SearchProfile` (input), `Job` (output) |
| `src/jd_scraper/filters.py` | Profile → request body; LinkedIn predicate |
| `src/jd_scraper/client.py` | HTTP, retries, pagination, credit cap |
| `src/jd_scraper/store.py` | SQLite schema, upsert, dedup |
| `src/jd_scraper/export.py` | CSV / JSONL writers |
| `src/jd_scraper/wizard.py` | Interactive profile builder / editor |
| `src/jd_scraper/cli.py` | Typer commands |

The `Job` model keeps unknown response fields rather than dropping them, and the full raw
payload of each posting is stored in SQLite — so if the API adds or renames a field, the
data is still on disk and re-parsing is a local migration, not a re-fetch you pay for.

## Tests

```bash
uv run pytest
```

47 tests, fully offline, driven by `httpx.MockTransport`. They cover filter mapping, the
mandatory-filter rule, LinkedIn detection (including lookalike hosts like
`notlinkedin.com`), pagination and the result cap, retry policy, and dedup.

They run against a hand-written fixture, so they verify this tool's behaviour against the
documented schema — not the live API's actual responses. A real `jd search --preview` run
is what confirms the end-to-end path.
