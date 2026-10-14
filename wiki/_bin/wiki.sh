#!/usr/bin/env bash
# wiki.sh — the ONE way every agent reaches ${CREW_WIKI_DIR:-$HOME/wiki}.
# See SKILL.md for usage. Do not hand-edit wiki files directly — go through this.
set -euo pipefail

WIKI="${CREW_WIKI_DIR:-${CREW_WIKI_DIR:-$HOME/wiki}}"
LOCKDIR="$WIKI/_lock"
LOCKFILE="$LOCKDIR/wiki.lock"
METADIR="$WIKI/_meta"
QUARANTINE="$WIKI/_quarantine"
INDEX="$WIKI/index.md"
LOG="$WIKI/log.md"
RULES="$WIKI/WIKI.md"
PROCESSED="$WIKI/processed"
RAW="$WIKI/raw"
FOLDERS=(personal business research mission journal)

die() { echo "$1" >&2; exit 1; }

NAME_RE='^[a-z0-9][a-z0-9-]{0,63}$'
valid_name() { [[ "$1" =~ $NAME_RE ]]; }
no_newline() { [[ "$1" != *$'\n'* ]]; }
is_folder() { local f; for f in "${FOLDERS[@]}"; do [[ "$f" == "$1" ]] && return 0; done; return 1; }

require_dirs() {
  mkdir -p "$LOCKDIR" \
           "$METADIR/personal" "$METADIR/business" "$METADIR/research" "$METADIR/mission" "$METADIR/journal" \
           "$QUARANTINE/personal" "$QUARANTINE/business" "$QUARANTINE/research" "$QUARANTINE/mission" "$QUARANTINE/journal" \
           "$PROCESSED/personal" "$PROCESSED/business" "$PROCESSED/research" "$PROCESSED/mission" "$PROCESSED/journal" "$RAW"
}

with_lock() {
  # Runs the given function+args while holding the wiki's one lock file.
  # flock releases it on ANY exit of this shell (kernel-held, never hand-rolled) —
  # it cannot stay stuck even if the write step below dies mid-way.
  require_dirs
  exec 9>"$LOCKFILE"
  if ! flock -w 30 9; then
    die "wiki is busy, try again"
  fi
  "$@"
}

sha256_of() { sha256sum "$1" | awk '{print $1}'; }

title_case() {
  echo "$1" | sed -E 's/(^|-)([a-z])/\1\U\2/g; s/-/ /g'
}

find_name_folder() {
  local n="$1" f
  for f in "${FOLDERS[@]}"; do
    [[ -f "$PROCESSED/$f/$n.md" ]] && { echo "$f"; return 0; }
  done
  return 1
}

log_line() {
  local action="$1" path="$2" reason="$3"
  printf -- '- %s — %s: %s — %s\n' "$(date '+%Y-%m-%d %H:%M')" "$action" "$path" "$reason" >> "$LOG"
}

wiki_commit() {
  # Commit exactly what this verb touched. MUST be called while the wiki
  # lock is held (every caller is inside with_lock). A failed commit dies
  # loudly: the whole point of the commit is recoverability — a silent
  # failure would leave the write un-recorded, which is the one thing the
  # commit exists to prevent.
  local agent="$1" page="$2"
  git -C "$WIKI" add -A -- "$page" >/dev/null 2>&1 || die "git add failed for $page (write NOT recoverable — fix git now)"
  if git -C "$WIKI" diff --cached --quiet -- "$page" 2>/dev/null; then
    return 0  # nothing actually changed (e.g. reindex with identical output)
  fi
  git -C "$WIKI" commit -m "$agent: $page" >/dev/null 2>&1 \
    || die "git commit failed for $page (write NOT recoverable — fix git now)"
}

