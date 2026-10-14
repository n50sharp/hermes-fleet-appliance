---
name: defuddle
description: "Strip webpage clutter to clean markdown for reading."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Web, Scraping, Tokens, Cost-Saving, Markdown]
    related_skills: [blocked-page-recovery]
---

# Defuddle — Clean Web Content Extraction

Defuddle strips a webpage down to its main content (removing nav bars, ads, footers, cookie banners, sidebars) and returns clean Markdown or HTML. Use it instead of raw fetch/web_extract when you want to minimize tokens spent on webpage clutter, or when a page's boilerplate is drowning out the actual content.

Installed globally via npm: `npm install -g defuddle`. Verify with `defuddle --version` or `which defuddle`.

## When to use

- Reading articles/blog posts/docs for research — always prefer this over dumping raw HTML/full page text into context.
- A page returned by web_extract still has heavy nav/ad clutter.
- You need clean Markdown for further LLM processing (summarizing, quoting).

## Usage

```bash
# Parse a URL directly, output markdown
npx defuddle parse https://example.com/article --markdown

# Parse a local HTML file
npx defuddle parse page.html --markdown

# Parse HTML piped from stdin (e.g. after curl or browser fetch)
curl -sL https://example.com/article | npx defuddle parse --markdown

# JSON output with metadata (title, author, published date, word count)
npx defuddle parse https://example.com/article --json

# Extract just one property
npx defuddle parse https://example.com/article -p title
```

If installed globally (`npm install -g defuddle`), drop the `npx` prefix and call `defuddle` directly.

## Options

| Flag | Effect |
|------|--------|
| `-m, --markdown` | Convert content to Markdown (recommended default) |
| `-j, --json` | Output as JSON with metadata + content |
| `-p, --property <name>` | Extract a single property (title, description, domain, wordCount, etc.) |
| `-o, --output <file>` | Write output to a file instead of stdout |
| `-l, --lang <code>` | Preferred language (BCP 47, e.g. en, fr, ja) |
| `-u, --user-agent <string>` | Custom User-Agent (helps with 403/FORBIDDEN responses) |
| `--debug` | Enable debug logging, includes removal info |

## Workflow

1. Given a URL, run `defuddle parse <url> --markdown` via terminal.
2. If it 403s or returns empty, retry with `-u "Mozilla/5.0"` (custom user-agent) or fall back to the `blocked-page-recovery` skill.
3. Feed the cleaned markdown into your reasoning/summary — this is far cheaper in tokens than raw HTML or an unfiltered web_extract dump.
4. For batch research (many URLs), loop the CLI call per URL and append results to a workspace file rather than holding them all in context at once.

## Notes

- Written for reader-mode extraction (like Safari Reading Mode / Obsidian Web Clipper) — best on article/blog-style pages, less useful on highly interactive app-like pages (dashboards, SPAs with client-rendered content behind auth).
- No API key or network service required beyond fetching the target page itself — runs fully local via Node.
