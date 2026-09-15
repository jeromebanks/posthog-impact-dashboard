#!/usr/bin/env python3
"""
Fetch all merged PRs for PostHog/posthog in the last 90 days via GraphQL,
sliced by single calendar day to stay safely under GitHub search's
1000-result-per-query cap. The initial query fetches 100 files and 10 reviews
per PR, then cursor-paginates every truncated nested connection. This keeps
individual query cost predictable without silently dropping evidence.
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
  rateLimit { cost remaining resetAt }
  search(query: "repo:%s is:pr is:merged merged:%s..%s", type: ISSUE, first: 50, after: $cursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        id
        number
        title
        url
        mergedAt
        createdAt
        changedFiles
        author { login __typename }
        files(first: 100) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { path }
        }
        reviews(first: 10) {
          totalCount
          pageInfo { hasNextPage endCursor }
          nodes { author { login __typename } state body submittedAt }
        }
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


def paginate_nested(prs, field, node_selection, page_size, batch_size=40):
    """Complete a nested PR connection in cost-bounded alias batches."""
    pending = [
        (pr, pr[field]["pageInfo"]["endCursor"])
        for pr in prs
        if pr[field]["pageInfo"]["hasNextPage"]
    ]
    pages = 0
    while pending:
        batch, pending = pending[:batch_size], pending[batch_size:]
        aliases = []
        for i, (pr, cursor) in enumerate(batch):
            aliases.append(
                f'r{i}: node(id: {json.dumps(pr["id"])}) {{ '
                f'... on PullRequest {{ {field}(first: {page_size}, after: {json.dumps(cursor)}) {{ '
                f'totalCount pageInfo {{ hasNextPage endCursor }} nodes {{ {node_selection} }} '
                f'}} }} }}'
            )
        query = "query { rateLimit { cost remaining resetAt } " + " ".join(aliases) + " }"
        data = gh_graphql(query)["data"]
        remaining = data["rateLimit"]["remaining"]
        for i, (pr, _) in enumerate(batch):
            page = data[f"r{i}"][field]
            pr[field]["nodes"].extend(page["nodes"])
            pr[field]["totalCount"] = page["totalCount"]
            pr[field]["pageInfo"] = page["pageInfo"]
            pages += 1
            if page["pageInfo"]["hasNextPage"]:
                pending.append((pr, page["pageInfo"]["endCursor"]))
        print(
            f"  completed {field}: pages={pages} pending={len(pending)} "
            f"rate_remaining={remaining}",
            flush=True,
        )
        if remaining < 200:
            sys.stderr.write(f"  rate limit low ({remaining}), pausing 60s\n")
            time.sleep(60)
    return pages


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

    all_prs_list = list(all_prs.values())
    review_pages = paginate_nested(
        all_prs_list,
        "reviews",
        "author { login __typename } state body submittedAt",
        100,
    )
    file_pages = paginate_nested(all_prs_list, "files", "path", 100)

    incomplete_reviews = sum(
        len(pr["reviews"]["nodes"]) != pr["reviews"]["totalCount"]
        for pr in all_prs_list
    )
    incomplete_files = sum(
        len(pr["files"]["nodes"]) != pr["files"]["totalCount"]
        for pr in all_prs_list
    )
    if incomplete_reviews or incomplete_files:
        raise RuntimeError(
            f"nested pagination incomplete: reviews={incomplete_reviews}, files={incomplete_files}"
        )

    with open(OUT_PATH, "w") as f:
        json.dump({
            "meta": {
                "repo": REPO,
                "window_start": START.isoformat(),
                "window_end": END.isoformat(),
                "total_prs": len(all_prs),
                "per_day_mismatches": mismatches,
                "initial_page_sizes": {"files_per_pr": 100, "reviews_per_pr": 10},
                "nested_pagination": {
                    "review_pages_fetched": review_pages,
                    "file_pages_fetched": file_pages,
                    "incomplete_review_connections": incomplete_reviews,
                    "incomplete_file_connections": incomplete_files,
                },
            },
            "prs": all_prs_list,
        }, f)

    print(f"\nDone. {len(all_prs)} unique merged PRs written to {OUT_PATH}")
    if mismatches:
        print(f"WARNING: {len(mismatches)} day(s) had reported != fetched count:")
        for m in mismatches:
            print(f"  {m}")


if __name__ == "__main__":
    main()