rebuild_index() {
  {
    echo "# index.md — wiki catalog"
    echo
    echo "One line per page. Agents read this file first, before searching anything else."
    echo
    local f
    for f in "${FOLDERS[@]}"; do
      echo "## $f"
      echo
      local any=0 meta n title summary aka relpath
      shopt -s nullglob
      for meta in "$METADIR/$f"/*.meta; do
        any=1
        n="$(basename "$meta" .meta)"
        title="$(grep -m1 '^title=' "$meta" | cut -d= -f2-)"
        summary="$(grep -m1 '^summary=' "$meta" | cut -d= -f2-)"
        aka="$(grep -m1 '^aka=' "$meta" | cut -d= -f2-)"
        relpath="processed/$f/$n.md"
        if [[ -n "$aka" ]]; then
          echo "- [$title]($relpath) — $summary (aka $aka)"
        else
          echo "- [$title]($relpath) — $summary"
        fi
      done
      shopt -u nullglob
      [[ "$any" -eq 0 ]] && echo "*(none yet)*"
      echo
    done
  } > "$INDEX.tmp"
  mv "$INDEX.tmp" "$INDEX"
}

# ── read verbs: never lock ──────────────────────────────────────────────────

cmd_index() {
  require_dirs
  [[ -f "$INDEX" ]] || die "index.md missing — run 'wiki reindex'"
  local full=0
  [[ "${1:-}" == "--full" ]] && full=1
  local section="" in_research=0 research_count=0 line
  while IFS= read -r line; do
    if [[ "$line" == "## "* ]]; then
      if [[ "$in_research" -eq 1 && "$full" -eq 0 ]]; then
        echo "$research_count page(s) — use --full to list"
      fi
      section="$line"; in_research=0; research_count=0
      [[ "$section" == "## research" ]] && in_research=1
      echo "$line"
      continue
    fi
    if [[ "$in_research" -eq 1 && "$full" -eq 0 ]]; then
      [[ "$line" == "- "* ]] && research_count=$((research_count+1))
      continue
    fi
    echo "$line"
  done < "$INDEX"
  if [[ "$in_research" -eq 1 && "$full" -eq 0 ]]; then
    echo "$research_count page(s) — use --full to list"
  fi
}

cmd_search() {
  require_dirs
  local term="${1:-}"
  [[ -n "$term" ]] || die "usage: wiki search <text>"
  local hits
  hits="$(grep -rFn --include='*.md' -- "$term" "$PROCESSED" "$RAW" 2>/dev/null | sed "s|$WIKI/||" || true)"
  if [[ -z "$hits" ]]; then echo "no matches"; else echo "$hits"; fi
}

cmd_read() {
  require_dirs
  local rel="${1:-}"
  [[ -n "$rel" ]] || die "usage: wiki read <relative-path>"
  case "$rel" in
    /*|*..*|_*|*/_*) die "invalid path" ;;
  esac
  local abs="$WIKI/$rel"
  [[ -f "$abs" ]] || die "not found: $rel"
  echo "--hash $(sha256_of "$abs")"
  cat "$abs"
}

cmd_rules() {
  [[ -f "$RULES" ]] || die "WIKI.md missing"
  cat "$RULES"
}

# ── write verbs: always lock ────────────────────────────────────────────────

cmd_file_impl() {
  local agent="" folder="" name="" hash="" summary="" aka="" raw_slug="" ext="md"
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent) agent="$2"; shift 2;;
      --folder) folder="$2"; shift 2;;
      --name) name="$2"; shift 2;;
      --hash) hash="$2"; shift 2;;
      --summary) summary="$2"; shift 2;;
      --aka) aka="$2"; shift 2;;
      --raw) raw_slug="$2"; shift 2;;
      --ext) ext="$2"; shift 2;;
      *) die "unknown flag: $1";;
    esac
  done
  [[ -n "$agent" ]] || die "usage: --agent required"
  valid_name "$agent" || die "invalid agent name"
  no_newline "$summary" || die "invalid characters in summary"
  no_newline "$aka" || die "invalid characters in aka"

  if [[ -n "$raw_slug" ]]; then
    valid_name "$raw_slug" || die "invalid raw slug"
    [[ "$ext" =~ ^[a-z0-9]{1,10}$ ]] || die "invalid extension"
    local dest="$RAW/$raw_slug.$ext"
    [[ -e "$dest" ]] && die "source already filed, choose a new slug"
    local tmp; tmp="$(mktemp "$RAW/.tmp.XXXXXX")"
    cat > "$tmp"
    mv "$tmp" "$dest"
    log_line "create" "raw/$raw_slug.$ext" "filed by $agent"
    wiki_commit "$agent" "raw/$raw_slug.$ext"
    echo "filed: raw/$raw_slug.$ext"
    return 0
  fi

  [[ -n "$folder" && -n "$name" ]] || die "usage: --folder and --name required (or --raw)"
  is_folder "$folder" || die "invalid folder: $folder"
  valid_name "$name" || die "invalid page name"
  [[ -n "$summary" ]] || die "usage: --summary required"

  local existing_folder
  existing_folder="$(find_name_folder "$name" || true)"
  if [[ -n "$existing_folder" && "$existing_folder" != "$folder" ]]; then
    die "name already used in processed/$existing_folder — names stay unique"
  fi

  local dest="$PROCESSED/$folder/$name.md"
  local existed=0
  [[ -f "$dest" ]] && existed=1

  if [[ "$existed" -eq 1 ]]; then
    [[ -n "$hash" ]] || die "page exists, read it first"
    local current; current="$(sha256_of "$dest")"
    [[ "$hash" == "$current" ]] || die "page changed since you read it, read it again"
  fi

  local tmp; tmp="$(mktemp "$PROCESSED/$folder/.tmp.XXXXXX")"
  cat > "$tmp"
  mv "$tmp" "$dest"

  {
    echo "title=$(title_case "$name")"
    echo "summary=$summary"
    echo "aka=$aka"
    echo "updated=$(date '+%Y-%m-%d')"
    echo "agent=$agent"
  } > "$METADIR/$folder/$name.meta"

  rebuild_index

  if [[ "$existed" -eq 1 ]]; then
    log_line "update" "processed/$folder/$name.md" "filed by $agent"
  else
    log_line "create" "processed/$folder/$name.md" "filed by $agent"
  fi
  wiki_commit "$agent" "processed/$folder/$name.md"
  wiki_commit "$agent" "index.md"
  wiki_commit "$agent" "log.md"
  echo "filed: processed/$folder/$name.md"
}
cmd_file() { with_lock cmd_file_impl "$@"; }

