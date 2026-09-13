# Claude-SSH-reporter

Automated daily/weekly reporting pipelines. Two independent pipelines live side by side in this repo, each with its own data, reports, and scripts:

- [`Monnit/`](Monnit/README.md) — 16-room Monnit home sensor network (temperature, humidity, dewpoint, condensation risk, current/power draw).
- [`Givenergy/`](Givenergy/README.md) — Givenergy inverter energy flows (solar generation, battery charge/discharge, grid import/export, home consumption).

Both follow the same shape: a scheduled GitHub Actions workflow fetches new data, dedups it into a JSONL history file, generates a self-contained HTML report (charts embedded as base64 PNGs), and commits/pushes the result. See each pipeline's own README for specifics.

**Viewing the reports**: GitHub doesn't render `.html` files in its file browser (it shows source), so the 4 latest reports are also published to GitHub Pages at **https://tjlehunte.github.io/Claude-SSH-reporter/** via [`publish-pages.yml`](.github/workflows/publish-pages.yml) — deliberately curated to include only the 4 `latest.html` files, never the raw `data/` history or scripts, since this repo is public.

No ongoing manual intervention or Claude API usage is required for the mechanical pipelines — they run autonomously. AI-narrative commentary (where present) is added by a locally scheduled routine, not a GitHub Action — see the relevant pipeline's CLAUDE.md.
