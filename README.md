# Lead Finder

A command-line scraper that collects no-code agencies and freelancers from public
partner directories, validates them against a strict schema, and pushes them into
a Notion CRM database.

## What it does and why I built it

I work as a freelance technical contractor for web and no-code agencies. Finding
those agencies is a recurring, manual task: they are listed on public partner
directories (Webflow Experts, Zapier Experts, Bubble, Make), one page at a time,
in a format nobody exports.

This tool automates the collection step and nothing else. It is not a CRM — the
CRM is an existing Notion database. It is not an outreach tool and it does not
send email. It reads public directory pages, extracts structured records, drops
the ones that do not fit a defined contract, skips the ones already in Notion,
and writes the rest to CSV and to the CRM.

The design constraint throughout was that each stage produces something usable on
its own: if Notion is unreachable, the CSV still exists.

## Architecture

```
public directory page
        |
        v
politeness.py    robots.txt check, 1 req/s per host, identifiable User-Agent
        |
        v
scraper.py       crawl4ai renders the page, an LLM cascade extracts records
        |
        v
models.py        Pydantic contract: unknown or malformed records are dropped
        |
        v
notion_client.py duplicate check on "Profil source"
        |
        +-------> leads.csv          (standalone artifact)
        |
        +-------> Notion CRM database
```

For each lead that survives validation and is not already in Notion, the scraper
optionally visits the agency's own site to look for a contact address, a LinkedIn
profile, and a short positioning summary.

## Stack and why

| Choice | Reason |
| --- | --- |
| `crawl4ai` (Playwright) | The directories render their listings client-side. A `requests` + `BeautifulSoup` pass returns an empty shell, so a real browser is required. |
| LLM extraction, not CSS selectors | Four directories with four different markups, all of which change without notice. Selectors broke on nearly every run; a schema-guided extraction survives layout changes. |
| Cascade of three LLM providers | Free and low-cost endpoints are unreliable. If one is down or rate limited, the run continues on the next rather than failing. |
| `pydantic` | The LLM output is untrusted input. The schema in `models.py` is the boundary: enums are closed, URLs are validated, and anything that does not fit is discarded rather than written to the CRM. |
| `requests` against the Notion REST API | Two endpoints are needed. The official SDK would be a dependency for no gain. |
| Notion as the datastore | The CRM already exists there. Adding a local database would mean maintaining two sources of truth. |

## Installation

Requires Python 3.11 or later.

```bash
git clone https://github.com/kromz-dev/leadfinder.git
cd leadfinder

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt

# Downloads the Chromium build crawl4ai drives, and runs its health check.
# Without this step every crawl fails at browser launch.
crawl4ai-setup
crawl4ai-doctor
```

If `crawl4ai-setup` fails behind a proxy or on a minimal container, install the
browser directly:

```bash
python -m playwright install --with-deps chromium
```

## Configuration

Copy `.env.example` to `.env` and fill in the five variables:

```bash
cp .env.example .env
```

| Variable | Purpose |
| --- | --- |
| `NOTION_TOKEN` | Integration token. The target database must be shared with the integration. |
| `NOTION_DATABASE_ID` | 32-character database ID from the database URL. |
| `BAZAARLINK_API_KEY` | First provider in the extraction cascade. |
| `GROQ_API_KEY` | Second provider. |
| `HUGGINGFACE_API_KEY` | Third provider. |

`.env` is git-ignored. Nothing in this repository reads a credential from
anywhere else.

Two values live in code rather than in `.env`, at the top of `politeness.py`:
`CONTACT_EMAIL` and `PROJECT_URL`. Both are advertised in the outgoing
User-Agent so that a site operator seeing this crawler in their logs can reach a
human. If you fork this, change them to your own.

The Notion database must expose these properties, with these types:
`Nom` (title), `Profil source` (url), `Site web` (url), `Localisation` (rich
text), `Spécialités` (multi-select), `Source` (select), `Contact` (rich text),
`Canal` (select), `Statut` (status).

## Usage

```bash
python scraper.py
```

The script picks three directory URLs at random from its source list, crawls
them, and writes results to `leads.csv` in the working directory before pushing
to Notion. Without Notion credentials it still runs and still writes the CSV,
logging each intended insert instead of performing it.

Console output during a run:

