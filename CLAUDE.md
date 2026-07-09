# CLAUDE.md

Repo-wide operational notes. Pipeline-specific gotchas live in [`Monnit/CLAUDE.md`](Monnit/CLAUDE.md) and [`GivenEnergy/CLAUDE.md`](GivenEnergy/CLAUDE.md) — read this file first, then the relevant one.

## Verifying changes

This repo runs unattended, so a change that "looks right" but hasn't actually been run is not verified. After editing any script:

1. Push to `main`.
2. Trigger the relevant workflow manually: `POST /repos/tjlehunte/Claude-SSH-reporter/actions/workflows/{name}.yml/dispatches` with `{"ref":"main"}` (add `"inputs":{"backfill_hours":"720"}` or `{"backfill_days":"14"}` for a historical backfill, depending on the pipeline).
3. Poll `GET .../actions/workflows/{name}.yml/runs?per_page=1` until `status: completed`, then check `conclusion` and pull the job logs (`.../actions/jobs/{id}/logs`) for errors — don't assume success from a green checkmark alone if you changed core logic.
4. Pull the result and check the actual output against what you expected.

Pushing changes to `.github/workflows/*.yml` requires a token with `workflow` scope — GitHub silently rejects the push otherwise. Triggering `workflow_dispatch` via the API needs `repo` scope too.

## Pages publishing (`publish-pages.yml`)

Two triggers, deliberately, for two different pushers:
- `workflow_run` (completion of any of the 4 report workflows) — those push using the default `GITHUB_TOKEN`, and GitHub suppresses `push`-triggered workflow runs for pushes made with that token (loop-prevention), so a plain push trigger would silently never fire for them. `workflow_run` is exempt from that suppression.
- `push`, path-filtered to just the 4 `latest.html` files — for the weekly AI-insights scheduled tasks, which are local routines (not GitHub Actions) that push directly with a personal access token, not `GITHUB_TOKEN`. The loop-suppression above doesn't apply to PAT-authenticated pushes, so a plain push trigger works for these. Without this second trigger, an AI-insights paragraph lands in the repo but never reaches Pages until someone manually re-dispatches this workflow — that gap existed for a while before being caught and fixed (2026-07-09).

If either the report workflows or the AI-insights tasks change how they authenticate their push, re-check whether the corresponding trigger here still fires.

Pages itself is configured with `build_type: workflow` (set via `POST /repos/.../pages`, not the branch-serving mode) specifically so only what this workflow explicitly copies into `_site/` is ever public — the raw `data/*.jsonl` history and scripts are deliberately excluded, even though this repo is already public. Don't switch Pages back to branch-serving mode without re-considering that.

## Shared push concurrency

All four workflows (`monnit-daily-report.yml`, `monnit-weekly-report.yml`, `givenergy-daily-report.yml`, `givenergy-weekly-report.yml`) share one concurrency group (`reporter-writes`) so GitHub Actions queues them instead of racing on `git push` — any two of them pushing to `main` at once risks a non-fast-forward rejection. Each also does `git pull --rebase` before pushing as defense-in-depth against a locally-scheduled AI-insights routine pushing at an unpredictable time outside GitHub Actions' concurrency control.

## Git push on Windows (this dev machine)

Pushing with a token embedded in the URL (`https://<token>@github.com/...`) does **not** avoid Windows Git Credential Manager on this machine — GCM is registered at the system gitconfig level and will still intercept the push and try to open a browser OAuth prompt. Always push with:

```
git -c credential.helper= push "https://<token>@github.com/tjlehunte/Claude-SSH-reporter.git" main
```

with `GIT_TERMINAL_PROMPT=0` set first. If a push hangs or fails confusingly, validate the token first: `curl -H "Authorization: token $TOKEN" https://api.github.com/user`.