cmd_reindex_impl() {
  local agent="$1"
  rebuild_index
  log_line "update" "index.md" "reindex by $agent"
  wiki_commit "$agent" "index.md"
  wiki_commit "$agent" "log.md"
  echo "reindexed"
}
cmd_reindex() {
  local agent=""
  while [[ $# -gt 0 ]]; do
    case "$1" in --agent) agent="$2"; shift 2;; *) die "unknown flag: $1";; esac
  done
  [[ -n "$agent" ]] || die "usage: --agent required"
  valid_name "$agent" || die "invalid agent name"
  with_lock cmd_reindex_impl "$agent"
}

cmd_log_impl() {
  local agent="$1" page="$2" reason="$3"
  log_line "log" "$page" "$reason (by $agent)"
  wiki_commit "$agent" "log.md"
  echo "logged: $page"
}
cmd_log() {
  local agent="" page="" reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent) agent="$2"; shift 2;;
      --page) page="$2"; shift 2;;
      --reason) reason="$2"; shift 2;;
      *) die "unknown flag: $1";;
    esac
  done
  [[ -n "$agent" && -n "$page" && -n "$reason" ]] || die "usage: --agent --page --reason required"
  valid_name "$agent" || die "invalid agent name"
  no_newline "$reason" || die "invalid characters in reason"
  case "$page" in
    /*|*..*|_*|*/_*) die "invalid page path";;
  esac
  [[ -f "$WIKI/$page" ]] || die "page was never filed: $page"
  with_lock cmd_log_impl "$agent" "$page" "$reason"
}

cmd_delete_impl() {
  local agent="$1" path="$2" reason="$3"
  local abs="$WIKI/$path"
  local qdest="$QUARANTINE/${path#processed/}"
  mkdir -p "$(dirname "$qdest")"
  mv "$abs" "$qdest"
  case "$path" in
    processed/*/*.md)
      local folder name
      folder="$(echo "$path" | cut -d/ -f2)"
      name="$(basename "$path" .md)"
      rm -f "$METADIR/$folder/$name.meta"
      rebuild_index
      ;;
  esac
  log_line "delete" "$path" "$reason (by $agent) -> quarantined at ${qdest#$WIKI/}"
  wiki_commit "$agent" "$path"
  case "$path" in
    processed/*/*.md) wiki_commit "$agent" "index.md";;
  esac
  wiki_commit "$agent" "log.md"
  echo "quarantined: $path -> ${qdest#$WIKI/}"
}
cmd_delete() {
  local agent="" path="" reason=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --agent) agent="$2"; shift 2;;
      --path) path="$2"; shift 2;;
      --reason) reason="$2"; shift 2;;
      *) die "unknown flag: $1";;
    esac
  done
  [[ -n "$agent" && -n "$path" && -n "$reason" ]] || die "usage: --agent --path --reason required"
  valid_name "$agent" || die "invalid agent name"
  no_newline "$reason" || die "invalid characters in reason"
  case "$path" in
    raw/*) die "raw is never removed at all";;
    /*|*..*|_*|*/_*) die "invalid path";;
  esac
  [[ -f "$WIKI/$path" ]] || die "not found: $path"
  with_lock cmd_delete_impl "$agent" "$path" "$reason"
}

# ── dispatch ─────────────────────────────────────────────────────────────────

main() {
  local verb="${1:-}"
  shift || true
  case "$verb" in
    index)   cmd_index "$@";;
    search)  cmd_search "$@";;
    read)    cmd_read "$@";;
    rules)   cmd_rules "$@";;
    file)    cmd_file "$@";;
    reindex) cmd_reindex "$@";;
    log)     cmd_log "$@";;
    delete)  cmd_delete "$@";;
    *) die "usage: wiki {index|search|read|rules|file|reindex|log|delete} ...";;
  esac
}
main "$@"
