# PostHog Engineering Impact Dashboard

Weave take-home: identify the most impactful engineers on `posthog/posthog` over the last 90 days.

**Live dashboard:** _(GitHub Pages URL added after deploy)_

## Approach

"Impact" here is deliberately **not** LOC, commit count, or PR count — those measure activity, not impact, and are the explicit red flag this assignment calls out. Instead, every engineer gets a composite **Impact Score (0–100)**, computed only from **merged** PRs in the last 90 days (2026-06-17 to 2026-09-15), from three components:

| Component | Weight | What it measures |
|---|---|---|
| **Shipped Scope / Blast Radius** | 45% | Structural reach — did this PR touch files many *other* engineers also depend on, vs. an isolated corner of the codebase? Computed via co-touch centrality across a file→authors map built from the whole window, not by counting directories or lines changed. |
| **Leverage on Others** | 35% | Substantive reviews given to teammates (non-trivial comment body, not rubber-stamp approvals), weighted by how many distinct people were reviewed, with a small bonus for faster turnaround. |
| **Ownership / Trust Routing** | 20% | Who a team actually routes work to for a given subsystem — derived by parsing each PR's conventional-commit scope (`feat(scope): …`), falling back to the dominant top-level file path when a title has no scope, then crediting the top-2 most active engineers per scope. |

All three are **percentile-ranked** (not min-max normalized) across the eligible engineer set, so a single high-volume outlier can't compress everyone else's score. There is **no minimum-activity eligibility floor** — someone who shipped one large piece of infrastructure and spent the rest of the window reviewing can still surface at the top.

Bot accounts (Dependabot, Renovate, CodeRabbit, Greptile, GitHub Actions, etc.) and mechanical PRs (lockfile-only diffs, dependency bumps, bulk codemods) are excluded from scoring but still counted in the displayed PR-count stat, so volume stays visible without driving the ranking.

**Ranking is fully deterministic** (Python, `scripts/score.py`) — no LLM anywhere in the scoring path, so there's no risk of a drifting classifier silently distorting the number. The only place AI touches the output is writing the one-clause "why" text for the top 5 cards' supporting PR evidence, applied *after* ranking is settled.

Every card links directly to 2–3 real PRs on GitHub, so a reviewer can validate the ranking by clicking through rather than trusting a bare number.

### Data

- Source: `gh api graphql` against `PostHog/posthog`, sliced by single calendar day (GitHub's search API caps at 1000 results per query, and this repo merges ~150–250 PRs/day — a single 90-day query would have silently truncated).
- Fetched count is checked against GitHub's own reported count for every day slice (see `data/raw_prs.json` → `meta.per_day_mismatches`).
- Per-PR nesting is capped (20 files, 10 reviews) to stay within the GraphQL rate budget across the full corpus; disclosed here rather than left implicit.
- Raw data and the scoring script are committed to this repo (`data/raw_prs.json`, `data/scores.json`, `scripts/`) so the analysis is reproducible, not just decorative.

### Stack

Local snapshot → Python scoring → static JSON → static HTML → GitHub Pages. No backend, no database, no live GitHub calls at view time — the dashboard loads a pre-computed `data/scores.json` alongside `index.html`, which keeps load time well under the 10s bar and means the page works even if GitHub's API is unavailable when a reviewer opens it.

## Time

_(reported separately per submission instructions — started/stopped on the candidate's own timer)_
