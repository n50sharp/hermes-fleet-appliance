---
name: local-browser-automation
description: "Drive the user's real logged-in browser for account tasks."
version: 0.1.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Browser, Automation, Real-Profile, Accounts, Delegation]
    related_skills: [computer-use, blocked-page-recovery]
---

# Local Browser Automation (real logged-in profile)

Use `mcp__browser_exec(..., local=True)` when a task needs the user's OWN logged-in web session (Google Photos, banking, any site without a usable API/OAuth scope) rather than a fresh headless/cloud browser. This is distinct from `computer_use` (native desktop apps) and from `browser_exec` without `local=True` (disposable cloud browser, no user cookies).

## When to use

- Task needs an account already logged in on the user's machine, and no API/MCP path can reach the same data (e.g. Google Photos Library API only sees app-uploaded media, not the real camera-roll library — see the `composio` skill).
- User asks to "organize/clean up/manage" something in a web app that has no bulk API.

Don't use for: anything reachable via a proper API/MCP/CLI integration — always check those first, `local=True` browser automation is the fallback, not the default.

## Prerequisite check (do this FIRST, before any navigation)

`local=True` snapshots the OS's actual default browser profile. Two failure modes, both environment setup issues, not tool bugs:

1. **Default browser isn't Chromium-family.** `ensure_real_tab()` / `new_tab()` errors `"browser.use_real_profile is on, but your default browser is not a supported Chromium browser"`. Verify with `xdg-settings get default-web-browser` (Linux) before assuming Chrome is active — Firefox-as-default is common and unsupported. Ask the user to install Chrome/Edge/Brave/Chromium and set it default; you cannot silently substitute a cloud browser and call it the same task, since it won't have the user's login.
2. **Browser installed but never launched.** Even after install + default is set, the profile directory (`~/.config/google-chrome/Default` on Linux) doesn't exist until the user opens the browser once. Error looks like `"could not snapshot the 'chrome' profile ... No such file or directory"`. Ask the user to open the browser once (and sign into Google/whatever account if prompted), then retry `ensure_real_tab()`.

Both are one-time environment setup, not something to route around — confirm resolution with a clean `ensure_real_tab()` call before proceeding.

## Procedure

1. Confirm prerequisites above.
2. `new_tab(url)` for the first navigation in a session, `goto_url(url)` after. `wait_for_load()` after navigation.
3. `capture_screenshot()` liberally to verify state visually — screenshots auto-attach for inspection.
4. **SPA sites often scroll an inner container, not the page.** `window.scrollTo` / `document.body.scrollHeight` is a no-op on apps like Google Photos. Find the real scrollable element via JS and scroll that:
   ```js
   Array.from(document.querySelectorAll('*'))
     .filter(e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 100)
   ```
   then `el.scrollTop += N` on the matched element, not `window`.
5. For tasks spanning many repetitive UI actions (searching, scrolling, multi-select, repeat per category) — **delegate to a subagent** (`mcp__delegate_task`) rather than driving it turn-by-turn in the main conversation. Give the subagent: the exact `local=True` tool-call pattern, any selectors/scroll technique already discovered this session, explicit non-destructive constraints (e.g. "do not delete anything"), and a request to report back concrete counts/results plus anything ambiguous it skipped rather than guessing. This keeps the main session free to keep talking with the user while the long mechanical pass runs in the background.

## Pitfalls

- Don't assume a `local=True` error is a login problem — check default-browser and profile-existence first; both produce clear, distinct error strings.
- Never treat a `local=True` failure as license to fall back to a non-local (cloud) browser for an account-specific task — that browser has no cookies/session and will hit a login wall instead of doing the task.
- Confirm with the user up front what's destructive (delete/move) vs additive (create album, add to album) and keep any automated pass to the additive operations unless the user explicitly approves deletion.

## Verification

- [ ] `ensure_real_tab()` / `new_tab()` succeeds with no profile/browser error before starting task actions.
- [ ] Scrollable containers verified via JS `scrollHeight`/`clientHeight` inspection, not assumed to be `window`.
- [ ] Long repetitive passes delegated to a subagent with concrete, reusable context, not driven step-by-step in the main thread.