```
[robots] https://experts.webflow.com/robots.txt: règles chargées
[rate-limit] https://experts.webflow.com: attente 1.00s
Tentative d'extraction avec openai/auto:free...
Extraction réussie avec openai/auto:free ! (12 leads)
Vérification du doublon Notion pour Northwind Studio...
Deep Scraping du site : https://northwind-studio.example.com/ ...
```

Output format, with the columns the tool writes today — see
[`leads.sample.csv`](leads.sample.csv) for the full anonymised file:

```csv
Nom,Profil source,Site web,Localisation,Spécialités,Source,Contact,Canal,Pitch,Tech Stack,Cible
Northwind Studio,https://experts.webflow.com/profile/northwind-studio,https://northwind-studio.example.com/,"Berlin, Germany","webflow, automation",Webflow Experts,hello@northwind-studio.example.com | https://www.linkedin.com/company/northwind-studio,LinkedIn,Webflow builds for B2B SaaS teams.,"Webflow, Finsweet, Zapier",B2B SaaS
Atlas Ops,https://zapier.com/partnerdirectory/partner/atlas-ops,,,"automation, api, zapier",Zapier Experts,,,,,
```

## Crawling conduct

Every outbound request goes through `politeness.py`, which:

- fetches and applies each host's `robots.txt` before the first request to it,
  honouring any `Crawl-delay` longer than the default;
- enforces a minimum of one second between requests to the same host;
- sends a User-Agent identifying the project with a contact address;
- treats a 5xx or unreachable `robots.txt` as a full disallow rather than as
  permission.

Only public directory pages are crawled. LinkedIn is deliberately not a source:
its terms prohibit scraping.

## Known limitations

Current state, verified rather than assumed:

- **No deduplication within a single run.** Duplicates are checked against
  Notion, but the inserts happen after the whole crawl loop, so the same agency
  appearing twice in one run is written twice. Across runs, deduplication works
  when Notion is configured.
- **No deduplication at all without Notion credentials.** The unconfigured path
  falls through to "insert" instead of "skip".
- **Infinite scroll does not work.** The scroll script is passed as a legacy
  keyword argument that crawl4ai 0.9.x ignores, so only the records present in
  the first render are collected — roughly 10 to 12 per directory instead of the
  full listing.
- **Two of four sources produce nothing.** Webflow Experts and Zapier Experts
  work. `bubble.io/agencies` has never returned a record. Make Partners is
  present in the source list but untested at volume.
- **`Localisation` is filled about 6% of the time.** Most directories render it
  behind a client-side filter that the current crawl does not reach.
- **`Pitch`, `Tech Stack` and `Cible` are extracted but never reach Notion.**
  They are collected during enrichment and written to the CSV, but the Notion
  payload in `notion_client.py` does not include them yet.
- **The CSV appends without rewriting its header.** If the schema in `models.py`
  changes, an existing `leads.csv` keeps the old header and the columns drift.
  Delete the file after a schema change.
- **Two of the three LLM providers are probably dead.** The Groq model ID was
  decommissioned upstream and the Hugging Face model ID does not resolve. In
  practice the cascade runs on its first provider only.
- **No retry on Notion rate limits.** A 429 is treated as a permanent failure
  and the lead is skipped.
- **No test suite.**

Known security surfaces, not yet addressed:

- **No SSRF protection on deep-scrape targets.** The enrichment step fetches a
  URL produced by the LLM. The scheme is validated, the host is not: private,
  loopback and link-local ranges are not blocked.
- **CSV output is not sanitized against formula injection.** Scraped values
  beginning with `=`, `+`, `-` or `@` are written verbatim and will execute when
  the file is opened in a spreadsheet.
- **Scraped page content reaches the LLM unfiltered.** A crawled page can carry
  instructions that influence extraction output, which in turn decides what gets
  fetched next and what is written to the CRM — an indirect prompt injection
  surface.

## Legal note

This tool only collects publicly listed business information. Using the result
for cold outreach is a separate question with its own rules — under GDPR, B2B
prospecting in France requires disclosing where the data came from at first
contact and providing a working opt-out, and sole traders are treated as
natural persons requiring opt-in. Check current guidance before running a
campaign. This is not legal advice.

## License

MIT — see [LICENSE](LICENSE).
