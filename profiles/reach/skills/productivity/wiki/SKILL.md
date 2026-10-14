---
name: wiki
description: "Use before answering anything the crew might already know, and after any work worth remembering. The one command that reaches the crew wiki at /home/hermes/wiki."
version: 1.0.0
author: builder (JV)
license: internal
---

# wiki — the crew's persistent memory

`/home/hermes/wiki` is plain markdown the crew reads before answering and writes to after working. Chat history gets summarised away; this does not. The `wiki` command is the ONLY way in — never read or write files under `/home/hermes/wiki` directly with your own file tools.

Read `/home/hermes/wiki/WIKI.md` (or run `wiki rules`) for the actual rulebook: where a page goes, naming, linking, and the RECALL/FILE workflows. This skill just documents the command surface.

## The eight verbs

**Read verbs — never lock, always safe to call:**

- `wiki index [--full]` — the catalog. Research folder shows a count unless you pass `--full`.
- `wiki search <text>` — literal text match across the wiki. Search TERMS, not whole sentences; this is grep, not semantic search.
- `wiki read <relative-path>` — prints a `--hash <sha256>` line, then the page body. **Keep that hash** — you need it to file an update to this exact page.
- `wiki rules` — prints WIKI.md verbatim.

**Write verbs — always lock (30s wait, then refuse), always require `--agent <your-name>`:**

- `wiki file` — replace or create a page, or file a raw source. See below — the flags differ for a wiki page vs. a raw source.
- `wiki reindex --agent <name>` — rebuild index.md from the pages on disk. Rarely needed by hand; `file` and `delete` already reindex themselves.
- `wiki log --agent <name> --page <relative-path> --reason "<text>"` — record that work happened touching an existing page, without changing the page itself. Refuses if the page was never filed.
- `wiki delete --agent <name> --path <relative-path> --reason "<text>"` — quarantines a page (moves it under `_quarantine/`, drops its index line). Nothing is ever destroyed. `raw/` can never be deleted this way — verbatim sources are permanent.

## Filing a wiki page (processed/personal, processed/business, processed/research)

```bash
wiki file --agent scout --folder research --name some-competitor \
  --summary "one-line factual summary" --aka "alias1, alias2" <<'EOF'
# Some Competitor

One-line summary.

Body of the page, short and factual.
EOF
```

- `--folder` is one of `personal`, `business`, `research`.
- `--name` is the page's lowercase-with-dashes filename (no `.md`).
- `--summary` is required, one line, no newlines.
- `--aka` is optional, comma-separated aliases, no newlines.
- **New page:** just the above — no hash needed.
- **Existing page:** you MUST pass `--hash <sha256>` from a `wiki read` you just did. Refusals you will hit on purpose or by accident:
  - Page exists, you passed no hash → `page exists, read it first`
  - Your hash is stale (someone else changed it since you read it) → `page changed since you read it, read it again`
  - The name is already used in a *different* folder → `name already used in processed/<folder> — names stay unique`

### STRENGTHEN, NEVER DUPLICATE

To add to a page: `wiki read` it, keep everything already there, add your new material, send the WHOLE page back (old + new) with `--hash` from that same read. If refused because it changed, **read again and re-apply your addition on top of the fresh copy** — never drop the hash just to force the write through. That refusal exists to stop you from clobbering someone else's edit.

## Filing a raw source (raw/ — verbatim, never edited, never replaced)

```bash
wiki file --agent scout --raw some-source-slug --ext md <<'EOF'
(verbatim source content — the ORIGINAL, not your summary of it)
EOF
```

- `--raw <slug>` + `--ext <extension>` (default `md`).
- If the slug already exists: `source already filed, choose a new slug` — raw is never replaced, ever. Pick a new slug (e.g. append a date) for a re-fetch of the same source.

## Piping a page body safely

Always use a quoted heredoc (`<<'EOF' ... EOF`) so the shell does not expand `$variables`, backticks, or `!` history-expansion inside your page content. Never build the body with string concatenation in a way that lets untrusted text inject shell syntax — the heredoc is piped straight to `wiki`'s stdin and treated as literal bytes.

```bash
wiki file --agent dev --folder business --name some-vendor --summary "..." <<'EOF'
... page content, completely literal, no shell expansion happens in here ...
EOF
```

## Logging work that touched no page

```bash
wiki log --agent reach --page processed/business/some-vendor.md --reason "confirmed pricing still current, no change needed"
```

Refuses with `page was never filed: <path>` if you name a page that doesn't exist yet — you cannot log a filing you never did.

## Quarantine / delete

```bash
wiki delete --agent dev --path processed/research/stale-page.md --reason "superseded by newer-page.md"
```

Moves the page to `_quarantine/`, drops it from `index.md` and from `search`. The file itself still exists on disk under quarantine — nothing is destroyed. `raw/*` paths always refuse: `raw is never removed at all`.

## Errors you may see and what they mean

| Message | Meaning |
|---|---|
| `wiki is busy, try again` | Another agent is writing right now. Wait a moment and retry — the lock always releases within 30s or on the other process exiting. |
| `page exists, read it first` | You tried to file over an existing page without a hash. Run `wiki read` first. |
| `page changed since you read it, read it again` | Someone else edited the page between your read and your write. Re-read, re-apply your change on the fresh copy. |
| `name already used in processed/<folder> — names stay unique` | Pick a different page name — names are unique across all three folders. |
| `source already filed, choose a new slug` | That raw slug exists already. raw/ is never overwritten — use a new slug. |
| `page was never filed: <path>` | `wiki log` was pointed at a page that doesn't exist. |
| `raw is never removed at all` | `wiki delete` refuses any path under raw/ — this is permanent by design. |
| `invalid page name` / `invalid agent name` | Only lowercase letters, digits, and dashes — nothing else, to keep log lines and filenames safe. |

## Workflow reminders (full rules live in WIKI.md / `wiki rules`)

- **RECALL:** before answering anything the wiki might know, `wiki search` first. Answer from what it knows. Say "Not on file" when it doesn't — never guess in its place.
- **FILE:** after real work, file the lasting part through `wiki file` — never by hand, never skipped.
