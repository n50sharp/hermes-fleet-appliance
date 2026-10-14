---
name: youtube-transcript
description: "Use whenever an ingest URL is a YouTube link — fetch a clean transcript BEFORE running the normal INGEST OPERATION on it."
version: 1.0.0
author: builder (JV)
license: internal
---

# youtube-transcript — clean transcript from a YouTube link

## When this applies

An ingest URL that is a YouTube link (`youtube.com/watch`, `youtu.be/...`, etc.) means: get the transcript with this skill FIRST, then run the normal INGEST OPERATION on the result. Nothing else about ingestion changes — the transcript is the verbatim source, so it lands in `raw/`; the research page with its provenance banner and concepts lands in `research/`, exactly like any other ingest.

## How to get the transcript

```bash
/home/hermes/wiki/_bin/yt_transcript.sh "<youtube-url>"
```

Prints, on success (exit 0):
```
Title: <video title>
Uploader: <channel name>
Upload date: <YYYYMMDD>
Video ID: <id>
Caption type: official|auto-generated (English)

<clean prose transcript, no timestamps, no repeats>
```

Prefers official (human-written) captions; falls back to auto-generated only when no official English track exists. The `Caption type:` line tells you which one you got — carry that into the research page's provenance (it's part of "how we know this").

## If it fails — exit codes matter

- **Exit 3: no captions available at all** (neither official nor auto-generated, English). This is a hard stop. **Never summarise a video you could not read** — say on the research page (or to the owner, if this comes up outside an ingest) that no transcript could be obtained, and do not proceed to file a page claiming to know the video's content.
- **Exit 2:** metadata fetch, caption download, or cleaning failed for some other reason (bad URL, network issue, empty caption file). Report the error, do not fabricate a transcript.
- **Exit 1:** usage error (no URL given).

## Then: run INGEST as normal

Once you have the transcript text:
1. `wiki search` the URL first (per the normal INGEST OPERATION) — if already filed, update instead of duplicating.
2. File the RAW verbatim transcript to `raw/YYYY-MM-DD-<short-slug>.md` — the transcript text IS the verbatim capture for a video (per the rulebook: "for a video, verbatim means the transcript"). One-line header: title, `Source: <url>`, `ingested YYYY-MM-DD`.
3. File the research page: provenance block (`Source: <url> · By: <uploader> · Published: <upload date> · Read: <today> · Type: video`), attributed claims ("the creator says/demonstrates..."), 3-6 concepts, links to personal/business pages it touches.
4. Both go through `wiki file` — never written by hand.

## Setup this skill relies on (already installed, documented for reference)

- `yt-dlp` (Python package) — installed system-wide via `pip3 install yt-dlp`, resolves on the shared PATH.
- A JavaScript runtime — `node` is what's on this machine (yt-dlp only auto-enables `deno` by default). `yt_transcript.sh` wires in `--js-runtimes node` automatically; you don't need to pass this yourself.
- `/home/hermes/wiki/_bin/clean_vtt.py` — strips inline timing tags and de-overlaps YouTube's rolling-window auto-caption repeats so the output reads as continuous prose. Called automatically by `yt_transcript.sh`; you don't need to invoke it directly.
