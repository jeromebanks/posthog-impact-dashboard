#!/usr/bin/env python3
"""
Deterministic scoring: no LLM anywhere in this file. Reads data/raw_prs.json,
computes the three weighted components (Shipped Scope 45%, Leverage on Others 35%,
Domain Stewardship 20%), rank-normalizes each, and writes data/scores.json.
"""
import json
import math
import re
from collections import Counter, defaultdict

IN_PATH = "data/raw_prs.json"
OUT_PATH = "data/scores.json"
WHY_OVERRIDES_PATH = "data/why_overrides.json"
REVIEW_COMPLETIONS_PATH = "data/review_completions.json"

BOT_MARKERS = [
    "[bot]", "dependabot", "renovate", "coderabbitai", "greptile",
    "sourcery", "copilot", "codecov", "snyk", "semgrep", "github-actions",
    "stamphog", "graphite-app", "posthog-security-review-bot",
    "exe-dev-github-integration",
]
LOCKFILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "uv.lock",
}
SCOPE_RE = re.compile(r"^[a-zA-Z]+\(([^)]+)\):")


def is_bot(login, typename):
    if typename == "Bot":
        return True
    l = (login or "").lower()
    if l.endswith("bot"):
        return True
    return any(m in l for m in BOT_MARKERS)


def is_mechanical(title, files):
    t = title.lower()
    if "chore(deps)" in t or t.startswith("bump ") or " bump " in t:
        return True
    if files and all(f.rsplit("/", 1)[-1] in LOCKFILES for f in files):
        return True
    return False


def parse_scope(title, files):
    m = SCOPE_RE.match(title.strip())
    if m:
        return m.group(1), False
    segs = [f.split("/")[0] for f in files if f]
    if not segs:
        return "unknown", True
    return Counter(segs).most_common(1)[0][0], True


def rank_normalize(values):
    """Percentile rank 0-100, ties averaged. values: dict[key] -> float."""
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][0]: 0.0 if items[0][1] == 0 else 100.0}
    result = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg_rank = (i + j) / 2
        pct = 0.0 if items[i][1] == 0 else avg_rank / (n - 1) * 100
        for k in range(i, j + 1):
            result[items[k][0]] = pct
        i = j + 1
    return result


