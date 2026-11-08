# Paper Radar

A personal alert system that finds new papers on my research topic
(electrokinetic, diffusio-kinetic and thermally driven transport in micro/nano
channels and nanopores, and their applications) and emails me a digest every
morning at about 10:00 IST.

It runs entirely on free services:

| Part | Service |
|---|---|
| New preprints | arXiv API |
| New journal papers, citations, authors | OpenAlex API (free key) |
| Relevance filtering and one-line summaries | NVIDIA API (free key) |
| Daily schedule | GitHub Actions |
| Web page with every paper found | GitHub Pages (`docs/`) |
| Email | Gmail (app password) |

## How it finds papers

1. **Keyword searches** on arXiv and OpenAlex (see `queries` in `config.yaml`).
2. **Citation tracking**: any new paper that cites one of the `key_papers`.
3. **Followed authors**: every new paper by the people in `authors`.

Every candidate is read by an AI model, which gives it a relevance score (0-10),
a category and a one-sentence reason. Scores of 8+ are "Highly relevant", 5-7 are
"Possibly relevant", and lower ones are dropped. Papers by followed authors are
always shown in their own section. Papers in the preferred journals/publishers
get a badge and rank higher.

## Changing things

Everything is in **`config.yaml`**; open it on GitHub, click the pencil icon,
edit and commit. The next run uses the new settings.

- Topic text / exclusions: `topic`
- Key papers: `key_papers` (a DOI is enough)
- Authors: `authors` (OpenAlex author ids)
- Thresholds: `ai.high_score`, `ai.keep_score`
- AI model order: `ai.models`

**Run it now:** Actions tab -> "Paper Radar" -> "Run workflow". You can enter a
number of days to look back.

**Change the time:** edit the `cron` line in `.github/workflows/radar.yml`
(it is in UTC; 04:30 UTC = 10:00 IST). GitHub may start scheduled runs a little late.

**Pause it:** Actions tab -> "Paper Radar" -> "..." -> "Disable workflow".

## Keys

Keys are stored as encrypted repository secrets (Settings -> Secrets and
variables -> Actions), never in the code:
`NVIDIA_API_KEY`, `OPENALEX_API_KEY`, `MAIL_ID`, `MAIL_TOKEN`, `MAIL_TO`.

## Files

- `radar/` - the Python code (`main.py` runs everything)
- `data/state.json` - which papers were already sent (so nothing repeats)
- `docs/` - the web page and its data
