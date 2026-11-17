# CLAUDE.md

Repo-wide operational notes. Pipeline-specific gotchas live in [`Monnit/CLAUDE.md`](Monnit/CLAUDE.md) and [`Givenergy/CLAUDE.md`](Givenergy/CLAUDE.md) — read this file first, then the relevant one.

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

**CDN caching (not configurable from this repo):** GitHub Pages serves through Fastly with `Cache-Control: max-age=600` — a deploy can be fully live server-side while a browser still shows a copy up to 10 minutes old, and a normal refresh doesn't reliably bypass it. This isn't a pipeline bug if a change "isn't showing up" — verify with `curl -sI <url>` (check the `Age` header) or a cache-busting query string (`?v=x`) before assuming the publish failed.

## Push concurrency and retries

Each of the four report workflows (`monnit-daily-report.yml`, `monnit-weekly-report.yml`, `givenergy-daily-report.yml`, `givenergy-weekly-report.yml`) has its **own** concurrency group (`${{ github.workflow }}`). They used to share one group (`reporter-writes`), but GitHub keeps only one *pending* run per group and cancels the rest, so a late daily run could silently cancel a waiting weekly one.

Because they no longer queue behind each other, two workflows can push to `main` at the same moment — e.g. when the weekly-report-freshness-check routine dispatches both weekly workflows together. A single `git pull --rebase && git push` is not enough: on 2026-11-16 the Monnit weekly lost a same-second race to the Givenergy weekly (`cannot lock ref 'refs/heads/main'`), the report didn't land until the late cron run hours later, and the AI-insights routine missed it. So every workflow's push step now retries pull --rebase + push up to 5 times with a short backoff. The workflows commit disjoint files, so the rebase never conflicts. The cloud AI-insights routines push from outside GitHub Actions and have their own one-shot `git pull --rebase` retry.

## Git push on Windows (this dev machine)

Pushing with a token embedded in the URL (`https://<token>@github.com/...`) does **not** avoid Windows Git Credential Manager on this machine — GCM is registered at the system gitconfig level and will still intercept the push and try to open a browser OAuth prompt. Always push with:

```
git -c credential.helper= push "https://<token>@github.com/tjlehunte/Claude-SSH-reporter.git" main
```

with `GIT_TERMINAL_PROMPT=0` set first. If a push hangs or fails confusingly, validate the token first: `curl -H "Authorization: token $TOKEN" https://api.github.com/user`.