def main():
    with open(IN_PATH) as f:
        raw = json.load(f)
    prs = raw["prs"]
    meta = raw["meta"]
    try:
        with open(REVIEW_COMPLETIONS_PATH) as f:
            review_completions_doc = json.load(f)
            review_completions = review_completions_doc["reviews"]
    except FileNotFoundError:
        review_completions_doc = {"meta": {}}
        review_completions = {}

    file_to_authors = defaultdict(set)      # file -> set(login)  [non-bot, non-mechanical]
    pr_records = []                          # cleaned eligible PRs
    scope_fallback_count = 0
    mechanical_count = 0
    bot_authored_count = 0

    for pr in prs:
        author = pr["author"]
        if author is None:
            continue
        login, typename = author["login"], author["__typename"]
        if is_bot(login, typename):
            bot_authored_count += 1
            continue
        files = [n["path"] for n in pr["files"]["nodes"]]
        mech = is_mechanical(pr["title"], files)
        if mech:
            mechanical_count += 1
        scope, fell_back = parse_scope(pr["title"], files)
        if fell_back:
            scope_fallback_count += 1
        reviews = []
        completed_reviews = review_completions.get(str(pr["number"]))
        review_nodes = completed_reviews if completed_reviews is not None else pr["reviews"]["nodes"]
        for r in review_nodes:
            if completed_reviews is not None:
                r_login = r.get("login")
                r_typename = r.get("typename")
                body_len = r.get("body_len", 0)
                submitted_at = r.get("submitted_at")
            else:
                r_author = r.get("author")
                if r_author is None:
                    continue
                r_login = r_author["login"]
                r_typename = r_author.get("__typename")
                body_len = len((r.get("body") or "").strip())
                submitted_at = r["submittedAt"]
            if r_login is None or is_bot(r_login, r_typename):
                continue
            if r_login == login:
                continue
            reviews.append({
                "login": r_login,
                "state": r["state"],
                "body_len": body_len,
                "submitted_at": submitted_at,
            })
        rec = {
            "number": pr["number"],
            "title": pr["title"],
            "url": pr["url"],
            "author": login,
            "mergedAt": pr["mergedAt"],
            "createdAt": pr["createdAt"],
            "files": files,
            "mechanical": mech,
            "scope": scope,
            "scope_fallback": fell_back,
            "reviews": reviews,
        }
        pr_records.append(rec)
        if not mech:
            for fpath in files:
                file_to_authors[fpath].add(login)

    # ---- Component 1: Shipped Scope / Blast Radius (45%) ----
    # Mean co-touch weight within each PR, then mean across an engineer's PRs:
    # a per-PR *rate* of structural
    # consequence, so this can't be won by sheer PR volume. An earlier version
    # summed contributions across all of a person's PRs, which -- despite using
    # co-touch centrality instead of a naive directory/PR count -- still made
    # the component mechanically correlate with total output (whoever merges
    # the most PRs racks up the most summed terms). Caught during self-review:
    # the resulting top 10 were dominated by people with 3-7x the average PR
    # count, reproducing exactly the "PR-count-in-disguise" pattern this
    # metric was redesigned to avoid. Small-sample noise from averaging over
    # few PRs is the tradeoff, mitigated by the evidence-confidence label.
    shipped_sum = defaultdict(float)
    shipped_count = defaultdict(int)
    pr_cotouch = {}  # pr number -> contribution
    for pr in pr_records:
        if pr["mechanical"]:
            pr_cotouch[pr["number"]] = 0.0
            continue
        file_weights = []
        for fpath in pr["files"]:
            others = len(file_to_authors[fpath] - {pr["author"]})
            file_weights.append(math.sqrt(others))
        contribution = sum(file_weights) / len(file_weights) if file_weights else 0.0
        pr_cotouch[pr["number"]] = contribution
        shipped_sum[pr["author"]] += contribution
        shipped_count[pr["author"]] += 1
    shipped_raw = {a: shipped_sum[a] / shipped_count[a] for a in shipped_sum}

    # ---- Component 2: Leverage on Others (35%) ----
    leverage_count = defaultdict(int)
    leverage_distinct = defaultdict(set)
    for pr in pr_records:
        for r in pr["reviews"]:
            if r["state"] not in ("APPROVED", "CHANGES_REQUESTED"):
                continue
            if r["body_len"] <= 20:
                continue
            leverage_count[r["login"]] += 1
            leverage_distinct[r["login"]].add(pr["author"])

    leverage_raw = {
        login: leverage_count[login] * math.log1p(len(leverage_distinct[login]))
        for login in leverage_count
    }
    # ---- Component 3: Domain Stewardship (20%) ----
    scope_activity = defaultdict(lambda: defaultdict(float))
    for pr in pr_records:
        if pr["mechanical"]:
            continue
        scope_activity[pr["scope"]][pr["author"]] += 1.0
        for r in pr["reviews"]:
            if r["state"] not in ("APPROVED", "CHANGES_REQUESTED") or r["body_len"] <= 20:
                continue
            scope_activity[pr["scope"]][r["login"]] += 1.0

    stewardship_raw = defaultdict(float)
    for scope, engineers in scope_activity.items():
        ranked = sorted(engineers.items(), key=lambda kv: (-kv[1], kv[0]))
        if len(ranked) >= 1:
            stewardship_raw[ranked[0][0]] += 2.0
        if len(ranked) >= 2:
            stewardship_raw[ranked[1][0]] += 1.0

    # ---- Universe of engineers: anyone who authored or substantively reviewed ----
    engineers = set(shipped_raw) | set(leverage_count) | set(stewardship_raw)

    shipped_pct = rank_normalize({e: shipped_raw.get(e, 0.0) for e in engineers})
    leverage_pct = rank_normalize({e: leverage_raw.get(e, 0.0) for e in engineers})
    stewardship_pct = rank_normalize({e: stewardship_raw.get(e, 0.0) for e in engineers})

    # ---- Displayed-only stats ----
    authored_prs = defaultdict(list)
    for pr in pr_records:
        authored_prs[pr["author"]].append(pr)

    collaborators = defaultdict(set)
    for pr in pr_records:
        for r in pr["reviews"]:
            collaborators[pr["author"]].add(r["login"])
            collaborators[r["login"]].add(pr["author"])

    merge_hours = defaultdict(list)
    for pr in pr_records:
        try:
            from datetime import datetime
            t0 = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(pr["mergedAt"].replace("Z", "+00:00"))
            merge_hours[pr["author"]].append(max(0.0, (t1 - t0).total_seconds() / 3600.0))
        except Exception:
            pass

    results = []
    for e in engineers:
        score = 0.45 * shipped_pct.get(e, 0) + 0.35 * leverage_pct.get(e, 0) + 0.20 * stewardship_pct.get(e, 0)
        n_authored = len(authored_prs.get(e, []))
        n_reviews = leverage_count.get(e, 0)
        qualifying = n_authored + n_reviews
        if qualifying >= 5:
            conf = "High"
        elif qualifying >= 2:
            conf = "Medium"
        else:
            conf = "Low"
        mh = merge_hours.get(e, [])
        median_merge_h = sorted(mh)[len(mh) // 2] if mh else None
        top_shipped_prs = sorted(
            [p for p in authored_prs.get(e, []) if not p["mechanical"]],
            key=lambda p: pr_cotouch.get(p["number"], 0.0),
            reverse=True,
        )[:2]
        best_review = None
        best_review_score = -1
        for pr in pr_records:
            for r in pr["reviews"]:
                if r["login"] != e:
                    continue
                if r["state"] not in ("APPROVED", "CHANGES_REQUESTED") or r["body_len"] <= 20:
                    continue
                if r["body_len"] > best_review_score:
                    best_review_score = r["body_len"]
                    best_review = {"number": pr["number"], "title": pr["title"], "url": pr["url"], "author": pr["author"]}

        results.append({
            "login": e,
            "impact_score": round(score, 2),
            "components": {
                "shipped_scope": round(shipped_pct.get(e, 0), 1),
                "leverage_on_others": round(leverage_pct.get(e, 0), 1),
                "domain_stewardship": round(stewardship_pct.get(e, 0), 1),
            },
            "confidence": {
                "label": conf,
                "authored_prs": n_authored,
                "substantive_reviews": n_reviews,
            },
            "stats": {
                "merged_prs": n_authored,
                "distinct_collaborators": len(collaborators.get(e, set())),
                "median_hours_to_merge": round(median_merge_h, 1) if median_merge_h is not None else None,
            },
            "evidence": {
                "top_shipped": [{"number": p["number"], "title": p["title"], "url": p["url"]} for p in top_shipped_prs],
                "best_review": best_review,
            },
        })

    results.sort(key=lambda r: r["impact_score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1

    from datetime import datetime, timezone
    try:
        with open(WHY_OVERRIDES_PATH) as f:
            why_overrides = json.load(f)
    except FileNotFoundError:
        why_overrides = {}

    out = {
        "meta": {
            **meta,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "eligible_engineers": len(engineers),
            "bot_authored_prs_excluded": bot_authored_count,
            "mechanical_prs_excluded": mechanical_count,
            "scope_fallback_fired_on": scope_fallback_count,
            "scope_fallback_pct": round(100 * scope_fallback_count / max(1, len(pr_records)), 1),
            "methodology": (
                "Impact = shipped scope + leverage on teammates + domain stewardship. "
                "GitHub cannot reveal total employee impact, so this dashboard ranks "
                "observable engineering impact over the last 90 days. Activity volume "
                "is shown for context but is not scored."
            ),
            "review_completion": review_completions_doc.get("meta", {}),
        },
        "rankings": results,
        "why_overrides": why_overrides,
    }
    with open(OUT_PATH, "w") as f:
        json.dump(out, f, indent=2)

    print(f"Wrote {OUT_PATH}: {len(results)} eligible engineers")
    print(f"Bot-authored PRs excluded: {bot_authored_count}")
    print(f"Mechanical PRs excluded from scoring: {mechanical_count}")
    print(f"Scope fallback fired on {out['meta']['scope_fallback_pct']}% of PRs")
    print("\nTop 10:")
    for r in results[:10]:
        print(f"  #{r['rank']:>2} {r['login']:<25} score={r['impact_score']:<6} "
              f"shipped={r['components']['shipped_scope']:<5} "
              f"leverage={r['components']['leverage_on_others']:<5} "
              f"stewardship={r['components']['domain_stewardship']:<5} "
              f"conf={r['confidence']['label']}")


if __name__ == "__main__":
    main()
