# Usage Guide

This project provides a complete pipeline for automated crawling of ICLR papers, keyword and optimization-direction extraction, and report generation. This document explains CLI usage and `.env` environment variable configuration.

## Environment Setup
- Install and use `uv` for dependency management and running.
- On first entering the project directory, run:
  - `uv sync` to install dependencies and create a local virtual environment
  - `cp .env_example .env` and fill in `.env` as needed

## Environment Variables (.env)
- `YEAR`: analysis year, supports `2025` or `2026`
- `OPENREVIEW_USERNAME`: OpenReview login username (required)
- `OPENREVIEW_PASSWORD`: OpenReview login password (required)
- `OPENAI_BASE_URL`: LLM API base, compatible with DeepSeek/OpenAI-style endpoints (optional)
- `OPENAI_API_KEY`: LLM API key (optional; if not set, LLM extraction is skipped)
- `OPENAI_MODEL`: LLM model name, e.g., `deepseek-reasoner` (optional)
- `OPENREVIEW_API_BASE`: OpenReview API base, default `https://api.openreview.net` (optional)
- `USER_AGENT`: HTTP UA identifier, default `iclr-analysis/1.0` (optional)

Note: `python-dotenv` automatically loads `.env`; when `YEAR=2026`, the crawl stage only counts submissions with average review score ≥ 6.

## CLI Usage (run with uv)

### One-Click Pipeline
- Run the full flow (crawl → extract → analyze):
  - `uv run python main.py pipeline [options]`
- Common options:
  - `--crawl-limit <int>`: max number to crawl, unlimited by default
  - `--crawl-resume/--no-crawl-resume`: whether to skip completed items based on checkpoint, enabled by default
  - `--crawl-adaptive/--no-crawl-adaptive`: whether to enable adaptive concurrency, enabled by default
  - `--crawl-max-concurrency <int>`: max crawl concurrency, default 5 (range 1–10)
  - `--crawl-force-refresh`: force refresh index, ignore local cache
  - `--extract-limit <int>`: max number to extract, unlimited by default
  - `--extract-resume/--no-extract-resume`: whether to skip completed items based on checkpoint, enabled by default
  - `--extract-max-concurrency <int>`: max extract concurrency, default 5 (range 1–50)

Examples:
- `YEAR=2025 uv run python main.py pipeline --crawl-max-concurrency 8 --extract-max-concurrency 8`
- `YEAR=2026 uv run python main.py pipeline --crawl-limit 2000 --crawl-max-concurrency 6`

### Run in Steps
- Crawl only:
  - `uv run python crawler.py crawl --limit 1000 --resume --adaptive --max-concurrency 10 --force-refresh`
- Extract only:
  - `uv run python extractor.py extract --limit 1000 --resume --max-concurrency 10`
- Generate report only:
  - `uv run python analyzer.py`

## Common Workflows
- Run 2025 full analysis:
  - `YEAR=2025 uv run python main.py pipeline`
- Run 2026 analysis (avg ≥ 6 samples only):
  - `YEAR=2026 uv run python main.py pipeline`
- Update extraction and report (existing crawl data):
  - `uv run python extractor.py extract && uv run python analyzer.py`

## Output Directory Structure
- `data/{YEAR}/meta/`: paper metadata JSON
- `data/{YEAR}/raw/`: original paper PDFs
- `data/{YEAR}/extracted/`: extracted structured JSON (including LLM tags and optimization phrases)
- `data/{YEAR}/state/`: `crawl_checkpoint.json` and `extract_checkpoint.json`
- `reports/{YEAR}/`: report artifacts (`index.html`, `report.md`, `wordcloud.png`, `keywords_top20.html`, `optimizations_top20.html`)

## Notes and Recommendations
- LLM extraction is optional: without `OPENAI_API_KEY`, the pipeline uses metadata and rule-based extraction only.
- Concurrency and adaptive mode: when the network is unstable or rate-limited, reduce `max_concurrency` and disable `adaptive` to localize issues.
- 2026 sample definition: only submissions with average review score ≥ 6 are included; distributions may differ from the officially accepted set.

## Troubleshooting
- OpenReview authentication failure: check `OPENREVIEW_USERNAME` and `OPENREVIEW_PASSWORD`.
- No LLM output: ensure `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `OPENAI_MODEL` are set.
- Crawl exceptions: try `--crawl-force-refresh` or lower concurrency; check network proxy and API availability.