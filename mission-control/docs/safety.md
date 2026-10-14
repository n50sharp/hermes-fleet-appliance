# Safety & Guardrails — agent-mission-control

Loops amplify judgment — good and bad. These are the minimum bar for any
loop-engineering pattern (daily-triage, or anything added later) running
unattended against this repo.

## Path Denylist

The loop must **never** auto-edit these without human approval:

```
**/.env
**/.env.*
**/secrets/**
**/credentials/**
**/*_key*
**/*_secret*
**/.terraform/**
**/k8s/production/**
**/migrations/**
**/auth/**
**/payments/**
**/billing/**
apps/crypto-income-strategy/secrets/**
**/*.db                      # agent-logs.db, example-metrics caches, etc.
**/*.jsonl                   # pull logs (e.g. etoro_pull_log.jsonl)
backups/**
apps/*/app/onchain/**        # live financial data sources (eToro, mining,
                              # on-chain RPC readers) — real money, real
                              # browser sessions; changes here always need
                              # a human read, see the 2026-09-25 eToro/
                              # mining incidents in the wiki.
```

[`loop-gate`](https://github.com/cobusgreyling/loop-engineering/tree/main/tools/loop-gate)
enforces this denylist (and the auto-merge allowlist below) mechanically
from [`gate.yaml`](../gate.yaml) instead of relying on the loop to have read
this file: `loop-gate check --action <type> --paths <changed files>` exits
`2` to escalate, `0` to proceed.

`gate.yaml` also sets `maxFiles: 10` — a change touching more than 10 files
escalates regardless of which paths it touches, on the assumption that a
loop proposing a large diff has lost the plot.

## Auto-Merge Policy

**Default: no auto-merge.** This repo is week-one L1 (report-only) as of
2026-09-25 — no loop may commit or merge anything on its own yet.

If/when auto-merge is enabled for a specific trivial loop:

| Allowed | Not allowed |
|---------|-------------|
| Typo in comment/docs | Behavior changes |
| Lint auto-fix in test files only | Dependency version bumps |
| Import ordering | Lockfile changes |
| Config in allowlisted `docs/` paths | Any denylist path (see above) |

## MCP Connector Least Privilege

| Connector | Read | Write |
|-----------|------|-------|
| GitHub | issues, PRs, checks | comment, label (not merge by default) |
| Database | — | no production write from loops (agent-logs.db, metrics caches are denylisted above) |

Use separate bot accounts / tokens with minimal scopes if/when a loop needs
its own GitHub token — do not reuse the owner's personal `gh` auth for unattended
runs.

## Human Gates (Required)

Always require human for:

- Security, authentication, authorization
- Payments, billing, PII, or anything touching real system/metrics balances
  (`apps/crypto-income-strategy/**`)
- Infrastructure / systemd units / production config
- Dependency upgrades (supply chain risk)
- Changes touching >10 files
- Third attempt failed on the same item
- Token budget extension requests — a human must explicitly edit
  `loop-budget.md`; loops cannot self-raise their own cap.

## Secrets in Prompts & Logs

- Never paste API keys, RPC keys, or eToro/CircleHash session data into
  scheduler prompts.
- CI logs may contain secrets — the triage skill must redact before writing
  to `STATE.md`.
- `STATE.md` is committed to git — no credentials in it, ever.

## Flake & Test Safety

- Do not disable tests to make CI green.
- Do not increase timeouts blindly without a root-cause note (see the
  2026-09-25 eToro render-race fix in the wiki for the right pattern: found
  *why* it was flaky, then fixed the actual cause).
- Quarantine flakes via explicit ticket + human approval.

## Incident Response

If a loop merges bad code or corrupts live data:

1. Pause all loops immediately (delete/disable the scheduler entry).
2. Revert the merge.
3. Record what happened in `STATE.md` and the wiki
   (`processed/personal/example-metrics-app.md` or
   `processed/personal/ai-agent-fleet.md` as appropriate).
4. Tighten the verifier or shrink loop scope before restarting.

## Pre-Flight Safety Check (before ever enabling L3 / unattended)

- [ ] Denylist encoded in skills, not just this doc
- [ ] Auto-merge off, or strict allowlist only
- [ ] Connector scopes reviewed
- [ ] Human gates documented in the pattern
- [ ] Kill switch documented and tested

## Status

- **Current level: L1 (report-only)** for the `daily-triage` pattern,
  installed 2026-09-25. No auto-fix, no auto-merge.
- See [`processed/personal/loop-engineering.md`](file:///home/hermes/wiki/processed/personal/loop-engineering.md)
  in the wiki for the full cyber-audit and install record.
