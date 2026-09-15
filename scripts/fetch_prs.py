#!/usr/bin/env python3
"""
Fetch all merged PRs for PostHog/posthog in the last 90 days via GraphQL,
sliced by single calendar day to stay safely under GitHub search's
1000-result-per-query cap. Per-PR nesting is capped (files: 20, reviews: 10)
to stay within the GraphQL cost budget across ~15k PRs -- disclosed in the
methodology rather than silently truncating the date range itself.
"""
import json
import subprocess
import sys
import time
from datetime import date, timedelta

REPO = "PostHog/posthog"
START = date(2026, 6, 17)
END = date(2026, 9, 15)
OUT_PATH = "data/raw_prs.json"

QUERY = """
query($cursor: String) {
  rateLimit { remaining resetAt }
  search(query: "repo:%s is:pr is:merged merged:%s..%s", type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number
        title
        url
        mergedAt
        createdAt
        changedFiles
        author { login __typename }
        files(first: 20) { nodes { path } }
        reviews(first: 10) { nodes { author { login } state body submittedAt } }
        labels(first: 5) { nodes { name } }
      }
    }
  }
}
"""


def gh_graphql(query_text, cursor=None, retries=5):
    for attempt in range(retries):
        args = ["gh", "api", "graphql", "-f", f"query={query_text}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        result = subprocess.run(args, capture_output=True, text=True)
        if result.returncode == 0:
            return json.loads(result.stdout)
        sys.stderr.write(f"  retry {attempt+1}: {result.stderr[:300]}\n")
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gh api graphql failed after {retries} retries")


def fetch_day(day_str):
    prs = []
    cursor = None
    total_reported = None
    while True:
        q = QUERY % (REPO, day_str, day_str)
        data = gh_graphql(q, cursor)
        remaining = data["data"]["rateLimit"]["remaining"]
        search = data["data"]["search"]
        total_reported = search["issueCount"]
        prs.extend(search["nodes"])
        if not search["pageInfo"]["hasNextPage"]:
            break
        cursor = search["pageInfo"]["endCursor"]
        if remaining < 200:
            sys.stderr.write(f"  rate limit low ({remaining}), pausing 60s\n")
            time.sleep(60)
    return prs, total_reported, remaining


def main():
    all_prs = {}
    day = START
    mismatches = []
    day_count = 0
    total_days = (END - START).days + 1
    while day <= END:
        day_str = day.isoformat()
        prs, reported, remaining = fetch_day(day_str)
        fetched = len(prs)
        if fetched != reported:
            mismatches.append({"day": day_str, "reported": reported, "fetched": fetched})
        for pr in prs:
            all_prs[pr["number"]] = pr
        day_count += 1
        print(f"[{day_count}/{total_days}] {day_str}: reported={reported} fetched={fetched} "
              f"running_total={len(all_prs)} rate_remaining={remaining}", flush=True)
        day = day + timedelta(days=1)

    with open(OUT_PATH, "w") as f:
        json.dump({
            "meta": {
                "repo": REPO,
                "window_start": START.isoformat(),
                "window_end": END.isoformat(),
                "total_prs": len(all_prs),
                "per_day_mismatches": mismatches,
                "caps": {"files_per_pr": 20, "reviews_per_pr": 10},
            },
            "prs": list(all_prs.values()),
        }, f)

    print(f"\nDone. {len(all_prs)} unique merged PRs written to {OUT_PATH}")
    if mismatches:
        print(f"WARNING: {len(mismatches)} day(s) had reported != fetched count:")
        for m in mismatches:
            print(f"  {m}")


if __name__ == "__main__":
    main()
