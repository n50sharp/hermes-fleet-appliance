---
name: github-company-research
description: "Research companies via GitHub API for live repo metadata."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Research, Companies, Repos, Maturity]
    category: research
    related_skills: [competitor-news-monitor, grounded-citations]
---

# GitHub Company Research

Research software/tech companies by querying the GitHub REST API directly for live repository metadata. Builds on `competitor-news-monitor` step 3 for the tech-company watchlist.

## When to Use

- User asks to search GitHub for repos related to a company, topic, or technology area
- Need live star counts, language, license, and description metadata (not web_search snippets)
- Cross-checking a repository's maturity or legitimacy when web_search results are ambiguous
- `web_search` backends (Firecrawl, Exa) returned 403/rate-limited and the research cannot proceed without repo metadata

## Procedure

### 1. Identify the query scope

Decide the search terms (repo names, topics, org names) and what metadata matters (stars threshold, language, license). Write them down before querying.

### 2. Query via execute_code

Use Python via `execute_code` with `urllib.request` against `https://api.github.com/search/repositories?q=<encoded-query>&sort=stars&order=desc&per_page=N`. Always set a `User-Agent` header (`scout-research` or similar). Example:

```python
import urllib.request, json, urllib.parse
def gh_search(q, sort="stars"):
    url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&sort={sort}&order=desc&per_page=8"
    req = urllib.request.Request(url, headers={"User-Agent":"scout-research"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)
```

For individual repo metadata, hit `https://api.github.com/repos/<org/name>` and read `stargazers_count`, `language`, `license.name`, `description`.

### 3. Rate-limit handling

GitHub unauthenticated API is rate-limited to 60 requests/hour. If you hit 403 or rate-limit errors, stop and report the cap. Add a short sleep between sequential repo lookups. Do not retry 403s as if they were transient.

### 4. Assess maturity signals

Record for each repo: stars, language, license, description, commit activity (from `pushed_at` or `created_at` fields). Apply these heuristics:
- High stars + recent commits + clear license → credible/maintained
- Low stars (<50) + elaborate README + no license field → likely portfolio/demo project, flag as such
- No license field → treat as "license unknown", not "open source"
- Description contains "AI" or buzzwords with low stars → candidate for AI-generated portfolio repo

## Pitfalls

- Treating web_search description as authoritative for star counts — always pull from GitHub API directly
- Ignoring rate limits and retrying 403s endlessly
- Counting every repo in search results as a "system" without checking stars/commit activity
- Mistaking a cyber-security C2 framework for a military/battlefield C2 just because they share the "C2" acronym (e.g., GunnerC2 is red-team tooling, not mission C2)

## Verification

- [ ] Star counts, language, license pulled from GitHub API, not web_search snippets
- [ ] Rate limits observed and reported
- [ ] Maturity heuristics applied to each repo before calling it "credible"
- [ ] Domain disambiguation applied (cyber-C2 vs military-C2, AI-agent-control vs mission-planning)
