#!/usr/bin/env python3
"""
Agent Mission Control — dashboard backend.

- Binds to 127.0.0.1 ONLY. Port defaults to 51763, overridable via PORT env var.
- Every Hermes SQLite connection is opened strictly read-only
  (file:...?mode=ro + PRAGMA query_only=1). We NEVER write to Hermes's own
  databases (state.db, kanban.db) or gateway_state.json.
- The only read-write store this server owns is ./board.db, a project-local
  SQLite file for the owner's personal task board (separate from Hermes's
  internal kanban.db).
- Python stdlib only. No pip, no npm.
- ONE exception to "no subprocess": POST /api/chat/send spawns the real
  `hermes` CLI (argv list, never shell=True) to run a live agent turn. This
  is the only place this server writes to Hermes state, and it does so only
  through Hermes's own CLI — never by touching state.db directly.
"""

import cgi
import os
import io
import json
import re
import sqlite3
import statistics
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import jv_diagnostics
import jv_watcher
import dev_self_heal
import secondary_agents

# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env before importing or calling voice/services
_env_path = os.path.join(PROJECT_DIR, ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f.read().splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
    except Exception:
        pass

if not os.environ.get("ELEVENLABS_API_KEY"):
    _jarvis_env = os.path.expanduser("~/jarvis-hermes-dashboard/.env")
    if os.path.exists(_jarvis_env):
        try:
            with open(_jarvis_env, encoding="utf-8") as _f:
                for _line in _f.read().splitlines():
                    _line = _line.strip()
                    if _line and not _line.startswith("#") and "=" in _line:
                        _k, _, _v = _line.partition("=")
                        if "ELEVENLABS" in _k:
                            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
        except Exception:
            pass

import voice as jv_voice
# NOTE: the ambient HERMES_HOME env var (when set by a per-profile shell,
# e.g. .../.hermes/profiles/dev) points at a PROFILE subdirectory, not the
# shared Hermes root where state.db / kanban.db / gateway_state.json live.
# Those shared files always live at ~/.hermes regardless of active profile,
# so we deliberately ignore HERMES_HOME here and use the fixed root path.
HERMES_HOME = os.path.expanduser("~/.hermes")

STATE_DB_PATH = os.path.join(HERMES_HOME, "state.db")
KANBAN_DB_PATH = os.path.join(HERMES_HOME, "kanban.db")
GATEWAY_STATE_PATH = os.path.join(HERMES_HOME, "gateway_state.json")
MODEL_ROUTING_PATH = os.path.join(HERMES_HOME, "agents", "_shared", "model-routing.json")

AGENT_LOGS_DB_PATH = os.path.join(PROJECT_DIR, "agent-logs.db")
BOARD_DB_PATH = os.path.join(PROJECT_DIR, "board.db")
INDEX_HTML_PATH = os.path.join(PROJECT_DIR, "index.html")
TEMPLATE_HTML_PATH = os.path.join(PROJECT_DIR, "hermes-dashboard-template.html")

# /api/avatar allow-list: the ONLY paths that route can ever serve, keyed by
# the exact agent name the frontend sends. Orchestrator has no profile
# subdir (it IS Hermes's default agent, same asymmetry as agent_state_db_path
# above) so its avatar lives at the shared root instead of profiles/<name>/.
AGENT_AVATAR_PATHS = {
    "orchestrator": os.path.join(HERMES_HOME, "assets", "avatar.png"),
    "scout": os.path.join(HERMES_HOME, "profiles", "scout", "assets", "avatar.png"),
    "scribe": os.path.join(HERMES_HOME, "profiles", "scribe", "assets", "avatar.png"),
    "reach": os.path.join(HERMES_HOME, "profiles", "reach", "assets", "avatar.png"),
    "dev": os.path.join(HERMES_HOME, "profiles", "dev", "assets", "avatar.png"),
    "dev2": os.path.join(HERMES_HOME, "profiles", "dev2", "assets", "avatar.png"),
}

# Chat: which state.db backs each agent's session history.
# The Orchestrator is Hermes's own default agent — it was never given a
# separate profile, so it reads/writes HERMES_HOME/state.db directly. Each
# specialist has its own profile dir with its own state.db.
CHAT_ALLOWED_AGENTS = ("orchestrator", "scout", "scribe", "reach", "dev", "dev2")


def agent_state_db_path(agent):
    if agent == "orchestrator":
        return STATE_DB_PATH
    return os.path.join(HERMES_HOME, "profiles", agent, "state.db")


import shutil as _shutil
HERMES_CLI_PATH = _shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")  # absolute under systemd's minimal PATH
CHAT_SUBPROCESS_TIMEOUT = 180
TELEGRAM_HOME_CHANNEL = os.environ.get("TELEGRAM_HOME_CHANNEL", "")

# Content library: real Markdown files on disk, one folder per agent.
# CONTENT_DIR defaults to a folder inside THIS project — never Hermes's own
# home directory — and is overridable via env var for flexibility.
CONTENT_DIR = os.environ.get("CONTENT_DIR", os.path.join(PROJECT_DIR, "content"))
CONTENT_ALLOWED_AGENTS = ("orchestrator", "scout", "scribe", "reach", "dev")

# Hermes cron — parsed read-only from jobs.json; write actions go through the
# real `hermes cron` CLI (argv list, never shell) so the job store's own
# validation/locking is always the source of truth, never our own writes.
CRON_JOBS_PATH = os.path.join(HERMES_HOME, "cron", "jobs.json")
CRON_PROFILE_NAMES = ["scout", "scribe", "reach", "dev", "dev2"]  # orchestrator = root, no subdir
CRON_ACTION_ALLOWED = ("run", "pause", "resume", "delete")
CRON_ACTION_SUBPROCESS_TIMEOUT = 30

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 51763))

CACHE_TTL_SECONDS = 3.0

# ── Missions (PRD workflow) ──────────────────────────────────────────────
# Owner's personal-to-Orchestrator mission pipeline: a Tasks-board card can
# be "assigned" to the Orchestrator, which (after explicit approval) gets
# its own dedicated Telegram forum topic + Hermes session so multiple
# concurrent missions never share context. Progress is journaled to an
# Obsidian note so a mission can span many days without re-explaining itself.
MISSIONS_DB_PATH = os.path.join(PROJECT_DIR, "missions.db")
TELEGRAM_TOPICS_JSON_PATH = os.path.join(
    HERMES_HOME, "plugins", "telegram_topic_profiles", "topics.json"
)

# ── Alerts (bell dropdown) ───────────────────────────────────────────────
# Append-only log of errors/warnings/info (schedule job start/completion,
# internal safe()-guarded failures, etc.), capped so the file never grows
# unbounded. Project-local, never touches Hermes's own state — this is the
# dashboard's own operational log, same tier as board.db/missions.db.
ALERTS_JSON_PATH = os.path.join(PROJECT_DIR, "alerts.json")
ALERTS_MAX_STORED = 200   # cap the on-disk log; well beyond the 10 shown in the UI
ALERTS_LEVELS = ("info", "warning", "error")
CRON_WATCH_POLL_SECONDS = 5.0  # how often the background watcher diffs cron state


def _load_hermes_env():
    """Minimal .env reader (stdlib only, no python-dotenv dependency) so this
    server can read TELEGRAM_BOT_TOKEN / TELEGRAM_GROUP_ALLOWED_CHATS even
    when launched as a systemd service with a bare environment. Read-only —
    never writes to ~/.hermes/.env. Commented/blank lines and quoted values
    are handled; anything malformed is silently skipped (best-effort only)."""
    values = {}
    env_path = os.path.join(HERMES_HOME, ".env")
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    values[key] = val
    except Exception:
        pass
    return values


_HERMES_ENV = _load_hermes_env()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or _HERMES_ENV.get("TELEGRAM_BOT_TOKEN", "")

def _load_project_env():
    """Read this project's own .env file (for RYZEN_API_BASE, RYZEN_*_API_KEY, etc.)"""
    values = {}
    env_path = os.path.join(PROJECT_DIR, ".env")
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key:
                    values[key] = val
    except Exception:
        pass
    return values

_PROJECT_ENV = _load_project_env()
RYZEN_API_BASE = os.environ.get("RYZEN_API_BASE") or _PROJECT_ENV.get("RYZEN_API_BASE", "http://100.65.84.84:8642")
RYZEN_DISPATCHES_PATH = os.path.join(PROJECT_DIR, "ryzen_dispatches.json")
_ryzen_dispatches_lock = threading.Lock()
_ryzen_fleet_cache = {"data": None, "ts": 0}

def _get_ryzen_key(profile):
    name = profile.split("-")[1].upper() if "-" in profile else profile.upper()
    env_var = f"RYZEN_{name}_API_KEY"
    return os.environ.get(env_var) or _PROJECT_ENV.get(env_var) or ""


def _load_telegram_group_chat_id():
    """The group chat id lives in config.yaml's
    platforms.telegram.group_allowed_chats list (NOT .env) — read it with
    a small targeted regex, same stdlib-only approach as
    read_agent_configured_model(), rather than adding a PyYAML dependency
    for one field. Falls back to the env var if config.yaml is unreadable
    or the key is missing (e.g. multiple allowed chats — we take the
    first)."""
    env_val = (os.environ.get("TELEGRAM_GROUP_ALLOWED_CHATS") or "").split(",")[0].strip()
    if env_val:
        return env_val
    try:
        with open(os.path.join(HERMES_HOME, "config.yaml"), "r") as f:
            text = f.read()
        m = re.search(r"group_allowed_chats:\s*\n\s*-\s*['\"]?(-?\d+)", text)
        if m:
            return m.group(1)
    except Exception:
        pass
    return ""


TELEGRAM_GROUP_CHAT_ID = _load_telegram_group_chat_id()
OBSIDIAN_VAULT_PATH = (
    os.environ.get("OBSIDIAN_VAULT_PATH")
    or _HERMES_ENV.get("OBSIDIAN_VAULT_PATH")
    or os.path.expanduser("~/Documents/Obsidian Vault")
)
MISSION_SUBPROCESS_TIMEOUT = 25  # spawning the kickoff turn is fire-and-forget in the background
SSE_INTERVAL_SECONDS = 3.0

# Static per-agent metadata table (hardcoded, per spec).
AGENT_META = {
    "orchestrator": {"code": "A-00", "initials": "OR", "role": "Coordinator", "secondaryRoles": ["Scrum Master"], "channel": "telegram", "name": "Orchestrator", "short": "ORCH"},
    "scout":        {"code": "A-01", "initials": "SC", "role": "Research",    "secondaryRoles": ["Discovery", "Copywriting"], "channel": "#scout",    "name": "Spock",        "short": "SC"},
    "scribe":       {"code": "A-02", "initials": "SB", "role": "Writing",     "secondaryRoles": ["QA/Testing", "Cyber Expert"], "channel": "#scribe",   "name": "Data",       "short": "SB"},
    "reach":        {"code": "A-03", "initials": "RE", "role": "Marketing",   "secondaryRoles": ["Business Analysis"], "channel": "#reach",    "name": "Seven",        "short": "RE"},
    "dev":          {"code": "A-04", "initials": "DV", "role": "Engineering", "secondaryRoles": ["Software Architect"], "channel": "#dev",      "name": "Scotty",          "short": "DV"},
    "dev2":         {"code": "A-05", "initials": "D2", "role": "Engineering (sub-agent)", "secondaryRoles": ["UI/UX Designer Expert"], "channel": "#dev2", "name": "La Forge", "short": "D2"},
}
AGENT_ORDER = ["orchestrator", "scout", "scribe", "reach", "dev", "dev2"]

# Each agent's config.yaml is the live source of truth for which model it's
# actually configured to run — the orchestrator has no separate profile dir
# and reads the shared root config.yaml directly; each specialist has its
# own profiles/<name>/config.yaml.
AGENT_CONFIG_PATH = {
    "orchestrator": os.path.join(HERMES_HOME, "config.yaml"),
    "scout": os.path.join(HERMES_HOME, "profiles", "scout", "config.yaml"),
    "scribe": os.path.join(HERMES_HOME, "profiles", "scribe", "config.yaml"),
    "reach": os.path.join(HERMES_HOME, "profiles", "reach", "config.yaml"),
    "dev": os.path.join(HERMES_HOME, "profiles", "dev", "config.yaml"),
    "dev2": os.path.join(HERMES_HOME, "profiles", "dev2", "config.yaml"),
}

def get_agent_config_path(agent):
    if agent in AGENT_CONFIG_PATH:
        return AGENT_CONFIG_PATH[agent]
    return os.path.join(HERMES_HOME, "profiles", agent, "config.yaml")
# Same base_url as the "ryzen-ollama" custom_providers entry in every
# profile's config.yaml — needed when live-switching an agent TO a Ryzen
# model, since `model.base_url` must be set alongside provider/default.
RYZEN_OLLAMA_BASE_URL = "http://100.65.84.84:11434/v1"
_MODEL_BLOCK_RE = re.compile(
    r"^model:\s*\n((?:[ \t]+\S.*\n?)+)", re.MULTILINE
)
_MODEL_FIELD_RE = re.compile(r"^\s*(default|provider):\s*(.*?)\s*$", re.MULTILINE)


def read_agent_configured_model(agent):
    """Read the model/provider this agent is CURRENTLY configured to run,
    straight from its config.yaml — read-only, stdlib-only (no PyYAML
    dependency), just enough parsing for the flat `model:` block Hermes
    writes. Falls back to '' on any read/parse failure so one bad config
    never breaks the whole snapshot.

    This is deliberately separate from agent_logs.model_used, which only
    reflects whatever model happened to be active the last time a task was
    logged — that goes stale the moment an agent's config.yaml is repointed
    to a different provider/model without a new log row being written.
    """
    path = get_agent_config_path(agent)
    if not path or not os.path.exists(path):
        return ""
    try:
        with open(path, "r") as f:
            text = f.read()
        m = _MODEL_BLOCK_RE.search(text)
        if not m:
            # Bare-string form `model: some-id` (Hermes dashboard may write it).
            bare = re.search(r"^model:[ \t]*([^\s#{][^\n#]*?)[ \t]*$", text, re.MULTILINE)
            return bare.group(1).strip("'\"") if bare else ""
        block = m.group(1)
        fields = dict(_MODEL_FIELD_RE.findall(block))
        model = fields.get("default", "").strip("'\"")
        provider = fields.get("provider", "").strip("'\"")
        if not model:
            return ""
        return f"{provider}:{model}" if provider else model
    except Exception:
        return ""

# Models selectable live from the Agents-tab model switcher. Each agent can
# switch to any Ryzen/Ollama model actually installed on the box (verified
# to meet Hermes's 64K-context minimum) plus whichever cloud provider it
# already uses. This is a fixed, curated list on purpose — NOT everything
# Hermes' own model-picker cache might know about — so the dropdown never
# offers a model that would silently fail (wrong context length, wrong
# provider auth, etc).
#
# List sourced from owner's 2026-09-13 request. Deliberately excludes two
# models that were in the raw request but don't belong in a chat-model
# switcher: nomic-embed-text (embedding-only, no chat/tool-call capability)
# and gemma4-e2b-8k (Modelfile bakes in num_ctx=8192, below Hermes's 64K
# hard minimum — confirmed via `POST /api/show` on the Ryzen box, same base
# weights as gemma4:e2b which reports the full 131072 via model_info).
# Every Ryzen/Ollama id below was verified installed + context-length via
# `GET /api/tags` + `POST /api/show` against http://100.65.84.84:11434.
# Every Nous id below was verified to exist via `GET /v1/models` against
# https://inference-api.nousresearch.com.
SWITCHABLE_MODELS = [
    # Ryzen box (Ollama), same custom_providers entry (provider: custom:ryzen-ollama
    # in every profile) — id format matches config.yaml's `model.default` value.
    {"id": "custom:ryzen-ollama:llama3.1:8b",        "label": "Llama 3.1 8B",      "vendor": "Ryzen (Ollama)", "tier": "Fast"},
    {"id": "custom:ryzen-ollama:qwen3.5:9b",         "label": "Qwen 3.5 9B",       "vendor": "Ryzen (Ollama)", "tier": "Balanced"},
    {"id": "custom:ryzen-ollama:gemma4:e2b",         "label": "Gemma 4 E2B",        "vendor": "Ryzen (Ollama)", "tier": "Fast"},
    {"id": "custom:ryzen-ollama:qwen3.6:latest",     "label": "Qwen 3.6",          "vendor": "Ryzen (Ollama)", "tier": "Balanced"},
    {"id": "custom:ryzen-ollama:gemma4:e4b",         "label": "Gemma 4 E4B",        "vendor": "Ryzen (Ollama)", "tier": "Balanced"},
    {"id": "custom:ryzen-ollama:gemma4:12b",         "label": "Gemma 4 12B",        "vendor": "Ryzen (Ollama)", "tier": "Reasoning"},
    {"id": "custom:ryzen-ollama:gemma4:latest",      "label": "Gemma 4 (latest)",   "vendor": "Ryzen (Ollama)", "tier": "Balanced"},
    {"id": "custom:ryzen-ollama:Agen/gemma-4-26B-A4B-it-uncensored-heretic:latest", "label": "Gemma 4 26B Uncensored Heretic", "vendor": "Ryzen (Ollama)", "tier": "Reasoning"},
    # sonct988/gemma4-26b-a4b-it-q4km-256k — verified installed + context via
    # `GET /api/tags` + `POST /api/show` against http://100.65.84.84:11434
    # (2026-09-27): 25.2B params, Q4_K_M quant, 262144 (256K) context.
    {"id": "custom:ryzen-ollama:sonct988/gemma4-26b-a4b-it-q4km-256k:latest", "label": "Gemma 4 26B Q4KM 256K", "vendor": "Ryzen (Ollama)", "tier": "Reasoning"},
    # Nous Portal (provider: nous, base_url https://inference-api.nousresearch.com/v1).
    {"id": "nous:z-ai/glm-5.3-flash",                "label": "GLM5.3 Flash",       "vendor": "Nous", "tier": "Fast"},
    {"id": "nous:openai/gpt-5.6-luna-pro",           "label": "GPT-5.6 Luna Pro",   "vendor": "Nous", "tier": "Premium"},
    {"id": "nous:openai/gpt-5.6-luna",               "label": "GPT-5.6 Luna",       "vendor": "Nous", "tier": "Reasoning"},
    {"id": "nous:upstage/solar-pro4",                "label": "Solar Pro 4",        "vendor": "Nous", "tier": "Balanced"},
    {"id": "nous:poolside/laguna-s-2.1",             "label": "Laguna S 2.1",       "vendor": "Nous", "tier": "Balanced"},
    {"id": "nous:inclusionai/ling-3.0-flash-fin",    "label": "Ling 3.0 Flash Fin", "vendor": "Nous", "tier": "Fast"},
    {"id": "nous:deepseek/deepseek-v4-flash-0731",  "label": "DeepSeek V4 Flash 0731", "vendor": "Nous", "tier": "Fast"},
    # Cloud providers already in use across the fleet.
    {"id": "anthropic:claude-sonnet-5",       "label": "Claude-Sonnet-5",   "vendor": "Anthropic",  "tier": "Premium"},
    {"id": "anthropic:claude-sonnet-4.6",     "label": "Claude-Sonnet-4.6", "vendor": "Anthropic",  "tier": "Premium"},
    # Claude Sonnet 5.5 — verified real via Anthropic's own release notes
    # (2026-09-28): API id claude-sonnet-5-5, same $2/$10 per MTok pricing
    # as Sonnet 5, 1M context window on the Anthropic API.
    {"id": "anthropic:claude-sonnet-5-5",     "label": "Claude-Sonnet-5.5", "vendor": "Anthropic",  "tier": "Premium"},
    # Claude Opus 5.5 — verified real via Anthropic's own release notes/docs
    # (2026-09-27): released 2026-09-22, API id claude-opus-5-5, 1M context,
    # $4/$20 per MTok in/out.
    {"id": "anthropic:claude-opus-5-5",       "label": "Claude-Opus-5.5",   "vendor": "Anthropic",  "tier": "Premium"},
    # Kept even though not in the owner's 2026-09-13 list: Reach's currently
    # live model. Without this the Agents-tab dropdown can't render Reach's
    # real current model as a valid selected value (see agent-mission-control
    # skill's "current model must render as a valid option" rule) — it would
    # silently default to showing the wrong model until someone applies a
    # real switch for Reach.
    {"id": "openrouter:nvidia/nemotron-3.5-lightning:free", "label": "Nemotron 3.5 Lightning (free)", "vendor": "OpenRouter", "tier": "Fast"},
    # Antigravity/Gemini subscription (agy CLI, DirectSDK plugin) — Dev + Dev2's
    # persistent default as of 2026-09-29. Requires the antigravity-subscription-directsdk
    # plugin installed+enabled on the target profile and a working agy login (verified via
    # --usage-file: model=gemini-3.8-flash, provider=antigravity-subscription-directsdk).
    {"id": "antigravity-subscription-directsdk:gemini-3.8-flash", "label": "Gemini 3.8 Flash (Antigravity)", "vendor": "Antigravity", "tier": "Balanced"},
]
_SWITCHABLE_MODEL_IDS = {m["id"] for m in SWITCHABLE_MODELS}


def switch_agent_model(agent, model_id):
    """Live-repoint an agent's config.yaml model.{default,provider,base_url}
    via the real `hermes [-p <profile>] config set` CLI (never edited by
    hand) — the same mechanism `hermes model`/`hermes config set` use, so
    the write goes through Hermes's own validation. Returns (ok, error)."""
    if agent not in get_effective_agents_meta():
        return False, "unknown agent"
    if model_id not in _SWITCHABLE_MODEL_IDS:
        return False, "model not in the allowed switch list"

    # id format is "provider:model" where provider may itself contain a
    # colon (e.g. "custom:ryzen-ollama") — split on the FIRST colon only
    # for provider, everything after is the model string verbatim.
    provider, _, model = model_id.partition(":")
    if provider == "custom":
        # "custom:ryzen-ollama:gemma4:e2b" -> provider "custom:ryzen-ollama",
        # model "gemma4:e2b"
        rest_provider, _, model = model.partition(":")
        provider = f"custom:{rest_provider}"
    base_url = RYZEN_OLLAMA_BASE_URL if provider == "custom:ryzen-ollama" else ""

    profile_flag = [] if agent == "orchestrator" else ["-p", agent]
    argv_prefix = [HERMES_CLI_PATH] + profile_flag + ["config", "set"]
    env = dict(os.environ)
    env["TIRITH_ENABLED"] = "false"
    env["HERMES_HOME"] = HERMES_HOME

    def _set(key, value):
        proc = subprocess.run(
            argv_prefix + [key, value],
            capture_output=True, text=True, timeout=CRON_ACTION_SUBPROCESS_TIMEOUT, env=env,
        )
        if proc.returncode != 0:
            return (proc.stderr or proc.stdout or "").strip()[-500:]
        return None

    for key, value in (("model.provider", provider), ("model.default", model), ("model.base_url", base_url)):
        err = _set(key, value)
        if err:
            return False, f"failed setting {key}: {err}"
    return True, None


BOARD_COLUMNS = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done")
# Statuses a UI move may request; "running" is dispatcher-owned (bridge also refuses it).
BOARD_MOVE_TARGETS = ("triage", "todo", "scheduled", "ready", "blocked", "review", "done")

# Software Dev Dashboard (Demo) -- 4 human roles that may approve review -> done.
# Kept here (not just in the bridge) so the 403 can be returned before even
# shelling out to the bridge subprocess.
SWDEV_HUMAN_ROLES = ("Project Manager", "System Architect", "UI/UX & Product Manager", "Senior Engineer")

# File types allowed through the web upload page: the finished dashboard
# template itself (.html) plus common reference/asset image formats.
ALLOWED_UPLOAD_EXTENSIONS = {".html", ".png", ".jpg", ".jpeg", ".webp", ".svg"}

UPLOAD_PAGE_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Upload &mdash; Agent Mission Control</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background: #0b0f14; color: #e6edf3;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }
  .card { background: #11161c; border: 1px solid #232b33; border-radius: 12px; padding: 32px 36px;
          max-width: 480px; width: 90%; box-shadow: 0 8px 24px rgba(0,0,0,0.4); }
  h1 { font-size: 20px; margin: 0 0 8px; }
  p.sub { color: #8b96a3; font-size: 13px; margin: 0 0 24px; }
  input[type=file] { display: block; width: 100%; margin-bottom: 16px; color: #e6edf3; }
  button { background: #2f81f7; color: white; border: none; border-radius: 8px; padding: 10px 20px;
           font-size: 14px; cursor: pointer; width: 100%; }
  button:hover { background: #4a94ff; }
  button:disabled { background: #445; cursor: not-allowed; }
  #status { margin-top: 18px; font-size: 13px; white-space: pre-wrap; word-break: break-all; }
  #status.ok { color: #3fb950; }
  #status.err { color: #f85149; }
</style>
</head>
<body>
<div class="card">
  <h1>Upload dashboard template</h1>
  <p class="sub">Choose the .html file (or a reference image) to hand off to Dev.</p>
  <input type="file" id="fileInput" accept=".html,.png,.jpg,.jpeg,.webp,.svg">
  <button id="uploadBtn">Upload</button>
  <div id="status"></div>
</div>
<script>
document.getElementById('uploadBtn').addEventListener('click', function () {
  var input = document.getElementById('fileInput');
  var statusEl = document.getElementById('status');
  var btn = document.getElementById('uploadBtn');
  statusEl.className = '';
  statusEl.textContent = '';

  if (!input.files || input.files.length === 0) {
    statusEl.className = 'err';
    statusEl.textContent = 'Please choose a file first.';
    return;
  }

  var file = input.files[0];
  var formData = new FormData();
  formData.append('file', file, file.name);

  btn.disabled = true;
  statusEl.textContent = 'Uploading ' + file.name + ' ...';

  fetch('/api/upload', { method: 'POST', body: formData })
    .then(function (resp) {
      return resp.json().then(function (data) { return { status: resp.status, data: data }; });
    })
    .then(function (result) {
      btn.disabled = false;
      if (result.status === 200 && result.data.ok) {
        statusEl.className = 'ok';
        statusEl.textContent = 'Uploaded successfully.\\nSaved as: ' + result.data.filename +
          '\\nPath: ' + result.data.path;
      } else {
        statusEl.className = 'err';
        statusEl.textContent = 'Upload failed: ' + (result.data.error || 'unknown error');
      }
    })
    .catch(function (err) {
      btn.disabled = false;
      statusEl.className = 'err';
      statusEl.textContent = 'Upload failed: ' + err;
    });
});
</script>
</body>
</html>
"""

SEED_TASKS = [
    ("Reply to sponsor emails", "todo", 2),
    ("Renew business insurance", "todo", 1),
    ("Edit this week's video", "doing", 2),
    ("Prep quarterly budget review", "doing", 1),
    ("Publish the blog post", "done", 1),
    ("Book flights for conference", "done", 1),
]

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _log_error(context, exc):
    """Best-effort stderr logging; never let logging itself raise."""
    try:
        print(f"[mission-control] {context}: {exc!r}")
    except Exception:
        pass
    # Internal failures are real operator-facing signal — surface them in the
    # bell's alert log too, not just stderr. Level "warning" rather than
    # "error": most safe()-guarded exceptions here are transient/read races
    # (a file mid-write, a DB momentarily locked) rather than hard failures.
    add_alert("warning", context, str(exc) or exc.__class__.__name__)


def ro_connect(path):
    """Open a SQLite DB strictly read-only. Raises if the file is missing."""
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.execute("PRAGMA query_only=1;")
    return conn


def safe(fn, default, context):
    """Run fn(), swallow any exception, return default and log the failure."""
    try:
        return fn()
    except Exception as exc:
        _log_error(context, exc)
        return default


def iso_now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Alerts log (bell dropdown) — append-only, capped, project-local JSON file.
# ---------------------------------------------------------------------------

_alerts_lock = threading.Lock()


def _alerts_load():
    if not os.path.exists(ALERTS_JSON_PATH):
        return []
    try:
        with open(ALERTS_JSON_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _alerts_save(items):
    tmp_path = ALERTS_JSON_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(items, f)
    os.replace(tmp_path, ALERTS_JSON_PATH)  # atomic on POSIX — no torn reads


def add_alert(level, title, detail=""):
    """Append one alert entry (level: info/warning/error). Thread-safe,
    capped at ALERTS_MAX_STORED (oldest dropped first). Never raises —
    a failure to log an alert must never break the caller's real work."""
    if level not in ALERTS_LEVELS:
        level = "info"
    entry = {
        "id": uuid.uuid4().hex[:12],
        "level": level,
        "title": str(title)[:200],
        "detail": str(detail)[:500],
        "ts": iso_now(),
    }
    try:
        with _alerts_lock:
            items = _alerts_load()
            items.append(entry)
            if len(items) > ALERTS_MAX_STORED:
                items = items[-ALERTS_MAX_STORED:]
            _alerts_save(items)
    except Exception as exc:
        # Deliberately NOT calling _log_error here — that would recurse
        # back into add_alert on every failure. Best-effort stderr only.
        try:
            print(f"[mission-control] add_alert failed: {exc!r}")
        except Exception:
            pass
    return entry


def read_alerts(limit=10):
    """Most recent `limit` alerts, newest first. Read-only, safe on any
    corruption (returns [] rather than raising)."""
    def _inner():
        items = _alerts_load()
        return list(reversed(items))[:limit]
    return safe(_inner, [], "read_alerts")


# ---------------------------------------------------------------------------
# Data readers — each one is defensive and independent so a single bad read
# never takes down the whole /api/state response.
# ---------------------------------------------------------------------------


def read_health():
    def _inner():
        with open(GATEWAY_STATE_PATH, "r") as f:
            data = json.load(f)
        platforms = data.get("platforms") or {}
        telegram = platforms.get("telegram") or {}
        return {
            "gateway_state": data.get("gateway_state"),
            "telegram_state": telegram.get("state"),
            "updated_at": data.get("updated_at"),
            # The template's applyState() reads d.health.platforms.telegram.state
            # directly (matches gateway_state.json's own nested shape) — keep it
            # alongside the flattened fields above for backward compatibility.
            "platforms": platforms,
            "core_op_state": _core_op_state(data),
        }
    return safe(_inner, {"gateway_state": None, "telegram_state": None, "updated_at": None, "platforms": {}, "core_op_state": "not_connected"}, "read_health")


def _core_op_state(gateway_data):
    """Normalize gateway_state.json's raw gateway_state + platform states into
    exactly the three states the Overview orbit's CORE node distinguishes:
    'live' (gateway running, real work can flow), 'restart' (mid-transition —
    starting/draining/restarting), or 'not_connected' (stopped/failed/unknown/
    stale). Real values observed in gateway_state.json: gateway_state is one
    of running/starting/draining/stopped/startup_failed; platforms.telegram.state
    includes connected/connecting/disconnected/error. A gateway can report
    "running" while its only configured platform is down — that still counts
    as 'restart' (degraded/reconnecting), not a clean 'live', since the
    operator's actual signal path isn't fully up."""
    gw = (gateway_data.get("gateway_state") or "").lower()
    platforms = gateway_data.get("platforms") or {}
    # gateway_state.json is NOT a continuous heartbeat — it's rewritten only
    # on real state transitions, so a healthy gateway can leave "updated_at"
    # hours stale while still very much alive. A wall-clock age check would
    # false-positive "not_connected" on a perfectly healthy gateway. The real
    # staleness signal is whether the PID it recorded is still a live process.
    pid = gateway_data.get("pid")
    if pid:
        alive = True
        try:
            os.kill(int(pid), 0)  # signal 0: existence check only, doesn't kill
        except ProcessLookupError:
            alive = False  # pid is dead -> this file is stale from a crashed process
        except Exception:
            alive = True  # any other error (permission, bad type, etc.) — don't assume dead
        if not alive:
            return "not_connected"
    if gw in ("stopped", "startup_failed", "") or gw is None:
        return "not_connected"
    if gw in ("starting", "draining", "restarting"):
        return "restart"
    if gw == "running":
        # Gateway process itself is up — check whether its platforms are
        # actually connected. Any platform not in a clean "connected"-ish
        # state degrades CORE to 'restart' (reconnecting), never a hard
        # 'not_connected' (the gateway IS running, just not fully healthy).
        if not platforms:
            return "live"  # no configured platforms to check — running is enough
        healthy_states = {"connected", "ok", "ready"}
        any_bad = any((p or {}).get("state") not in healthy_states for p in platforms.values())
        return "restart" if any_bad else "live"
    return "not_connected"


def read_sessions():
    def _inner():
        conn = ro_connect(STATE_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
                "COALESCE(SUM(message_count),0), COUNT(*) FROM sessions"
            )
            in_tok, out_tok, msg_count, total_sessions = cur.fetchone()

            cur.execute(
                "SELECT id, display_name, model, started_at, last_activity_at, message_count "
                "FROM sessions ORDER BY started_at DESC LIMIT 10"
            )
            recent = []
            for row in cur.fetchall():
                sid, display_name, model, started_at, last_activity_at, message_count = row
                recent.append({
                    "id": sid,
                    "display_name": display_name,
                    "model": model,
                    "started_at": started_at,
                    "last_activity_at": last_activity_at,
                    "message_count": message_count,
                })
            return {
                "total_input_tokens": in_tok,
                "total_output_tokens": out_tok,
                "total_messages": msg_count,
                "total_sessions": total_sessions,
                "recent": recent,
            }
        finally:
            conn.close()
    return safe(_inner, {
        "total_input_tokens": 0, "total_output_tokens": 0, "total_messages": 0,
        "total_sessions": 0, "recent": [],
    }, "read_sessions")


def _read_proc_stat():
    with open("/proc/stat", "r") as f:
        for line in f:
            if line.startswith("cpu "):
                parts = line.split()
                # user nice system idle iowait irq softirq steal guest guest_nice
                vals = [int(x) for x in parts[1:]]
                idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                total = sum(vals)
                return idle, total
    raise RuntimeError("no cpu line in /proc/stat")


def read_vps():
    def _inner():
        # CPU: two samples of /proc/stat with a short sleep between them.
        idle1, total1 = _read_proc_stat()
        time.sleep(0.1)
        idle2, total2 = _read_proc_stat()
        d_idle = idle2 - idle1
        d_total = total2 - total1
        cpu_pct = 0.0
        if d_total > 0:
            cpu_pct = round((1.0 - (d_idle / d_total)) * 100, 1)

        # RAM from /proc/meminfo
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key, _, rest = line.partition(":")
                rest = rest.strip().split()[0]
                meminfo[key] = int(rest)  # kB
        mem_total = meminfo.get("MemTotal", 0)
        mem_available = meminfo.get("MemAvailable", 0)
        mem_pct = 0.0
        if mem_total > 0:
            mem_pct = round((1.0 - (mem_available / mem_total)) * 100, 1)

        # Disk via os.statvfs on the project dir's filesystem.
        st = os.statvfs(PROJECT_DIR)
        total_blocks = st.f_blocks
        free_blocks = st.f_bfree
        disk_pct = 0.0
        if total_blocks > 0:
            disk_pct = round((1.0 - (free_blocks / total_blocks)) * 100, 1)

        return {"cpu_pct": cpu_pct, "mem_pct": mem_pct, "disk_pct": disk_pct}
    return safe(_inner, {"cpu_pct": 0.0, "mem_pct": 0.0, "disk_pct": 0.0}, "read_vps")


def _load_model_routing():
    """Optional add-on: tags each model with a tier. Returns {} if absent/broken."""
    def _inner():
        if not os.path.exists(MODEL_ROUTING_PATH):
            return {}
        with open(MODEL_ROUTING_PATH, "r") as f:
            data = json.load(f)
        # Expect something like {"models": {"model-id": {"tier": "premium"}}} or a flat dict.
        if isinstance(data, dict) and "models" in data and isinstance(data["models"], dict):
            return data["models"]
        if isinstance(data, dict):
            return data
        return {}
    return safe(_inner, {}, "load_model_routing")


def _tier_for_model(model_id, routing_map):
    entry = routing_map.get(model_id)
    if isinstance(entry, dict):
        return entry.get("tier", "")
    if isinstance(entry, str):
        return entry
    return ""


def read_agent_logs_raw():
    """Read all rows from agent-logs.db once; used by fleet/models/routing/agentlogs."""
    def _inner():
        conn = ro_connect(AGENT_LOGS_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, agent_name, task_description, model_used, status, created_at "
                "FROM agent_logs"
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "agent_name": r[1],
                    "task_description": r[2],
                    "model_used": r[3],
                    "status": r[4],
                    "created_at": r[5],
                }
                for r in rows
            ]
        finally:
            conn.close()
    return safe(_inner, [], "read_agent_logs_raw")


FAOS_SERVICE_BASE = "http://127.0.0.1:51764"


def faos_proxy_request(method, path, body_bytes=None):
    """Server-side proxy to the standalone FAOS backend (127.0.0.1:51764).

    The FAOS worker-control endpoints (list/detail/pause/kill/reassign) were
    originally called directly from the BROWSER via a hardcoded
    'http://127.0.0.1:51764' base in index.html -- that only works when the
    browser and the FAOS service are on the same machine. Anyone loading this
    dashboard remotely (e.g. over Tailscale) has their own browser try to
    reach port 51764 on ITS OWN device, which is nothing, and the dashboard
    correctly (but confusingly) reports "FAOS service: unreachable".

    Fix: the dashboard's own server process (which DOES sit on the same host
    as the FAOS service) proxies these calls server-side, same pattern
    already used for /api/faos/audit-log and _telegram_api() above -- stdlib
    urllib only, no new dependency. The browser now only ever talks to this
    dashboard's own origin, whether local or over Tailscale.
    """
    import urllib.request
    import urllib.error

    url = FAOS_SERVICE_BASE + path
    req = urllib.request.Request(url, data=body_bytes, method=method)
    if body_bytes is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return 502, json.dumps({"error": f"FAOS service unreachable: {e}"}).encode("utf-8")


def read_faos_audit_log(limit=50):
    """Read-only tail of faos_audit_log — the FAOS service (a separate process,
    127.0.0.1:51764) owns writes to this table; this dashboard only ever reads it,
    same read-only-connection discipline as read_agent_logs_raw() above. The table
    may not exist yet if the FAOS service has never run — that's not an error."""
    def _inner():
        conn = ro_connect(AGENT_LOGS_DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='faos_audit_log'"
            )
            if cur.fetchone() is None:
                return []
            cur.execute(
                "SELECT id, actor, action, target, reason, ok, message, created_at "
                "FROM faos_audit_log ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r[0],
                    "actor": r[1],
                    "action": r[2],
                    "target": r[3],
                    "reason": r[4],
                    "ok": bool(r[5]),
                    "message": r[6],
                    "created_at": r[7],
                }
                for r in rows
            ]
        finally:
            conn.close()
    return safe(_inner, [], "read_faos_audit_log")


# ---------------------------------------------------------------------------
# FAOS Rings view — backend aggregates the 5 concentric rings
# (missions / models / memory / skills / agents) around the FAOS/CE hub.
# Everything here is read-only: config.yaml model.default, skills/ dirs,
# memories/MEMORY.md, missions.db (missions_list), and the live worker roster
# (proxied /api/workers). No placeholders — only real filesystem + DB state.
# ---------------------------------------------------------------------------

# Roster order for the agents ring (innermost). dev2 is a real fleet profile,
# append it to the canonical AGENT_ORDER list (line ~194) as well.
FAOS_RINGS_ROSTER = ("orchestrator", "scout", "scribe", "reach", "dev", "dev2")

# Per-agent UI label/role — mirrors index.html's CHAT_AGENTS_UI + a dev2 row,
# so the rings view uses the same labels the rest of the dashboard does.
FAOS_RINGS_AGENT_META = {
    "orchestrator": {"name": "Orchestrator", "role": "Coordinator"},
    "scout":        {"name": "Spock",        "role": "Research"},
    "scribe":       {"name": "Data",         "role": "Writing"},
    "reach":        {"name": "Seven",        "role": "Marketing"},
    "dev":          {"name": "Scotty",       "role": "Engineering"},
    "dev2":         {"name": "La Forge",     "role": "Engineering (sub-agent)"},
}

def get_effective_agents_meta():
    """Return dynamic AGENT_META merged with registered secondary agents."""
    meta = dict(AGENT_META)
    try:
        for i, s in enumerate(secondary_agents.list_secondary_agents()):
            p = s["profile_name"]
            code = f"A-{7 + i:02d}"
            initials = (s.get("display_name")[:2] if s.get("display_name") else p[:2]).upper()
            meta[p] = {
                "code": code,
                "initials": initials,
                "role": s.get("primary_role") or "Specialist",
                "secondaryRoles": s.get("secondary_roles") or [],
                "channel": f"#{p}",
                "name": s.get("display_name") or p,
                "short": (s.get("display_name") or p).upper()[:4],
                "avatar": s.get("avatar") or "Spare1.jpg",
                "isSecondary": True,
            }
    except Exception as e:
        print(f"[server] get_effective_agents_meta error: {e}")
    return meta


def get_effective_agent_order():
    """Return dynamic AGENT_ORDER including secondary agents in chronological creation order."""
    order = list(AGENT_ORDER)
    try:
        for s in secondary_agents.list_secondary_agents():
            p = s["profile_name"]
            if p not in order:
                order.append(p)
    except Exception as e:
        print(f"[server] get_effective_agent_order error: {e}")
    return order


def get_effective_chat_allowed_agents():
    """Return dynamic allowed agents for chat."""
    allowed = list(CHAT_ALLOWED_AGENTS)
    try:
        for s in secondary_agents.list_secondary_agents():
            p = s["profile_name"]
            if p not in allowed:
                allowed.append(p)
    except Exception as e:
        print(f"[server] get_effective_chat_allowed_agents error: {e}")
    return tuple(allowed)


def get_effective_faos_roster():
    """Return dynamic FAOS_RINGS_ROSTER including secondary agents."""
    roster = list(FAOS_RINGS_ROSTER)
    try:
        for s in secondary_agents.list_secondary_agents():
            p = s["profile_name"]
            if p not in roster:
                roster.append(p)
    except Exception as e:
        print(f"[server] get_effective_faos_roster error: {e}")
    return tuple(roster)


def get_effective_faos_agent_meta():
    """Return dynamic FAOS_RINGS_AGENT_META including secondary agents."""
    meta = dict(FAOS_RINGS_AGENT_META)
    try:
        for s in secondary_agents.list_secondary_agents():
            p = s["profile_name"]
            meta[p] = {
                "name": s.get("display_name") or p,
                "role": s.get("primary_role") or "Specialist",
            }
    except Exception as e:
        print(f"[server] get_effective_faos_agent_meta error: {e}")
    return meta


# Static agent character portraits mapped from profile key -> filename in assets/agents/
FAOS_AGENT_AVATARS = {
    "orchestrator": "JV.jpg",
    "jv":           "JV.jpg",
    "root":         "JV.jpg",
    "scout":        "Scout.jpg",
    "scribe":       "Scribe.jpg",
    "reach":        "Reach.jpg",
    "dev":          "Dev.jpg",
    "dev2":         "Dev2.jpg",
    "ryzen-jv":     "JV.jpg",
    "ryzen-scout":  "Scout.jpg",
    "ryzen-scribe": "Scribe.jpg",
    "ryzen-dev":    "Dev.jpg",
}
FAOS_SPARE_AVATARS = ["Spare1.jpg", "Spare2.jpg", "Spare3.jpg"]

def get_effective_faos_agent_avatars():
    """Return dynamic FAOS_AGENT_AVATARS including secondary agents."""
    avatars = dict(FAOS_AGENT_AVATARS)
    try:
        for s in secondary_agents.list_secondary_agents():
            p = s["profile_name"]
            avatars[p] = s.get("avatar") or "Spare1.jpg"
    except Exception as e:
        print(f"[server] get_effective_faos_agent_avatars error: {e}")
    return avatars

# ---------------------------------------------------------------------------
# Ryzen Remote Fleet Helpers (Cross-Machine Fleet at 100.65.84.84)
# ---------------------------------------------------------------------------

def read_ryzen_dispatches():
    with _ryzen_dispatches_lock:
        if not os.path.isfile(RYZEN_DISPATCHES_PATH):
            return []
        try:
            with open(RYZEN_DISPATCHES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

def save_ryzen_dispatch(dispatch_dict):
    with _ryzen_dispatches_lock:
        items = []
        if os.path.isfile(RYZEN_DISPATCHES_PATH):
            try:
                with open(RYZEN_DISPATCHES_PATH, "r", encoding="utf-8") as f:
                    items = json.load(f)
            except Exception:
                items = []
        updated = False
        for i, item in enumerate(items):
            if item.get("run_id") == dispatch_dict.get("run_id"):
                items[i] = dispatch_dict
                updated = True
                break
        if not updated:
            items.insert(0, dispatch_dict)
        try:
            with open(RYZEN_DISPATCHES_PATH, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2)
        except Exception:
            pass
        return items

def update_pending_ryzen_dispatches(dispatches):
    base = os.environ.get("RYZEN_API_BASE") or _PROJECT_ENV.get("RYZEN_API_BASE", "http://100.65.84.84:8642")
    key = _get_ryzen_key("ryzen-jv")
    if not key:
        return dispatches
    modified = False
    for item in dispatches:
        if item.get("status") in ("started", "running"):
            run_id = item.get("run_id")
            if not run_id:
                continue
            try:
                url = f"{base}/p/ryzen-jv/v1/runs/{run_id}"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    if resp.getcode() == 200:
                        data = json.loads(resp.read().decode("utf-8") or "{}")
                        st = data.get("status")
                        if st and st != item.get("status"):
                            item["status"] = st
                            modified = True
                        if data.get("output") is not None:
                            item["output"] = data.get("output")
                            modified = True
                        if data.get("model"):
                            item["model"] = data.get("model")
                            modified = True
                        if data.get("runtime"):
                            item["runtime"] = data.get("runtime")
                            modified = True
                        if data.get("usage"):
                            item["usage"] = data.get("usage")
                            modified = True
                        item["updated_at"] = datetime.now(timezone.utc).isoformat()
            except Exception:
                pass
    if modified:
        with _ryzen_dispatches_lock:
            try:
                with open(RYZEN_DISPATCHES_PATH, "w", encoding="utf-8") as f:
                    json.dump(dispatches, f, indent=2)
            except Exception:
                pass
    return dispatches

def dispatch_task_to_ryzen(profile, task_text):
    base = os.environ.get("RYZEN_API_BASE") or _PROJECT_ENV.get("RYZEN_API_BASE", "http://100.65.84.84:8642")
    key = _get_ryzen_key(profile)
    if not key:
        return {"ok": False, "error": f"No API key configured for {profile}"}
    url = f"{base}/p/{profile}/v1/runs"
    req_body = json.dumps({"input": task_text}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=8.0) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
            run_id = data.get("run_id") or data.get("id")
            if not run_id:
                return {"ok": False, "error": "No run_id returned by Ryzen API", "raw": data}
            now_iso = datetime.now(timezone.utc).isoformat()
            dispatch_entry = {
                "id": run_id,
                "run_id": run_id,
                "target": profile,
                "input": task_text,
                "status": data.get("status") or "started",
                "created_at": now_iso,
                "updated_at": now_iso,
                "output": None,
                "model": "claude-sonnet-5"
            }
            save_ryzen_dispatch(dispatch_entry)
            _ryzen_fleet_cache["ts"] = 0
            return {"ok": True, "dispatch": dispatch_entry}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def poll_single_ryzen_dispatch(run_id):
    dispatches = read_ryzen_dispatches()
    found = None
    for d in dispatches:
        if d.get("run_id") == run_id:
            found = d
            break
    if not found:
        return None
    if found.get("status") in ("started", "running"):
        base = os.environ.get("RYZEN_API_BASE") or _PROJECT_ENV.get("RYZEN_API_BASE", "http://100.65.84.84:8642")
        key = _get_ryzen_key(found.get("target") or "ryzen-jv")
        if key:
            try:
                url = f"{base}/p/{found.get('target', 'ryzen-jv')}/v1/runs/{run_id}"
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.getcode() == 200:
                        data = json.loads(resp.read().decode("utf-8") or "{}")
                        st = data.get("status")
                        if st:
                            found["status"] = st
                        if data.get("output") is not None:
                            found["output"] = data.get("output")
                        if data.get("model"):
                            found["model"] = data.get("model")
                        if data.get("runtime"):
                            found["runtime"] = data.get("runtime")
                        if data.get("usage"):
                            found["usage"] = data.get("usage")
                        found["updated_at"] = datetime.now(timezone.utc).isoformat()
                        save_ryzen_dispatch(found)
                        _ryzen_fleet_cache["ts"] = 0
            except Exception:
                pass
    return found

def get_ryzen_fleet_payload():
    now = time.time()
    if _ryzen_fleet_cache["data"] is not None and (now - _ryzen_fleet_cache["ts"]) < 5.0:
        return _ryzen_fleet_cache["data"]

    base = os.environ.get("RYZEN_API_BASE") or _PROJECT_ENV.get("RYZEN_API_BASE", "http://100.65.84.84:8642")
    dispatches = read_ryzen_dispatches()
    dispatches = update_pending_ryzen_dispatches(dispatches)

    jv_key = _get_ryzen_key("ryzen-jv")
    reachable = False
    try:
        h_req = urllib.request.Request(f"{base}/health", headers={"Authorization": f"Bearer {jv_key}"} if jv_key else {})
        with urllib.request.urlopen(h_req, timeout=2.0) as resp:
            if resp.getcode() == 200:
                reachable = True
    except Exception:
        reachable = False

    if not reachable:
        payload = {
            "reachable": False,
            "error": "Unreachable (timeout or connection error)",
            "center": {
                "id": "ryzen-jv",
                "name": "Ryzen JV",
                "role": "Orchestrator",
                "status": "idle",
                "avatar": "/assets/agents/JV.jpg",
            },
            "agents": [
                {"id": "ryzen-scout", "name": "Ryzen Scout", "role": "Research", "status": "idle", "avatar": "/assets/agents/Scout.jpg"},
                {"id": "ryzen-scribe", "name": "Ryzen Scribe", "role": "Writing", "status": "idle", "avatar": "/assets/agents/Scribe.jpg"},
                {"id": "ryzen-dev", "name": "Ryzen Dev", "role": "Engineering", "status": "idle", "avatar": "/assets/agents/Dev.jpg"},
            ],
            "sessions": {},
            "dispatches": dispatches,
        }
        _ryzen_fleet_cache["data"] = payload
        _ryzen_fleet_cache["ts"] = now
        return payload

    sessions_by_profile = {}
    def _fetch_prof(prof):
        key = _get_ryzen_key(prof)
        req = urllib.request.Request(f"{base}/p/{prof}/api/sessions", headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                d = json.loads(resp.read().decode("utf-8") or "{}")
                return prof, d.get("data") or []
        except Exception:
            return prof, []

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = dict(ex.map(_fetch_prof, ["ryzen-jv", "ryzen-scout", "ryzen-scribe", "ryzen-dev"]))
        sessions_by_profile = results

    def _is_prof_running(prof):
        if prof == "ryzen-jv":
            if any(d.get("status") in ("started", "running") for d in dispatches):
                return True
        sess_list = sessions_by_profile.get(prof, [])
        for s in sess_list:
            if s.get("ended_at") is None and s.get("last_active"):
                if (now - s.get("last_active")) < 30.0:
                    return True
        return False

    center = {
        "id": "ryzen-jv",
        "name": "Ryzen JV",
        "role": "Orchestrator",
        "status": "running" if _is_prof_running("ryzen-jv") else "idle",
        "avatar": "/assets/agents/JV.jpg",
    }

    outer_agents = [
        {"id": "ryzen-scout", "name": "Ryzen Scout", "role": "Research", "status": "running" if _is_prof_running("ryzen-scout") else "idle", "avatar": "/assets/agents/Scout.jpg"},
        {"id": "ryzen-scribe", "name": "Ryzen Scribe", "role": "Writing", "status": "running" if _is_prof_running("ryzen-scribe") else "idle", "avatar": "/assets/agents/Scribe.jpg"},
        {"id": "ryzen-dev", "name": "Ryzen Dev", "role": "Engineering", "status": "running" if _is_prof_running("ryzen-dev") else "idle", "avatar": "/assets/agents/Dev.jpg"},
    ]

    payload = {
        "reachable": True,
        "error": None,
        "center": center,
        "agents": outer_agents,
        "sessions": sessions_by_profile,
        "dispatches": dispatches,
    }
    _ryzen_fleet_cache["data"] = payload
    _ryzen_fleet_cache["ts"] = now
    return payload


def _faos_avatar_for_profile(profile, spare_idx=0):
    p = (profile or "").lower()
    eff_avatars = get_effective_faos_agent_avatars()
    if p in eff_avatars:
        return f"/assets/agents/{eff_avatars[p]}"
    spare = FAOS_SPARE_AVATARS[spare_idx % len(FAOS_SPARE_AVATARS)]
    return f"/assets/agents/{spare}"


def _faos_config_default_model(config_path):
    """Return the `model.default` string from a Hermes config.yaml, or None.

    Targets the TOP-LEVEL `model:` / `  default:` key only — the flat
    model.default that selects the agent's active model (e.g. the SWITCHABLE_MODELS
    field). Nested `model:` blocks under tts/transcription/etc. are unrelated and
    must not be picked up, so we anchor on a column-0 `model:` line and read the
    first indented `default:` under it.
    """
    if not os.path.isfile(config_path):
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    for i, ln in enumerate(lines):
        if ln.strip() == "model:" and ln == ln.strip():
            for sub in lines[i + 1: i + 6]:
                m = re.match(r"^\s+default:\s*(\S+)\s*$", sub)
                if m:
                    return m.group(1)
            break
    return None


def _faos_profile_paths(profile):
    """Return (config_path, skills_dir, memory_path) for one roster profile.

    orchestrator is Hermes's own default agent (no profiles/ subdir) — its
    files live at the shared root, same asymmetry as AGENT_AVATAR_PATHS.
    """
    if profile == "orchestrator":
        return (
            os.path.join(HERMES_HOME, "config.yaml"),
            os.path.join(HERMES_HOME, "skills"),
            os.path.join(HERMES_HOME, "memories", "MEMORY.md"),
        )
    base = os.path.join(HERMES_HOME, "profiles", profile)
    return (
        os.path.join(base, "config.yaml"),
        os.path.join(base, "skills"),
        os.path.join(base, "memories", "MEMORY.md"),
    )


# Dir entries that are NOT a real skill (Hermes housekeeping / hidden). We
# enumerate skill dirs only; dotted support dirs are excluded, not real skills.
_FAOS_SKILL_SKIP = {".hub", ".locks", ".curator_backups"}
def _faos_skill_names(skills_dir):
    if not os.path.isdir(skills_dir):
        return []
    out = []
    try:
        for name in os.listdir(skills_dir):
            if name.startswith("."):
                continue
            if name in _FAOS_SKILL_SKIP:
                continue
            if os.path.isdir(os.path.join(skills_dir, name)):
                out.append(name)
    except OSError:
        pass
    return sorted(out)


def _faos_live_running_profiles():
    """Profiles with a running kanban worker RIGHT NOW, from the live
    /api/workers poll (proxied to the FAOS backend service). Returns a set of
    profile/assignee names. On any failure, falls back to this server's own
    get_working_agents() (the snapshot used for the Overview orbit)."""
    try:
        status, body = faos_proxy_request("GET", "/api/workers")
        if status == 200:
            data = json.loads(body.decode("utf-8") or "{}")
            profs = set()
            for w in (data.get("workers") or []):
                key = w.get("profile") or w.get("assignee")
                if key:
                    profs.add(key)
            if profs:
                return profs
    except Exception:
        pass
    return set(get_working_agents())


def _faos_state_db_path(profile):
    """Return the state.db path for one roster profile.

    Mirrors _faos_profile_paths' orchestrator asymmetry: orchestrator is
    Hermes's own default agent and its state.db lives at the shared
    HERMES_HOME root, not under profiles/<name>/.
    """
    if profile == "orchestrator":
        return os.path.join(HERMES_HOME, "state.db")
    return os.path.join(HERMES_HOME, "profiles", profile, "state.db")


def _faos_chat_active_profiles():
    """Profiles with a currently-held (non-expired) turn lease in their own
    state.db RIGHT NOW -- the same session_turn_leases mechanism the agent
    core uses to guard against overlapping turns in a bare chat session
    (Telegram/TUI/CLI), independent of any kanban task. Strict "running right
    now" signal only, per the owner's locked 2026-09-28 decision -- no looser
    "recently active" heuristic.

    Returns {profile: {"session_id": str, "expires_at": float, "acquired_at": float}}
    for the most-recently-acquired live lease per profile. Read-only SQLite
    access; any missing/locked/corrupt state.db is skipped, not fatal to the
    caller -- one bad profile must never take down the whole endpoint.
    """
    now = time.time()
    out = {}
    for prof in get_effective_faos_roster():
        db_path = _faos_state_db_path(prof)
        if not os.path.isfile(db_path):
            continue
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            cur = conn.execute(
                "SELECT conversation_id, holder, acquired_at, expires_at "
                "FROM session_turn_leases WHERE expires_at > ? "
                "ORDER BY expires_at DESC LIMIT 1",
                (now,),
            )
            row = cur.fetchone()
            if row:
                conv_id, holder, acquired_at, expires_at = row
                # holder encodes platform as "...:platform=<name>" when present.
                platform = None
                if holder and "platform=" in holder:
                    platform = holder.rsplit("platform=", 1)[-1].strip()
                out[prof] = {
                    "session_id": conv_id,
                    "acquired_at": acquired_at,
                    "expires_at": expires_at,
                    "platform": platform,
                }
        except Exception:
            # Missing table, locked db, corrupt file -- skip this profile only.
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return out


def read_kanban_fleet_tasks():
    """Read all kanban tasks from the shared kanban.db (default board) in read-only mode."""
    if not os.path.isfile(KANBAN_DB_PATH):
        return []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{KANBAN_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        q = (
            "SELECT t.id, t.title, t.body, t.assignee, t.status, t.priority, "
            "t.created_at, t.started_at, t.completed_at, t.result, "
            "r.summary, r.error, r.outcome "
            "FROM tasks t "
            "LEFT JOIN (SELECT task_id, summary, error, outcome, MAX(id) as max_id FROM task_runs GROUP BY task_id) r "
            "ON t.id = r.task_id "
            "ORDER BY t.created_at DESC"
        )
        cur = conn.execute(q)
        return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        _log_error("read_kanban_fleet_tasks", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def run_kanban_task_action(action, task_id):
    """Run/retry a kanban task immediately using CLI subcommands (promote/unblock/dispatch)."""
    if not task_id:
        return False, "task_id is required"
    if not os.path.isfile(KANBAN_DB_PATH):
        return False, "kanban.db not found"
    
    conn = None
    try:
        conn = sqlite3.connect(KANBAN_DB_PATH, timeout=5.0)
        cur = conn.execute("SELECT id, status, assignee FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        if not row:
            return False, f"task {task_id} not found"
        tid, status, assignee = row
        if status == "running":
            return False, "task is already running"
        
        env = dict(os.environ)
        env["TIRITH_ENABLED"] = "false"
        env["HERMES_HOME"] = HERMES_HOME
        env.pop("HERMES_SESSION_SOURCE", None)
        env.pop("HERMES_KANBAN_WORKSPACE", None)
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)

        if status == "blocked":
            subprocess.run([HERMES_CLI_PATH, "kanban", "unblock", task_id], env=env, capture_output=True, text=True, timeout=15)
        elif status == "todo":
            subprocess.run([HERMES_CLI_PATH, "kanban", "promote", task_id, "dashboard: run now"], env=env, capture_output=True, text=True, timeout=15)
        elif status == "done":
            conn.execute("UPDATE tasks SET status = 'ready', completed_at = NULL WHERE id = ?", (task_id,))
            conn.commit()
        # Trigger dispatch pass to spawn immediately
        subprocess.run([HERMES_CLI_PATH, "kanban", "dispatch"], env=env, capture_output=True, text=True, timeout=15)
        return True, None
    except Exception as e:
        _log_error("run_kanban_task_action", e)
        return False, str(e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def faos_rings_payload():
    """Assemble the FAOS /api/faos/rings payload from real state.

     - center   : the FAOS/CE hub node (JV / Orchestrator)
     - agents   : full roster (orchestrator..dev2) + live status
     - models   : one node per distinct top-level model.default across profiles
     - memory   : one node per profile that has a memories/MEMORY.md
     - skills   : one node per distinct skill DIRECTORY name across profiles
     - missions : one node per row from missions.db (missions_list)
     - edges    : agent<->its skill/memory/model nodes, mission -> its assignee
     - kanban_tasks : all fleet tasks with latest run summary
     - cron_jobs    : all root + profile scheduled cron jobs
    """
    running = _faos_live_running_profiles()
    chat_active = _faos_chat_active_profiles()
    center_running = ("orchestrator" in running) or ("root" in running) or ("jv" in running)
    center_chat = ("orchestrator" in chat_active) or ("root" in chat_active) or ("jv" in chat_active)
    if center_running and center_chat:
        center_active_source = "both"
    elif center_running:
        center_active_source = "kanban"
    elif center_chat:
        center_active_source = "chat"
    else:
        center_active_source = None

    center = {
        "id": "orchestrator",
        "label": "JV / CE",
        "name": "JV / CE",
        "role": "Orchestrator",
        "status": "running" if (center_running or center_chat) else "idle",
        "active_source": center_active_source,
        "avatar": "/assets/agents/JV.jpg",
    }

    # Per-profile discovery
    model_by_profile = {}
    skills_by_profile = {}
    memory_meta = {}
    eff_roster = get_effective_faos_roster()
    eff_faos_meta = get_effective_faos_agent_meta()
    for prof in eff_roster:
        cfg, sdir, mpath = _faos_profile_paths(prof)
        model_default = _faos_config_default_model(cfg)
        if model_default:
            model_by_profile[prof] = model_default
        skill_names = _faos_skill_names(sdir)
        if skill_names:
            skills_by_profile[prof] = skill_names
        if os.path.isfile(mpath):
            try:
                st = os.stat(mpath)
                memory_meta[prof] = {
                    "exists": True,
                    "size_bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            except OSError:
                memory_meta[prof] = {"exists": True, "size_bytes": 0, "mtime": 0}

    # —— agents (with live running status snapshot) ——
    # Two independent activity signals, merged: a claimed kanban task (the
    # existing /api/workers poll) OR a live, non-expired session_turn_lease in
    # the profile's own state.db (a bare chat session with no kanban task at
    # all -- Telegram/TUI/CLI). active_source tells the UI honestly which one
    # (or both) fired, so it never implies a kanban task exists when it does not.
    running = _faos_live_running_profiles()
    chat_active = _faos_chat_active_profiles()
    agents = []
    for i, prof in enumerate(eff_roster):
        meta = eff_faos_meta.get(prof, {"name": prof, "role": ""})
        in_kanban = prof in running
        chat_info = chat_active.get(prof)
        in_chat = chat_info is not None
        if in_kanban and in_chat:
            active_source = "both"
        elif in_kanban:
            active_source = "kanban"
        elif in_chat:
            active_source = "chat"
        else:
            active_source = None
        agent_entry = {
            "id": prof,
            "name": meta["name"],
            "role": meta["role"],
            "status": "running" if (in_kanban or in_chat) else "idle",
            "task_id": None,  # real task_id resolved client-side from /api/workers poll
            "active_source": active_source,
            "avatar": _faos_avatar_for_profile(prof, i),
        }
        if chat_info:
            agent_entry["chat_session"] = chat_info
        agents.append(agent_entry)

    # —— models: one node per distinct model string ——
    model_profiles = {}
    for prof, m in model_by_profile.items():
        model_profiles.setdefault(m, []).append(prof)
    for m in model_profiles:
        model_profiles[m].sort()
    models = [
        {"id": "model:" + m, "label": m, "profiles": profs}
        for m, profs in sorted(model_profiles.items())
    ]

    # —— memory: one node per profile with MEMORY.md ——
    memory = [
        {"id": "memory:" + prof, "profile": prof, **meta}
        for prof, meta in sorted(memory_meta.items())
    ]

    # —— skills: one node per distinct skill name ——
    skill_profiles = {}
    for prof, names in skills_by_profile.items():
        for n in names:
            skill_profiles.setdefault(n, []).append(prof)
    for n in skill_profiles:
        skill_profiles[n].sort()
    skills = [
        {"id": "skill:" + n, "name": n, "profiles": sorted(set(profs))}
        for n, profs in sorted(skill_profiles.items())
    ]

    # —— missions: one node per missions.db row ——
    missions = []
    for row in missions_list():
        missions.append({
            "id": "mission:" + row.get("id", ""),
            "title": row.get("title") or "(untitled)",
            "status": row.get("status") or "",
            "board_task_id": row.get("board_task_id") or "",
            # Missions are, by design, the Orchestrator's pipeline (the mission
            # flow always assigns to orchestrator — see the block comment above
            # missions_init). board.db has no assignee column, so orchestrator
            # is the clean, correct assignee, not a guess.
            "assignee": "orchestrator",
            "created_at": row.get("created_at"),
        })

    # —— edges ——
    edges = []
    for prof in eff_roster:
        for skill_name in skills_by_profile.get(prof, []):
            edges.append({"from": prof, "to": "skill:" + skill_name, "kind": "skill"})
        m = model_by_profile.get(prof)
        if m:
            edges.append({"from": prof, "to": "model:" + m, "kind": "model"})
        if prof in memory_meta:
            edges.append({"from": prof, "to": "memory:" + prof, "kind": "memory"})
    for mi in missions:
        if mi.get("assignee"):
            edges.append({"from": mi["id"], "to": mi["assignee"], "kind": "mission"})

    return {
        "center": center,
        "agents": agents,
        "models": models,
        "memory": memory,
        "skills": skills,
        "missions": missions,
        "edges": edges,
        "kanban_tasks": read_kanban_fleet_tasks(),
        "cron_jobs": read_hermes_cron(),
    }


def read_agent_detail(key):
    """Build the /api/agent drawer payload for one agent, from real agent-logs.db
    rows only. Returns None if key isn't a known agent."""
    eff_meta = get_effective_agents_meta()
    if key not in eff_meta:
        return None

    meta = eff_meta[key]
    all_rows = read_agent_logs_raw()
    rows = [r for r in all_rows if r.get("agent_name") == key]

    total = len(rows)
    if total == 0:
        success = 100.0
    else:
        completed = sum(1 for r in rows if r.get("status") == "completed")
        success = round((completed / total) * 100, 1)

    # Per-model task counts for this agent (real usage, not synthetic tiers).
    model_counts = {}
    for r in rows:
        m = r.get("model_used") or "Others"
        model_counts[m] = model_counts.get(m, 0) + 1
    models = [{"model": m, "count": c} for m, c in sorted(model_counts.items(), key=lambda kv: -kv[1])]

    # Most recent failure's task description, if any — surfaced as last_error.
    failed_rows = [r for r in rows if r.get("status") == "failed"]
    last_error = None
    if failed_rows:
        most_recent_failure = max(failed_rows, key=lambda r: (r.get("created_at") or ""))
        last_error = most_recent_failure.get("task_description") or "task failed"

    # Recent tasks, newest first.
    sorted_rows = sorted(rows, key=lambda r: (r.get("created_at") or ""), reverse=True)
    recent = [
        {
            "task": r.get("task_description") or "",
            "status": r.get("status") or "",
            "time": r.get("created_at") or "",
            "model": r.get("model_used") or "",
        }
        for r in sorted_rows[:10]
    ]

    return {
        "initials": meta["initials"],
        "name": meta["name"],
        "role": meta["role"],
        "secondaryRoles": meta.get("secondaryRoles", []),
        "channel": meta["channel"],
        "total": total,
        "success": success,
        "models": models,
        "last_error": last_error,
        "recent": recent,
    }


# ---------------------------------------------------------------------------
# Chat: real Telegram-threaded agent turns, per agent.
# ---------------------------------------------------------------------------

_META_LINE_RE = re.compile(r"^\s*(↻\s*Resumed|session_id\s*:|⚠\s*tirith)", re.IGNORECASE)

# Which agents currently have a live turn in flight — drives the Office tab's
# blue/orange tower glow. Set/cleared only around the actual subprocess
# lifetime (added right before spawn, removed in a finally once the turn
# ends, however it ends: success, error, or timeout).
_working_agents_lock = threading.Lock()
_working_agents = set()


def mark_agent_working(agent):
    with _working_agents_lock:
        _working_agents.add(agent)
    invalidate_snapshot_cache()


def mark_agent_idle(agent):
    with _working_agents_lock:
        _working_agents.discard(agent)
    invalidate_snapshot_cache()


def get_working_agents():
    with _working_agents_lock:
        return sorted(_working_agents)


def _agent_has_active_turn_lease(agent):
    """Ground-truth check: does this agent's own state.db currently hold an
    unexpired turn lease? This catches turns the DASHBOARD itself didn't
    spawn (e.g. a cron job or a Telegram message dispatched by the real
    Hermes gateway) — get_working_agents() alone only tracks turns this
    server's own /api/chat/send spawned, so it would otherwise miss those
    ever showing as 'running' on the Overview orbit. Read-only, safe on any
    failure (missing/locked db -> False, never raises)."""
    def _inner():
        db_path = agent_state_db_path(agent)
        if not os.path.exists(db_path):
            return False
        conn = ro_connect(db_path)
        try:
            cur = conn.execute(
                "SELECT COUNT(*) FROM session_turn_leases WHERE expires_at > ?",
                (time.time(),),
            )
            return (cur.fetchone() or [0])[0] > 0
        finally:
            conn.close()
    return safe(_inner, False, "_agent_has_active_turn_lease")


def _agent_op_state(agent, tasks_rows, working_set):
    """Real-time operational state for one agent's orbit node: 'running'
    (mid-turn right now, from either this server's own tracked subprocess
    or a live turn-lease in its own state.db), 'fault' (its most recent
    logged task ended in a failure/error status), or 'idle' (everything
    else — the default, healthy resting state)."""
    if agent in working_set or _agent_has_active_turn_lease(agent):
        return "running"
    if tasks_rows:
        most_recent = max(tasks_rows, key=lambda r: (r.get("created_at") or ""))
        status = (most_recent.get("status") or "").lower()
        if "failed" in status or "error" in status:
            return "fault"
    return "idle"


def _strip_meta_lines(text):
    """Drop Hermes CLI status/metadata lines (↻ Resumed / session_id: ...)
    from CLI stdout before showing it as a chat reply."""
    lines = [ln for ln in text.splitlines() if not _META_LINE_RE.match(ln)]
    return "\n".join(lines).strip()


def read_chat_history(agent):
    """Read-only: return the agent's full conversation history, merged
    across ALL its non-archived live sessions in chronological order — not
    just the single newest session.

    Hermes periodically rotates to a new session id for the same ongoing
    Telegram thread (compression, restarts, etc.), so relying on "newest
    session only" silently drops everything said in earlier sessions even
    though the conversation is logically continuous from the user's point
    of view. No session yet -> {telegram:False, messages:[]}, which is a
    normal, correct state (not an error).
    """
    if agent not in get_effective_chat_allowed_agents():
        return None

    db_path = agent_state_db_path(agent)
    if not os.path.exists(db_path):
        return {"telegram": False, "messages": []}

    def _inner():
        conn = ro_connect(db_path)
        try:
            cur = conn.cursor()
            # Specialists are ALSO routed via Telegram (the
            # telegram_topic_profiles plugin maps each specialist's
            # #channel topic to their profile), so the same
            # prefer-Telegram rule applies to every agent, not just the
            # Orchestrator — otherwise a throwaway CLI test session can
            # shadow the agent's real ongoing Telegram conversation.
            cur.execute(
                "SELECT id, started_at FROM sessions WHERE archived=0 AND source='telegram' "
                "ORDER BY started_at ASC"
            )
            session_rows = cur.fetchall()
            is_telegram_thread = bool(session_rows)
            if not session_rows:
                # No telegram-sourced session yet — fall back to the newest
                # non-archived session of any source so the drawer still
                # backfills something sensible, but is_telegram stays False.
                cur.execute(
                    "SELECT id, started_at FROM sessions WHERE archived=0 "
                    "ORDER BY started_at DESC LIMIT 1"
                )
                row = cur.fetchone()
                session_rows = [row] if row else []

            if not session_rows:
                return {"telegram": False, "messages": []}

            newest_session_id = session_rows[-1][0]
            is_telegram = (agent == "orchestrator" and is_telegram_thread)

            messages = []
            for session_id, _started_at in session_rows:
                cur.execute(
                    "SELECT role, content, timestamp FROM messages "
                    "WHERE session_id=? AND role IN ('user','assistant') AND active=1 "
                    "ORDER BY timestamp ASC",
                    (session_id,),
                )
                for role, content, ts in cur.fetchall():
                    text = (content or "").strip()
                    if not text:
                        continue  # tool-only assistant turns carry no chat text
                    messages.append({"role": role, "text": text, "ts": ts})

            return {"telegram": is_telegram, "messages": messages, "session_id": newest_session_id}
        finally:
            conn.close()

    return safe(_inner, {"telegram": False, "messages": []}, "read_chat_history")


def run_chat_send_stream(agent, text):
    """Resume the agent's newest live session with a real Hermes CLI turn,
    yielding stdout chunks as they arrive (true streaming, not buffered).

    Spawns via argv list (never shell=True) so user text can never be
    interpreted as shell syntax. TIRITH_ENABLED=false in the subprocess env
    per spec (this is an internal, already-authenticated operator action).

    Yields raw text chunks. The LAST yielded value is always one of:
      ("__error__", message)   on failure
      ("__done__", full_reply) on success, full_reply already meta-stripped
    Callers should treat any other yielded value as a live stdout chunk to
    forward to the client immediately.
    """
    if agent not in get_effective_chat_allowed_agents():
        yield ("__error__", "unknown agent")
        return

    history = read_chat_history(agent)
    session_id = history.get("session_id") if history else None

    argv = [HERMES_CLI_PATH]
    if agent != "orchestrator":
        argv += ["-p", agent]
    if session_id:
        argv += ["chat", "--resume", session_id, "-Q", "-q", text]
    else:
        argv += ["chat", "-Q", "-q", text]

    env = dict(os.environ)
    env["TIRITH_ENABLED"] = "false"
    # CRITICAL: the ambient HERMES_HOME this server itself was launched with
    # may point at a per-profile subdir (e.g. .../.hermes/profiles/dev) if a
    # profile shell started it. That's fine for reading state.db files
    # directly (server.py always uses the fixed root path — see HERMES_HOME
    # above), but it's WRONG to hand down to a spawned `hermes` subprocess:
    # a bare `hermes` call (Orchestrator, no -p) would inherit that env var
    # and look for the session in the wrong profile's state.db, failing with
    # "Session not found" even though the session clearly exists. Force the
    # subprocess's HERMES_HOME to the real shared root so it resolves
    # sessions exactly like an interactive `hermes` invocation would.
    env["HERMES_HOME"] = HERMES_HOME

    mark_agent_working(agent)
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except Exception as exc:
            _log_error("run_chat_send_stream.spawn", exc)
            yield ("__error__", "failed to start agent turn")
            return

        # Hard watchdog: kill the process if it runs past the timeout even if it
        # never produces output (a blocking `for line in proc.stdout` wouldn't
        # otherwise notice a hung, silent subprocess).
        stop_watchdog = threading.Event()   # set when the turn finishes normally, to retire the watchdog
        watchdog_fired = threading.Event()  # set only if the watchdog actually had to kill the process

        def _watchdog():
            if not stop_watchdog.wait(CHAT_SUBPROCESS_TIMEOUT):
                watchdog_fired.set()
                try:
                    proc.kill()
                except Exception:
                    pass

        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        collected = []
        try:
            for line in proc.stdout:
                collected.append(line)
                if not _META_LINE_RE.match(line):
                    yield line
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            yield ("__error__", "agent turn timed out")
            return
        except Exception as exc:
            _log_error("run_chat_send_stream.read", exc)
            try:
                proc.kill()
            except Exception:
                pass
            yield ("__error__", "agent turn failed while streaming")
            return
        finally:
            stop_watchdog.set()  # turn is over one way or another; retire the watchdog

        if watchdog_fired.is_set():
            yield ("__error__", "agent turn timed out")
            return

        if proc.returncode != 0:
            stderr_tail = ""
            try:
                stderr_tail = (proc.stderr.read() or "").strip()[-500:]
            except Exception:
                pass
            yield ("__error__", f"agent exited with code {proc.returncode}: {stderr_tail}")
            return

        reply = _strip_meta_lines("".join(collected))

        # Mirror both sides to Telegram — Orchestrator only, since it's the only
        # agent with a live Telegram thread to mirror into.
        if agent == "orchestrator" and TELEGRAM_HOME_CHANNEL:
            def _mirror(msg):
                try:
                    subprocess.run(
                        [HERMES_CLI_PATH, "send", msg, "--to", f"telegram:{TELEGRAM_HOME_CHANNEL}", "-q"],
                        capture_output=True, text=True, timeout=30, env=env,
                    )
                except Exception as exc:
                    _log_error("run_chat_send_stream.mirror", exc)
            _mirror(text)
            if reply:
                _mirror(reply)

        yield ("__done__", reply)
    finally:
        # However this generator exits — normal completion, an error return,
        # or the caller abandoning iteration early (GeneratorExit) — the
        # agent must never be left stuck "working" on the Office tab.
        mark_agent_idle(agent)


PIPER_VOICE_MODELS = {
    "female": os.path.expanduser("~/.local/share/piper/voices/en_US-lessac-medium/en_US-lessac-medium.onnx"),
    "male": os.path.expanduser("~/.local/share/piper/voices/en_US-ryan-medium/en_US-ryan-medium.onnx"),
}

# Agent voice assignment: distinct voice character per agent
AGENT_PIPER_VOICES = {
    "orchestrator": "male",
    "scout": "female",
    "scribe": "female",
    "reach": "male",
    "dev": "male",
    "dev2": "male",
}

_PIPER_CACHE = {}
_PIPER_LOCK = threading.Lock()

def synthesize_piper_wav(text: str, voice_name: str = "female") -> bytes:
    """Synthesize text to WAV bytes using local piper-tts."""
    clean_text = text.strip()
    if not clean_text:
        return b""
    model_path = PIPER_VOICE_MODELS.get(voice_name) or PIPER_VOICE_MODELS["female"]
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Piper voice model not found: {model_path}")

    from piper.voice import PiperVoice
    with _PIPER_LOCK:
        if model_path not in _PIPER_CACHE:
            _PIPER_CACHE[model_path] = PiperVoice.load(model_path)
        piper_voice = _PIPER_CACHE[model_path]

    buf = io.BytesIO()
    wav = wave.open(buf, "wb")
    try:
        piper_voice.synthesize_wav(clean_text, wav)
    finally:
        wav.close()
    return buf.getvalue()



def build_fleet_models_routing_agentlogs(all_rows):
    """Derive fleet, models, model_usage, routing, agentlogs, agentlogs_stats
    from the raw agent_logs rows, per the exact rules given."""
    routing_map = _load_model_routing()
    has_routing_addon = bool(routing_map)
    working_set = set(get_working_agents())

    total_rows = len(all_rows)

    # Group rows per agent_name.
    eff_meta = get_effective_agents_meta()
    eff_order = get_effective_agent_order()
    by_agent = {name: [] for name in eff_order}
    for row in all_rows:
        name = row.get("agent_name")
        if name in by_agent:
            by_agent[name].append(row)
        # rows for unknown agent names are ignored for fleet cards, but still
        # counted in totals/models/routing/agentlogs below.

    fleet = {}
    for name in eff_order:
        meta = eff_meta[name]
        rows = by_agent[name]
        tasks_today = len(rows)

        if tasks_today == 0:
            success = 100.0
        else:
            completed = sum(1 for r in rows if r.get("status") == "completed")
            success = round((completed / tasks_today) * 100, 1)

        # Most recent row by created_at (ISO strings sort lexically = chronologically).
        # "task" (what it was last doing) still comes from the log history —
        # that's genuinely historical info. But "defaultModel" (what it's
        # configured to run NOW) comes straight from config.yaml below,
        # since the log's model_used goes stale the instant an agent's
        # config is repointed to a different provider without a fresh
        # logged task to reflect it.
        task = ""
        if rows:
            most_recent = max(rows, key=lambda r: (r.get("created_at") or ""))
            task = most_recent.get("task_description") or ""

        configured_model = read_agent_configured_model(name)
        if not configured_model and rows:
            # Fallback only if config.yaml is unreadable/missing — better a
            # stale-but-real value than a blank card.
            most_recent = max(rows, key=lambda r: (r.get("created_at") or ""))
            configured_model = most_recent.get("model_used") or ""
        default_model = configured_model

        share = round((tasks_today / total_rows) * 100) if total_rows > 0 else 0
        load = min(100, tasks_today * 25)
        op_state = _agent_op_state(name, rows, working_set)

        fleet[name] = {
            "code": meta["code"],
            "initials": meta["initials"],
            "role": meta["role"],
            "secondaryRoles": meta.get("secondaryRoles", []),
            "channel": meta["channel"],
            "name": meta["name"],
            "tasksToday": tasks_today,
            "success": success,
            "defaultModel": default_model,
            "share": share,
            "load": load,
            "tokens": f"{tasks_today} tasks",
            "latency": "\u2014",
            "state": "IDLE",
            "opState": op_state,
            "task": task,
            "isSecondary": bool(meta.get("isSecondary")),
            "profile_name": name,
        }

    # Distinct models actually used (from logged task history — this feeds
    # the inference-ledger's usage/cost breakdown, so it stays historical).
    model_counts = {}
    for row in all_rows:
        m = row.get("model_used") or "Others"
        model_counts[m] = model_counts.get(m, 0) + 1

    models = []
    seen_model_ids = set()
    for model_id in sorted(model_counts.keys()):
        tier = _tier_for_model(model_id, routing_map) if has_routing_addon else ""
        models.append({"id": model_id, "label": model_id, "vendor": "", "tier": tier})
        seen_model_ids.add(model_id)
    # Also include each agent's LIVE configured model (from config.yaml) as a
    # selectable option, even if it hasn't been logged yet — otherwise the
    # Agents-tab model picker can't show an agent's real current model as a
    # valid choice right after its config.yaml is repointed.
    for name in eff_order:
        cur = fleet.get(name, {}).get("defaultModel") or ""
        if cur and cur not in seen_model_ids:
            tier = _tier_for_model(cur, routing_map) if has_routing_addon else ""
            models.append({"id": cur, "label": cur, "vendor": "", "tier": tier})
            seen_model_ids.add(cur)

    model_usage = []
    for model_id, count in sorted(model_counts.items(), key=lambda kv: -kv[1]):
        pct = round((count / total_rows) * 100, 1) if total_rows > 0 else 0.0
        model_usage.append({"name": model_id, "count": count, "pct": pct})

    # Routing summary.
    if has_routing_addon:
        premium_calls = 0
        fast_calls = 0
        for row in all_rows:
            tier = _tier_for_model(row.get("model_used") or "", routing_map)
            if tier == "fast":
                fast_calls += 1
            else:
                premium_calls += 1
        offload_pct = round((fast_calls / total_rows) * 100) if total_rows > 0 else 0
    else:
        premium_calls = total_rows
        fast_calls = 0
        offload_pct = 0

    routing = {
        "total": total_rows,
        "models": len(model_counts),
        "premium_calls": premium_calls,
        "fast_calls": fast_calls,
        "offload_pct": offload_pct,
    }

    # Recent activity feed — newest first, short agent codes.
    sorted_rows = sorted(all_rows, key=lambda r: (r.get("created_at") or ""), reverse=True)
    agentlogs = []
    for row in sorted_rows[:50]:
        name = row.get("agent_name")
        short = eff_meta.get(name, {}).get("short", (name or "??").upper()[:4])
        agentlogs.append({
            "agent": short,
            "task": row.get("task_description") or "",
            "time": row.get("created_at") or "",
            "model": row.get("model_used") or "",
            "status": row.get("status") or "",
        })

    completed_count = sum(1 for r in all_rows if r.get("status") == "completed")
    failed_count = sum(1 for r in all_rows if r.get("status") == "failed")
    agentlogs_stats = {"total": total_rows, "completed": completed_count, "failed": failed_count}

    return fleet, models, model_usage, routing, agentlogs, agentlogs_stats


# ---------------------------------------------------------------------------
# Personal Tasks board — now backed by REAL Hermes Kanban (board slug "personal"),
# via kanban_bridge.py (a subprocess under the Hermes install's own venv, since
# this server runs on bare python3.11 without hermes_cli importable). board.db
# is kept only as an emergency fallback if the bridge is ever unavailable.
# ---------------------------------------------------------------------------

_board_lock = threading.Lock()

_KANBAN_BRIDGE_PATH = os.path.join(PROJECT_DIR, "kanban_bridge.py")


def _resolve_kanban_bridge_python():
    """Find a real, current Hermes venv python3 rather than hardcoding one
    install/environment hash — `hermes update` rotates these, and a stale
    path here means the bridge subprocess fails at launch every single
    time, silently (board_list()'s safe() wrapper swallows it into []).
    Picks the most-recently-modified venv under any install's
    environments/ dir (freshest = the one the running `hermes` actually
    uses after an update), falling back to whatever `python3` resolves to
    on PATH if none is found."""
    installs_root = os.path.expanduser("~/.hermes/installs")
    candidates = []
    if os.path.isdir(installs_root):
        for install_id in os.listdir(installs_root):
            envs_dir = os.path.join(installs_root, install_id, "environments")
            if not os.path.isdir(envs_dir):
                continue
            for env_id in os.listdir(envs_dir):
                py = os.path.join(envs_dir, env_id, "venv", "bin", "python3")
                if os.path.isfile(py):
                    candidates.append((os.path.getmtime(py), py))
    if candidates:
        return max(candidates, key=lambda c: c[0])[1]
    return _shutil.which("python3") or "python3"


def _kanban_bridge_call(cmd, payload=None):
    """Run one kanban_bridge.py command; raises RuntimeError with the bridge's
    own error message on failure (never silently falls through to board.db —
    a loud failure here is safer than a card silently going nowhere)."""
    bridge_python = _resolve_kanban_bridge_python()
    proc = subprocess.run(
        [bridge_python, _KANBAN_BRIDGE_PATH],
        input=json.dumps({"cmd": cmd, "payload": payload or {}}),
        capture_output=True, text=True, timeout=25,
    )
    # The bridge always prints exactly one JSON line on stdout, even on error;
    # anything else (a traceback in stderr) means it crashed before that point.
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (ValueError, IndexError):
        out = {}
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or proc.stderr.strip() or "kanban bridge failed")
    return out["result"]


def board_connect():
    conn = sqlite3.connect(BOARD_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def board_init():
    """No-op for storage: the real Kanban 'personal' board self-inits via
    kanban_db.init_db() on first bridge call. board.db is left alone as a
    historical fallback file, not seeded or read on the normal path."""
    return


def board_list():
    def _inner():
        return _kanban_bridge_call("list")
    return safe(_inner, [], "board_list")


def board_create(payload):
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    status = payload.get("status") or "todo"
    if status not in ("triage", "todo", "ready"):
        status = "todo"
    priority = payload.get("priority", 1)
    try:
        priority = int(priority)
    except (TypeError, ValueError):
        priority = 1
    notes = payload.get("notes") or ""

    with _board_lock:
        result = _kanban_bridge_call(
            "create", {"title": title, "status": status, "priority": priority, "notes": notes})
        return result["id"]


def board_update(task_id, payload):
    if not task_id:
        raise ValueError("id is required")

    body = {"id": task_id}
    if "title" in payload:
        body["title"] = (payload.get("title") or "").strip()
    if "status" in payload:
        status = payload.get("status")
        if status == "running":
            raise ValueError("Cannot move a task into 'running': the dispatcher owns that")
        if status not in BOARD_MOVE_TARGETS:
            raise ValueError(f"invalid status: {status}")
        body["status"] = status
        if payload.get("reason"):
            body["reason"] = str(payload["reason"])
    if "priority" in payload:
        try:
            body["priority"] = int(payload.get("priority"))
        except (TypeError, ValueError):
            body["priority"] = 1
    if "notes" in payload:
        body["notes"] = payload.get("notes") or ""

    if len(body) == 1:  # only "id" present, nothing to change
        return False

    with _board_lock:
        result = _kanban_bridge_call("update", body)
        return bool(result.get("ok"))


def board_show(task_id):
    if not task_id:
        raise ValueError("id is required")
    return _kanban_bridge_call("show", {"id": task_id})


def board_action(action, payload):
    """comment / assign / block / unblock via the bridge (actor fixed to 'ngom')."""
    if action not in ("comment", "assign", "block", "unblock"):
        raise ValueError(f"unknown action: {action}")
    if not payload.get("id"):
        raise ValueError("id is required")
    with _board_lock:
        return _kanban_bridge_call(action, {k: payload.get(k) for k in ("id", "body", "assignee", "reason")})


def board_delete(task_id):
    if not task_id:
        raise ValueError("id is required")
    with _board_lock:
        result = _kanban_bridge_call("delete", {"id": task_id})
        return bool(result.get("ok"))


# ---------------------------------------------------------------------------
# Software Dev Dashboard (Demo) -- board slug "swdev-demo", via
# swdev_demo_bridge.py (same subprocess-under-Hermes-venv pattern as the
# "personal" board above, kept on its own bridge script/board/lock so the
# two never interleave or cross-write).
# ---------------------------------------------------------------------------

_swdev_lock = threading.Lock()
_SWDEV_BRIDGE_PATH = os.path.join(PROJECT_DIR, "swdev_demo_bridge.py")


def _swdev_bridge_call(cmd, payload=None):
    """Same contract as _kanban_bridge_call: one JSON command in, one JSON
    result out, loud failure (never a silent empty board)."""
    bridge_python = _resolve_kanban_bridge_python()
    proc = subprocess.run(
        [bridge_python, _SWDEV_BRIDGE_PATH],
        input=json.dumps({"cmd": cmd, "payload": payload or {}}),
        capture_output=True, text=True, timeout=25,
    )
    try:
        out = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (ValueError, IndexError):
        out = {}
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or proc.stderr.strip() or "swdev bridge failed")
    return out["result"]


def swdev_list():
    def _inner():
        return _swdev_bridge_call("list")
    return safe(_inner, [], "swdev_list")


def swdev_show(task_id):
    if not task_id:
        raise ValueError("id is required")
    return _swdev_bridge_call("show", {"id": task_id})


def swdev_create(payload):
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    actor = (payload.get("actor") or "").strip()
    if not actor:
        raise ValueError("actor is required")
    body = {
        "title": title,
        "description": payload.get("description") or "",
        "assignee_role": payload.get("assignee_role") or None,
        "actor": actor,
    }
    with _swdev_lock:
        result = _swdev_bridge_call("create", body)
        return result


def swdev_move(payload):
    task_id = payload.get("task_id")
    to_status = payload.get("to_status")
    actor = (payload.get("actor") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not to_status:
        raise ValueError("to_status is required")
    if not actor:
        raise ValueError("actor is required")
    if to_status == "running":
        raise ValueError("Cannot move a task into 'running': the dispatcher owns that")
    if to_status not in BOARD_MOVE_TARGETS:
        raise ValueError(f"invalid status: {to_status}")

    with _swdev_lock:
        # The core gate, enforced server-side (not just in the UI, and not
        # just in the bridge): a review -> done transition may only be
        # performed by one of the 4 human roles, never an agent profile.
        # Checked here -- before the bridge call -- so an agent actor gets a
        # clean 403 rather than depending on the bridge's own refusal.
        current = _swdev_bridge_call("show", {"id": task_id})
        current_status = (current.get("task") or {}).get("status")
        if current_status == "review" and to_status == "done" and actor not in SWDEV_HUMAN_ROLES:
            raise PermissionError(
                f"only a human role may approve review -> done (got actor={actor!r})"
            )
        result = _swdev_bridge_call(
            "move", {"task_id": task_id, "to_status": to_status, "actor": actor,
                     "reason": payload.get("reason")})
        return result


def swdev_comment(payload):
    task_id = payload.get("task_id")
    actor = (payload.get("actor") or "").strip()
    body = (payload.get("body") or "").strip()
    if not task_id:
        raise ValueError("task_id is required")
    if not actor:
        raise ValueError("actor is required")
    if not body:
        raise ValueError("body is required")
    with _swdev_lock:
        return _swdev_bridge_call("comment", {"task_id": task_id, "actor": actor, "body": body})


# ---------------------------------------------------------------------------
# Missions — the "assign this task to the Orchestrator" PRD pipeline.
#
# Flow: a board task gets "assigned" (mission row created, status=pending_
# approval, a real Telegram forum topic is created for it, a brief is sent
# there). The owner approves either via the dashboard button or by replying
# "approve" in that Telegram topic. On approval, the dashboard kicks off the
# Orchestrator's first turn in that topic's own Hermes session — from then
# on the owner and the Orchestrator work the PRD interactively in Telegram,
# with the Orchestrator using its own delegate_task tool to pull in Scout
# (research), Scribe (PRD review/tightening), Dev (build spec) or Reach
# (implementation plan / investor pitch) as needed. All of that happens
# inside Hermes itself — this server's job is just: create the topic, send
# the kickoff, and journal state to Obsidian + missions.db so a mission
# spanning many days doesn't have to re-explain itself from scratch.
# ---------------------------------------------------------------------------

_missions_lock = threading.Lock()
MISSION_STATUSES = (
    "pending_approval",  # brief sent, waiting for owner's "approve"
    "prd_in_progress",   # approved, Orchestrator + owner drafting the PRD
    "researching",       # Orchestrator delegated a gap to Scout
    "reviewing",         # Orchestrator delegated PRD polish to Scribe
    "ready_to_build",    # Dev build-spec handoff done
    "ready_to_pitch",    # Reach implementation-plan/investor-pitch handoff done
    "done",
)


def missions_connect():
    conn = sqlite3.connect(MISSIONS_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def missions_init():
    with _missions_lock:
        conn = missions_connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS missions (
                    id TEXT PRIMARY KEY,
                    board_task_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    telegram_thread_id TEXT,
                    telegram_chat_id TEXT,
                    obsidian_note_path TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    approved_at REAL,
                    approved_via TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()


def missions_list():
    def _inner():
        conn = missions_connect()
        try:
            cur = conn.execute("SELECT * FROM missions ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
        finally:
            conn.close()
    return safe(_inner, [], "missions_list")


def missions_get(mission_id):
    def _inner():
        conn = missions_connect()
        try:
            cur = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    return safe(_inner, None, "missions_get")


def missions_get_by_task(board_task_id):
    def _inner():
        conn = missions_connect()
        try:
            cur = conn.execute(
                "SELECT * FROM missions WHERE board_task_id = ? ORDER BY created_at DESC LIMIT 1",
                (board_task_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    return safe(_inner, None, "missions_get_by_task")


def missions_create_row(mission_id, board_task_id, title, thread_id, chat_id, note_path):
    with _missions_lock:
        conn = missions_connect()
        try:
            now = time.time()
            conn.execute(
                "INSERT INTO missions (id, board_task_id, title, status, telegram_thread_id, "
                "telegram_chat_id, obsidian_note_path, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending_approval', ?, ?, ?, ?, ?)",
                (mission_id, board_task_id, title, str(thread_id), str(chat_id), note_path, now, now),
            )
            conn.commit()
        finally:
            conn.close()


def missions_update_status(mission_id, status, approved_via=None):
    if status not in MISSION_STATUSES:
        raise ValueError(f"invalid mission status: {status}")
    with _missions_lock:
        conn = missions_connect()
        try:
            now = time.time()
            if approved_via:
                conn.execute(
                    "UPDATE missions SET status = ?, updated_at = ?, approved_at = ?, approved_via = ? WHERE id = ?",
                    (status, now, now, approved_via, mission_id),
                )
            else:
                conn.execute(
                    "UPDATE missions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, mission_id),
                )
            conn.commit()
            return True
        finally:
            conn.close()


def _mission_slug(title):
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "mission"


def _telegram_api(method, payload):
    """POST to the Telegram Bot API using stdlib urllib only (no requests
    dependency). Returns the parsed JSON response dict, or raises on
    network/HTTP failure so callers can decide how to handle it."""
    import urllib.request
    import urllib.error

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def create_mission_telegram_topic(title):
    """Create a real Telegram forum topic for this mission and register it
    in topics.json under a synthetic profile name so future messages in
    that topic route back to the Orchestrator (multiplex_profiles routing
    keys off thread_id -> profile; 'orchestrator' has no separate profile
    dir, so we intentionally do NOT add an entry — the orchestrator is the
    gateway's default agent when no profile mapping matches, exactly like
    the #command channel already works)."""
    name = f"Mission: {title}"[:128]
    result = _telegram_api("createForumTopic", {"chat_id": TELEGRAM_GROUP_CHAT_ID, "name": name})
    if not result.get("ok"):
        raise RuntimeError(f"createForumTopic failed: {result}")
    thread_id = result["result"]["message_thread_id"]
    return thread_id


def _obsidian_mission_note_path(title, mission_id):
    slug = _mission_slug(title)
    missions_dir = os.path.join(OBSIDIAN_VAULT_PATH, "Missions")
    os.makedirs(missions_dir, exist_ok=True)
    return os.path.join(missions_dir, f"{slug}-{mission_id[:8]}.md")


def _write_mission_prd_note(note_path, title, mission_id, board_task_id, thread_id):
    now_iso = datetime.now(timezone.utc).isoformat()
    content = f"""# Mission: {title}

- **Mission ID:** `{mission_id}`
- **Board task:** `{board_task_id}`
- **Telegram topic (thread_id):** `{thread_id}`
- **Status:** pending_approval
- **Created:** {now_iso}

## PRD

### Problem

_(to be filled in with the owner)_

### Goals

_(to be filled in with the owner)_

### Users

_(to be filled in with the owner)_

### Scope

_(to be filled in with the owner)_

### Requirements

_(to be filled in with the owner; research gaps get delegated to [[Scout]])_

### Open Questions

_(to be filled in with the owner)_

### Success Metrics

_(to be filled in with the owner)_

## Delegation Log

_(Orchestrator logs each delegation here: what was asked, to whom, and the outcome, so re-opening this mission days later doesn't repeat work.)_

## Status Log

- {now_iso} — mission created, brief sent, awaiting owner approval.
"""
    with open(note_path, "w") as f:
        f.write(content)


def append_mission_status_log(note_path, line):
    """Best-effort append-only status log line to the mission's Obsidian
    note. Never raises — a note-append failure should never block the
    actual mission workflow."""
    try:
        if not os.path.exists(note_path):
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        with open(note_path, "a") as f:
            f.write(f"- {now_iso} — {line}\n")
    except Exception as exc:
        _log_error("append_mission_status_log", exc)


def update_mission_note_status_field(note_path, status):
    """Best-effort update of the '- **Status:** ...' header line in the
    mission's Obsidian note so the top-of-file summary stays accurate,
    separate from the append-only Status Log section further down."""
    try:
        if not os.path.exists(note_path):
            return
        with open(note_path, "r") as f:
            text = f.read()
        new_text = re.sub(
            r"^- \*\*Status:\*\* .*$", f"- **Status:** {status}", text, count=1, flags=re.MULTILINE
        )
        if new_text != text:
            with open(note_path, "w") as f:
                f.write(new_text)
    except Exception as exc:
        _log_error("update_mission_note_status_field", exc)


def send_mission_brief(thread_id, title, notes, note_path):
    brief = (
        f"🗂️ New mission assigned: *{title}*\n\n"
        + (f"{notes}\n\n" if notes else "")
        + "This will become a Project Requirement Document (PRD) we build together, "
        "iteratively — I'll pull in Scout for research gaps, Scribe to review/tighten it, "
        "and hand it to Dev (build spec) or Reach (implementation plan / investor pitch) "
        "once it's ready.\n\n"
        f"Progress is journaled at `{note_path}` so we can pick this up anytime without "
        "starting over.\n\n"
        "Reply *approve* here (or click Approve on the dashboard) when you're ready to start."
    )
    target = f"telegram:{TELEGRAM_GROUP_CHAT_ID}:{thread_id}"
    subprocess.run(
        [HERMES_CLI_PATH, "send", brief, "--to", target, "-q"],
        capture_output=True,
        text=True,
        timeout=MISSION_SUBPROCESS_TIMEOUT,
    )


def start_mission_orchestrator_session(mission_id, thread_id, title, note_path):
    """Post the PRD kickoff framing into the mission's Telegram topic as a
    static message via the same proven `hermes send` path used for the
    mission brief (see send_mission_brief) — NOT by trying to fake a fresh
    agent turn via hand-crafted session-key env vars, which would rely on
    internal gateway session machinery this server has no business faking.

    The actual live Orchestrator turn happens the moment the owner sends
    their first real message in this topic: the gateway is already running
    as its own service and already handles every Telegram message through
    its normal, well-tested routing (this is exactly how every other topic
    — #dev, #scout, #command, etc. — already works). This server's only
    job here is to make sure the topic and its framing exist BEFORE that
    happens, not to simulate the agent turn itself."""
    kickoff = (
        f"✅ Mission *{title}* approved — this topic is now its own dedicated session.\n\n"
        "Send your first message here to begin: which PRD section do you want to start with — "
        "Problem, Goals, Users, Scope, Requirements, Open Questions, or Success Metrics?\n\n"
        f"Progress is journaled at `{note_path}`."
    )
    target = f"telegram:{TELEGRAM_GROUP_CHAT_ID}:{thread_id}"
    try:
        subprocess.run(
            [HERMES_CLI_PATH, "send", kickoff, "--to", target, "-q"],
            capture_output=True,
            text=True,
            timeout=MISSION_SUBPROCESS_TIMEOUT,
        )
    except Exception as exc:
        _log_error("start_mission_orchestrator_session", exc)


def approve_mission(mission_id, via):
    """Shared approval path for BOTH the dashboard button and the
    Telegram-detected 'approve' reply — flips status, journals the
    Obsidian note, and posts the kickoff framing. `via` is 'dashboard' or
    'telegram' purely for the audit trail (approved_via column / log line)."""
    mission = missions_get(mission_id)
    if not mission or mission["status"] != "pending_approval":
        return False
    missions_update_status(mission_id, "prd_in_progress", approved_via=via)
    update_mission_note_status_field(mission["obsidian_note_path"], "prd_in_progress")
    append_mission_status_log(
        mission["obsidian_note_path"], f"approved via {via}; PRD work begins"
    )
    start_mission_orchestrator_session(
        mission_id, mission["telegram_thread_id"], mission["title"], mission["obsidian_note_path"]
    )
    return True


def check_telegram_approvals():
    """Read-only scan: for every mission still pending_approval, check the
    Orchestrator's real Telegram session data for a user message containing
    'approve' in that mission's own topic thread, sent after the mission
    was created. Read-only against state.db (mode=ro); only ever WRITES to
    missions.db via approve_mission(). Safe to call frequently — a no-op
    scan is cheap and this never blocks the request that triggers it."""
    def _inner():
        pending = [m for m in (missions_list() or []) if m["status"] == "pending_approval"]
        if not pending:
            return
        conn = ro_connect(STATE_DB_PATH)
        try:
            cur = conn.cursor()
            for mission in pending:
                thread_id = mission.get("telegram_thread_id")
                if not thread_id:
                    continue
                cur.execute(
                    "SELECT m.content FROM messages m "
                    "JOIN sessions s ON s.id = m.session_id "
                    "WHERE s.source='telegram' AND s.thread_id=? AND m.role='user' "
                    "AND m.timestamp >= ? AND m.active=1 "
                    "ORDER BY m.timestamp ASC",
                    (str(thread_id), mission["created_at"]),
                )
                for (content,) in cur.fetchall():
                    text = (content or "").strip().lower()
                    # User-sourced messages are stored with a "[Display
                    # Name] " prefix (e.g. "[Cheng HN] approve") — strip
                    # any bracketed prefix before matching, and require the
                    # remaining text to BE "approve" (optionally with
                    # trailing punctuation), not just start with it, so
                    # "approve the budget too" or similar doesn't misfire.
                    stripped = re.sub(r"^\[[^\]]*\]\s*", "", text).strip()
                    if re.fullmatch(r"approve[.!]?", stripped):
                        approve_mission(mission["id"], via="telegram")
                        break
        finally:
            conn.close()

    safe(_inner, None, "check_telegram_approvals")


# ---------------------------------------------------------------------------
# File upload — lets the owner hand off a prebuilt HTML template (or a
# reference image) through the browser instead of a terminal/CLI step.
# ---------------------------------------------------------------------------

_upload_lock = threading.Lock()


def safe_upload_filename(raw_name):
    """Validate an uploaded filename: no path traversal, allowed extension only.
    Returns the sanitized basename, or raises ValueError."""
    if not raw_name:
        raise ValueError("missing filename")

    # Browsers should only ever send a basename, but be defensive anyway:
    # strip any directory components a hostile client might smuggle in.
    basename = os.path.basename(raw_name.replace("\\", "/"))

    if not basename or basename in (".", ".."):
        raise ValueError("invalid filename")
    if "/" in raw_name or ".." in raw_name:
        raise ValueError("path traversal is not allowed")

    _, ext = os.path.splitext(basename)
    if ext.lower() not in ALLOWED_UPLOAD_EXTENSIONS:
        raise ValueError(
            f"file type '{ext}' is not allowed (allowed: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))})"
        )

    return basename


def save_uploaded_file(filename, data_bytes):
    """Write the uploaded bytes into the project directory. Thread-safe."""
    safe_name = safe_upload_filename(filename)
    dest_path = os.path.join(PROJECT_DIR, safe_name)

    # Belt-and-suspenders: confirm the resolved path really stays inside
    # PROJECT_DIR even after sanitization, before touching disk.
    resolved = os.path.realpath(dest_path)
    if os.path.commonpath([resolved, PROJECT_DIR]) != PROJECT_DIR:
        raise ValueError("resolved path escapes the project directory")

    with _upload_lock:
        with open(dest_path, "wb") as f:
            f.write(data_bytes)

    return safe_name, dest_path


# ---------------------------------------------------------------------------
# Content library: real Markdown files on disk under CONTENT_DIR, one folder
# per agent. Strict path safety on every call — agent must be in the known
# list, filenames may never contain ".." or "/".
# ---------------------------------------------------------------------------

_content_lock = threading.Lock()
_FILENAME_SAFE_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")


def content_agent_dir(agent):
    return os.path.join(CONTENT_DIR, agent)


def validate_content_agent(agent):
    if agent not in CONTENT_ALLOWED_AGENTS:
        raise ValueError(f"unknown agent: {agent}")
    return agent


def validate_content_filename(filename):
    if not filename:
        raise ValueError("filename is required")
    if ".." in filename or "/" in filename or "\\" in filename:
        raise ValueError("path traversal is not allowed")
    if not _FILENAME_SAFE_RE.match(filename):
        raise ValueError("filename must be a plain .md name (letters, numbers, dot, dash, underscore)")
    return filename


def content_file_path(agent, filename):
    """Validate agent + filename, resolve to an absolute path, and confirm
    the resolved path stays inside CONTENT_DIR/<agent>/ before returning it."""
    validate_content_agent(agent)
    validate_content_filename(filename)
    agent_dir = content_agent_dir(agent)
    dest_path = os.path.join(agent_dir, filename)
    resolved = os.path.realpath(dest_path)
    resolved_agent_dir = os.path.realpath(agent_dir)
    if os.path.commonpath([resolved, resolved_agent_dir]) != resolved_agent_dir:
        raise ValueError("resolved path escapes the agent's content directory")
    return dest_path


def _extract_title(text, fallback):
    """First '# Title' line, else the filename-derived fallback."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or fallback
        if stripped:
            break  # first non-blank line wasn't a heading; stop looking
    return fallback


def list_content_docs():
    """Scan every agent folder under CONTENT_DIR and return
    [{agent, filename, title, modified_at}], newest-first."""
    def _inner():
        docs = []
        for agent in CONTENT_ALLOWED_AGENTS:
            agent_dir = content_agent_dir(agent)
            if not os.path.isdir(agent_dir):
                continue
            for filename in os.listdir(agent_dir):
                if not filename.endswith(".md"):
                    continue
                try:
                    validate_content_filename(filename)
                except ValueError:
                    continue  # skip anything that doesn't match the naming convention
                full_path = os.path.join(agent_dir, filename)
                if not os.path.isfile(full_path):
                    continue
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        text = f.read()
                except OSError:
                    continue
                mtime = os.path.getmtime(full_path)
                title = _extract_title(text, filename)
                docs.append({
                    "agent": agent,
                    "filename": filename,
                    "title": title,
                    "modified_at": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                })
        docs.sort(key=lambda d: d["modified_at"], reverse=True)
        return docs
    return safe(_inner, [], "list_content_docs")


def read_content_doc(agent, filename):
    """Return the raw text of one doc, or raise ValueError/FileNotFoundError."""
    path = content_file_path(agent, filename)
    if not os.path.isfile(path):
        raise FileNotFoundError(filename)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def save_content_doc(agent, filename, text):
    """Create or overwrite a doc. Thread-safe; creates the agent folder if needed."""
    path = content_file_path(agent, filename)
    agent_dir = os.path.dirname(path)
    with _content_lock:
        os.makedirs(agent_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return path


def delete_content_doc(agent, filename):
    """Unlink a doc. Raises FileNotFoundError if it doesn't exist."""
    path = content_file_path(agent, filename)
    with _content_lock:
        if not os.path.isfile(path):
            raise FileNotFoundError(filename)
        os.remove(path)


# ---------------------------------------------------------------------------
# Hermes cron: read-only parse of jobs.json for display; write actions go
# through the real `hermes cron` CLI (argv list, never shell).
# ---------------------------------------------------------------------------


def _read_one_cron_file(path, profile_tag):
    """Parse a single jobs.json (root or a specialist's profile dir) into
    the dashboard's Schedule-tab job shape, tagged with which profile owns
    it (so actions know which `hermes -p <profile> cron ...` to run)."""
    if not os.path.exists(path):
        return []
    with open(path, "r") as f:
        data = json.load(f)
    jobs = data.get("jobs") or []
    out = []
    for job in jobs:
        schedule = job.get("schedule") or {}
        schedule_display = job.get("schedule_display") or schedule.get("display") or ""
        out.append({
            "id": job.get("id"),
            "name": job.get("name") or "(unnamed job)",
            "enabled": bool(job.get("enabled")),
            "state": job.get("state") or ("paused" if not job.get("enabled") else "scheduled"),
            "schedule": schedule_display,
            "next_run_at": job.get("next_run_at"),
            "last_status": job.get("last_status"),
            "last_error": job.get("last_error"),
            "deliver": job.get("deliver") or "origin",
            "model": job.get("model"),
            "prompt": job.get("prompt") or "",
            "profile": profile_tag,
        })
    return out


def read_hermes_cron():
    """Parse the root ~/.hermes/cron/jobs.json (orchestrator's own jobs)
    PLUS every specialist profile's own cron/jobs.json (read-only), merged
    into one list the dashboard's Schedule tab can show and act on.
    Missing files / no jobs -> [] for that source, never an error."""
    def _inner():
        out = list(_read_one_cron_file(CRON_JOBS_PATH, "root"))
        for profile in CRON_PROFILE_NAMES:
            profile_path = os.path.join(HERMES_HOME, "profiles", profile, "cron", "jobs.json")
            out.extend(_read_one_cron_file(profile_path, profile))
        return out
    return safe(_inner, [], "read_hermes_cron")


def _cron_output_dir(profile):
    """Resolve a profile's cron output dir. 'root' = orchestrator's own
    ~/.hermes/cron/output; any other name = that profile's own dir."""
    if not profile or profile == "root":
        return os.path.join(HERMES_HOME, "cron", "output")
    return os.path.join(HERMES_HOME, "profiles", profile, "cron", "output")


def read_cron_latest_output(job_id, profile):
    """Latest output file for a job (cron writes one timestamped .md/.txt
    file per run under output/<job_id>/). Read-only; never trusts job_id/
    profile beyond basename validation (both come from a query string)."""
    if not job_id or not re.fullmatch(r"[a-zA-Z0-9_-]+", job_id):
        return {"ok": False, "error": "invalid job id"}
    if profile and not re.fullmatch(r"[a-zA-Z0-9_-]+", profile):
        return {"ok": False, "error": "invalid profile"}
    job_dir = os.path.join(_cron_output_dir(profile), job_id)
    if not os.path.isdir(job_dir):
        return {"ok": False, "error": "no output directory for this job yet (it may not have run)"}
    try:
        files = [f for f in os.listdir(job_dir) if os.path.isfile(os.path.join(job_dir, f))]
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if not files:
        return {"ok": False, "error": "no output files recorded for this job yet"}
    latest = max(files, key=lambda f: os.path.getmtime(os.path.join(job_dir, f)))
    try:
        with open(os.path.join(job_dir, latest), "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "filename": latest, "content": content}


def _cron_job_profile(job_id):
    """Which profile owns this job id ('root' or a specialist name), or
    None if unknown — looked up fresh each time so it always reflects the
    real, current job store rather than a cache that could drift."""
    for job in read_hermes_cron():
        if job.get("id") == job_id:
            return job.get("profile")
    return None


def _cron_job_ids():
    """Set of known job IDs, for validating action requests before we ever
    touch the real CLI."""
    return {job["id"] for job in read_hermes_cron() if job.get("id")}


def run_cron_action(action, job_id):
    """Validate action + id, then shell out to the real `hermes [-p <profile>]
    cron <action> <id>` (argv list, never shell=True), routed to whichever
    profile actually owns this job (root/orchestrator or a specialist).
    Returns (ok, error)."""
    if action not in CRON_ACTION_ALLOWED:
        return False, f"unknown action: {action}"
    if not job_id:
        return False, "id is required"
    profile = _cron_job_profile(job_id)
    if profile is None:
        return False, "unknown job id"

    # `hermes cron delete` aliases remove/rm; the CLI subcommand itself is
    # named "remove" — pass that through explicitly for clarity/robustness.
    cli_action = "remove" if action == "delete" else action
    argv = [HERMES_CLI_PATH]
    if profile != "root":
        argv += ["-p", profile]
    argv += ["cron", cli_action, job_id]

    env = dict(os.environ)
    env["TIRITH_ENABLED"] = "false"
    env["HERMES_HOME"] = HERMES_HOME  # see run_chat_send_stream for why this is forced

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=CRON_ACTION_SUBPROCESS_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "cron action timed out"
    except Exception as exc:
        _log_error("run_cron_action", exc)
        return False, "failed to run cron action"

    if proc.returncode != 0:
        err_tail = (proc.stderr or proc.stdout or "").strip()[-500:]
        return False, f"cron {cli_action} exited with code {proc.returncode}: {err_tail}"

    # A successful manual action (run/pause/resume/delete) from the dashboard
    # is itself operator-facing signal worth an info alert — distinct from
    # the background watcher's own "it ran on schedule" alerts below.
    add_alert("info", f"Schedule: {action} requested", f"job {job_id} \u00b7 profile {profile}")

    return True, None


# ---------------------------------------------------------------------------
# Cron watcher — background thread that diffs Hermes cron job state on a
# short poll and turns real transitions into alert-log entries (schedule
# task started / completed / failed). Read-only against jobs.json (same
# read_hermes_cron() the Schedule tab already uses) — this thread never
# writes cron state, it only observes it and writes to our own alerts.json.
# ---------------------------------------------------------------------------

_cron_watch_lock = threading.Lock()
_cron_watch_seen = {}  # job_id -> {"last_run_at": ..., "last_status": ...}


def _cron_watch_tick():
    """One poll: compare each job's current (last_run_at, last_status)
    against what we last saw. A last_run_at that just advanced means the
    job fired since our last poll — log a "ran" info alert, then a second
    alert if the resulting last_status is failed/delivery_failed so
    completion problems are distinguishable from a bare "it ran"."""
    jobs = read_hermes_cron()
    with _cron_watch_lock:
        for job in jobs:
            jid = job.get("id")
            if not jid:
                continue
            prev = _cron_watch_seen.get(jid)
            cur = {
                "last_run_at": job.get("last_run_at"),
                "last_status": job.get("last_status"),
            }
            if prev is not None and cur["last_run_at"] != prev.get("last_run_at") and cur["last_run_at"]:
                name = job.get("name") or "(unnamed job)"
                add_alert("info", f"Schedule: \u201c{name}\u201d ran", f"job {jid} \u00b7 profile {job.get('profile')}")
                status = cur["last_status"]
                if status == "failed" or job.get("last_error"):
                    add_alert("error", f"Schedule: \u201c{name}\u201d failed", job.get("last_error") or "last run failed")
                elif status == "delivery_failed":
                    add_alert("warning", f"Schedule: \u201c{name}\u201d delivery failed", job.get("last_delivery_error") or "delivery failed after the run completed")
                elif status == "ok":
                    add_alert("info", f"Schedule: \u201c{name}\u201d completed", f"next run {job.get('next_run_at') or 'not scheduled'}")
            _cron_watch_seen[jid] = cur


def cron_watch_loop():
    # Prime the "seen" state on first tick without alerting — otherwise every
    # job that already ran before this server started fires a false "just
    # ran" alert the moment the watcher boots.
    with _cron_watch_lock:
        for job in read_hermes_cron():
            jid = job.get("id")
            if jid:
                _cron_watch_seen[jid] = {
                    "last_run_at": job.get("last_run_at"),
                    "last_status": job.get("last_status"),
                }
    while True:
        time.sleep(CRON_WATCH_POLL_SECONDS)
        safe(_cron_watch_tick, None, "cron_watch_tick")


# ---------------------------------------------------------------------------
# Aggregated snapshot with a small in-memory cache.
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


def get_dashboard_version():
    """Live version string derived from git, never hand-edited/hardcoded.

    Uses the newest tag reachable from HEAD (git describe), falling back to
    a short commit hash if no tag exists yet. This is why the badge kept
    going stale before: it was a literal 'vN.N' string in index.html that
    nobody remembered to bump. Deriving it from git means it always matches
    reality without a manual step.
    """
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=PROJECT_DIR, capture_output=True, text=True, timeout=3,
        )
        v = out.stdout.strip()
        return v if v else "unknown"
    except Exception:
        return "unknown"


def build_snapshot():
    check_telegram_approvals()
    health = read_health()
    sessions = read_sessions()
    vps = read_vps()
    raw_logs = read_agent_logs_raw()
    fleet, models, model_usage, routing, agentlogs, agentlogs_stats = safe(
        lambda: build_fleet_models_routing_agentlogs(raw_logs),
        ({}, [], [], {"total": 0, "models": 0, "premium_calls": 0, "fast_calls": 0, "offload_pct": 0}, [], {"total": 0, "completed": 0, "failed": 0}),
        "build_fleet_models_routing_agentlogs",
    )
    board = board_list()

    # The template's applyState() expects fleet as an ARRAY (it mirrors the
    # shape of the old DEMO_STATE.fleet), not a dict keyed by agent name.
    # Keep AGENT_ORDER so cards render in the same stable order every time.
    eff_order = get_effective_agent_order()
    fleet_list = [fleet[name] for name in eff_order if name in fleet]

    return {
        "generated_at": iso_now(),
        "dashboard_version": get_dashboard_version(),
        "health": health,
        "sessions": sessions,
        "vps": vps,
        "fleet": fleet_list,
        "models": models,
        "model_usage": model_usage,
        "routing": routing,
        "agentlogs": agentlogs,
        "agentlogs_stats": agentlogs_stats,
        "board": board,
        "working_agents": get_working_agents(),
        "hermes_cron": read_hermes_cron(),
        "missions": missions_list(),
        "switchable_models": SWITCHABLE_MODELS,
        "alerts": read_alerts(10),
    }


# ---------------------------------------------------------------------------
# Builds tab — tracks Dev + its sub-agent Dev2. All 5 cards read straight
# from real Hermes ground truth (board.db, agent-logs.db, dev's own
# state.db messages table, and both agents' agent.log files) — nothing
# here is synthesized or cached beyond the tiny short-TTL cache below.
# ---------------------------------------------------------------------------

BUILDS_CACHE_TTL_SECONDS = 2.0
_builds_cache_lock = threading.Lock()
_builds_cache = {"ts": 0.0, "data": None}

_MESSAGE_AGENT_REPLY_RE = re.compile(r"^Message from .*?\(@(\w+)\):\s*(.*)$", re.DOTALL)
DEV_AGENT_LOG_PATH = os.path.join(HERMES_HOME, "profiles", "dev", "logs", "agent.log")
DEV2_AGENT_LOG_PATH = os.path.join(HERMES_HOME, "profiles", "dev2", "logs", "agent.log")

# Only these agent.log line shapes are operator-relevant; the rest (memory
# trims, mcp discovery chatter) would just be noise in a live-tail card.
_LOG_LEVEL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+(?P<level>[A-Z]+)\s+(?:\[[^\]]+\]\s+)?(?P<logger>[\w.]+):\s+(?P<msg>.*)$"
)
_LOG_INTERESTING_LOGGERS = (
    "agent.turn_context", "agent.conversation_loop", "agent.tool_executor", "gateway.run",
)


def _tail_lines(path, max_lines=400):
    """Read up to the last max_lines of a text file without loading the
    whole thing into memory for a multi-MB log — seek from the end in
    growing chunks until enough newlines are found, same approach `tail -n`
    uses. Missing file -> empty list, never raises."""
    if not os.path.exists(path):
        return []
    chunk = 8192
    data = b""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        pos = size
        while pos > 0 and data.count(b"\n") <= max_lines:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def _parse_agent_log_lines(lines, limit=60, since_epoch=None):
    """Filter raw agent.log lines down to the ones worth showing on a live
    log card (conversation turns, API calls, tool completions, gateway
    send events) and shape each into {ts, level, logger, text}. Silently
    skips lines that don't match the expected format (e.g. multi-line
    tracebacks) rather than guessing at their meaning. When `since_epoch`
    is given, lines timestamped before it are dropped — without this, an
    agent's own log tail happily shows every OLDER job mixed in with the
    new one every single poll, since the raw file has no concept of "job
    boundaries" on its own."""
    out = []
    for ln in lines:
        m = _LOG_LEVEL_RE.match(ln)
        if not m:
            continue
        logger = m.group("logger")
        if logger not in _LOG_INTERESTING_LOGGERS:
            continue
        if since_epoch is not None:
            line_epoch = _log_ts_to_epoch(m.group("ts"))
            if line_epoch is not None and line_epoch < since_epoch:
                continue
        out.append({
            "ts": m.group("ts"),
            "level": m.group("level"),
            "logger": logger,
            "text": m.group("msg").strip(),
        })
    return out[-limit:]


def _dev_active_session_id():
    """Resolve Dev's currently-relevant session id: prefer a session
    currently holding a live, unexpired turn lease (Dev is mid-turn RIGHT
    NOW on this session) over just picking the newest by started_at —
    cron-triggered sessions can have a more recent started_at than the
    actual live interactive session without Dev being active in them at
    this moment. Falls back to newest non-archived session if no lease is
    currently held (Dev fully idle)."""
    db_path = agent_state_db_path("dev")
    if not os.path.exists(db_path):
        return None
    conn = ro_connect(db_path)
    try:
        cur = conn.execute(
            "SELECT conversation_id FROM session_turn_leases WHERE expires_at > ? ORDER BY expires_at DESC LIMIT 1",
            (time.time(),),
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur = conn.execute("SELECT id FROM sessions WHERE archived=0 ORDER BY started_at DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _log_ts_to_epoch(ts_str):
    """Convert an agent.log timestamp string ('2026-09-18 17:48:56,270') to
    a unix epoch float for comparison against state.db's epoch timestamps.
    Returns None on any parse failure rather than raising, so a single
    malformed line never breaks filtering for the whole tail."""
    try:
        return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S,%f").timestamp()
    except Exception:
        return None


def _builds_current_mission():
    """Card 1: what Dev is actually working on right now. Ground truth is
    the most recent user instruction on Dev's currently-active session
    (same active-lease-first resolution as the transcript below) — that's
    the actual live job, not a stale board.db task that happens to still
    sit in the 'doing' column from days ago. Falls back to board.db's
    'doing' column, then the newest agent_logs.db row, only if there's no
    live session to read from at all."""
    def _inner():
        session_id = _dev_active_session_id()
        if session_id:
            db_path = agent_state_db_path("dev")
            conn = ro_connect(db_path)
            try:
                cur = conn.execute(
                    "SELECT id, content, timestamp FROM messages "
                    "WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
                    (session_id,),
                )
                row = cur.fetchone()
            finally:
                conn.close()
            if row and row[1]:
                text = re.sub(r"^\[[^\]]*\]\s*", "", row[1]).strip()  # strip the "[Display Name] " prefix
                return {
                    "source": "session",
                    "title": text[:220],
                    "status": "doing",
                    "updated_at": row[2],
                    "created_at": row[2],
                    "job_key": f"msg:{row[0]}",  # the actual "new job" signal: this message row id
                }
        board = board_list()
        doing = [t for t in board if (t.get("status") or "") == "running"]
        if doing:
            task = max(doing, key=lambda t: t.get("updated_at") or 0)
            return {
                "source": "board",
                "title": task.get("title"),
                "status": task.get("status"),
                "updated_at": task.get("updated_at"),
                "created_at": task.get("created_at"),
                "job_key": f"board:{task.get('id')}",
            }
        raw_logs = read_agent_logs_raw()
        dev_rows = [r for r in raw_logs if (r.get("agent_name") or "").lower() == "dev"]
        if dev_rows:
            newest = max(dev_rows, key=lambda r: r.get("created_at") or "")
            return {
                "source": "agent_logs",
                "title": newest.get("task_description"),
                "status": newest.get("status"),
                "updated_at": newest.get("created_at"),
                "created_at": newest.get("created_at"),
                "job_key": f"agentlog:{newest.get('id')}",
            }
        return None
    return safe(_inner, None, "_builds_current_mission")


def _builds_delegation_flow(since_ts=None, limit=20):
    """Card 2: the real Dev <-> Dev2 exchange history, read from Dev's own
    state.db messages table. Two distinct real shapes to detect, both
    ground-truth (not inferred):
      - Dev -> Dev2:  an assistant tool_calls entry naming message_agent
        with {"target": "dev2", "message": "..."}
      - Dev2 -> Dev:  a user-role message whose content starts with
        'Message from ... (@dev2): ...' (the bot_mode_dm reply-injection
        format Hermes itself uses)
    Returns them interleaved, oldest first, capped to `limit` most recent.
    When `since_ts` is given (the current job's start time), events from
    before it are dropped — otherwise every OLDER job's exchanges pile up
    in this card forever since it has no natural size limit of its own."""
    def _inner():
        db_path = agent_state_db_path("dev")
        if not os.path.exists(db_path):
            return []
        conn = ro_connect(db_path)
        try:
            cur = conn.execute(
                "SELECT id, role, content, tool_calls, timestamp FROM messages "
                "WHERE tool_calls LIKE '%message_agent%' OR content LIKE '%dev2%' "
                "ORDER BY id DESC LIMIT 400"
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        events = []
        for row in rows:
            _id, role, content, tool_calls_json, ts = row
            if since_ts is not None and (ts or 0) < since_ts:
                continue
            if role == "assistant" and tool_calls_json:
                try:
                    calls = json.loads(tool_calls_json)
                except Exception:
                    calls = []
                for call in calls:
                    fn = (call.get("function") or {})
                    if fn.get("name") != "message_agent":
                        continue
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    if args.get("target") != "dev2":
                        continue
                    events.append({
                        "direction": "dev_to_dev2",
                        "timestamp": ts,
                        "message": (args.get("message") or "")[:400],
                    })
            elif role == "user" and content:
                m = _MESSAGE_AGENT_REPLY_RE.match(content)
                if m and m.group(1) == "dev2":
                    events.append({
                        "direction": "dev2_to_dev",
                        "timestamp": ts,
                        "message": m.group(2).strip()[:400],
                    })
        events.sort(key=lambda e: e["timestamp"])
        return events[-limit:]
    return safe(_inner, [], "_builds_delegation_flow")


def _builds_agent_log_tail(path, limit=60, since_epoch=None):
    """Cards 3 & 4: live-tail one agent's own agent.log, filtered to the
    operator-relevant line shapes AND to the current job's time window
    (since_epoch) so an older job's lines never linger in the tail."""
    return safe(lambda: _parse_agent_log_lines(_tail_lines(path, max_lines=800), limit=limit, since_epoch=since_epoch), [], "_builds_agent_log_tail")


def _builds_dev_transcript(since_msg_id=None, limit=40):
    """Card 5: Dev's full recent tool-call transcript — the richest of the
    5 cards. Reads messages from Dev's OWN currently-active session in
    state.db: user turns, assistant reasoning/replies, tool calls (name +
    arguments), and tool results. This is intentionally the same
    ground-truth source Card 2 reads from, just unfiltered and
    session-scoped instead of message_agent-filtered.

    When `since_msg_id` is given (the current job's own triggering message
    row id), only messages from that row onward are returned — a session
    can span MANY jobs over hours/days (compression keeps it alive), so
    without this cutoff every earlier job's entire transcript stays mixed
    into this card forever and the list only grows."""
    def _inner():
        db_path = agent_state_db_path("dev")
        if not os.path.exists(db_path):
            return {"session_id": None, "entries": []}
        session_id = _dev_active_session_id()
        if not session_id:
            return {"session_id": None, "entries": []}
        conn = ro_connect(db_path)
        try:
            if since_msg_id is not None:
                cur = conn.execute(
                    "SELECT role, content, tool_calls, tool_name, timestamp FROM messages "
                    "WHERE session_id = ? AND id >= ? ORDER BY id DESC LIMIT ?",
                    (session_id, since_msg_id, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT role, content, tool_calls, tool_name, timestamp FROM messages "
                    "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                    (session_id, limit),
                )
            rows = cur.fetchall()
        finally:
            conn.close()
        entries = []
        for role, content, tool_calls_json, tool_name, ts in rows:
            if role == "assistant":
                if tool_calls_json:
                    try:
                        calls = json.loads(tool_calls_json)
                    except Exception:
                        calls = []
                    for call in calls:
                        fn = (call.get("function") or {})
                        entries.append({
                            "kind": "tool_call",
                            "label": fn.get("name") or "tool",
                            "text": (fn.get("arguments") or "")[:500],
                            "timestamp": ts,
                        })
                if content:
                    entries.append({
                        "kind": "assistant",
                        "label": "assistant",
                        "text": content[:500],
                        "timestamp": ts,
                    })
            elif role == "tool":
                entries.append({
                    "kind": "tool_result",
                    "label": tool_name or "result",
                    "text": (content or "")[:500],
                    "timestamp": ts,
                })
            elif role == "user" and content:
                entries.append({
                    "kind": "user",
                    "label": "instruction",
                    "text": content[:500],
                    "timestamp": ts,
                })
        entries.reverse()  # oldest first for a natural top-to-bottom transcript read
        return {"session_id": session_id, "entries": entries}
    return safe(_inner, {"session_id": None, "entries": []}, "_builds_dev_transcript")


def build_builds_snapshot():
    mission = _builds_current_mission()
    job_key = (mission or {}).get("job_key")

    # Derive the current job's cutoff for every other card so an older
    # job's data never lingers alongside the new one. Only a
    # session-sourced mission (source == "session") gives us BOTH a
    # precise message row id (for the transcript's exact SQL cutoff) and
    # a timestamp (for the delegation/log cards, which key off wall-clock
    # time instead of a row id). Board/agent_logs-sourced missions (the
    # rare fallback path, see _builds_current_mission) have neither, so
    # those cards fall back to their old unbounded-but-limited behavior —
    # acceptable since that path only fires when there's no live session
    # to scope anything to in the first place.
    since_msg_id = None
    since_ts = None
    if mission and mission.get("source") == "session" and job_key:
        try:
            since_msg_id = int(job_key.split(":", 1)[1])
        except (ValueError, IndexError):
            since_msg_id = None
        since_ts = mission.get("updated_at")

    return {
        "generated_at": iso_now(),
        "mission": mission,
        # frontend clears/resets all 5 cards when this key changes — anchored
        # to the actual message/task row that IS the job (job_key), not the
        # session id (which stays the same across many turns of one
        # conversation) and not just title text (which could repeat).
        "mission_key": job_key,
        "delegation": _builds_delegation_flow(since_ts=since_ts),
        "dev_log": _builds_agent_log_tail(DEV_AGENT_LOG_PATH, since_epoch=since_ts),
        "dev2_log": _builds_agent_log_tail(DEV2_AGENT_LOG_PATH, since_epoch=since_ts),
        "transcript": _builds_dev_transcript(since_msg_id=since_msg_id),
        "dev_op_state": _agent_op_state("dev", [r for r in read_agent_logs_raw() if (r.get("agent_name") or "").lower() == "dev"], get_working_agents()),
        "dev2_op_state": _agent_op_state("dev2", [r for r in read_agent_logs_raw() if (r.get("agent_name") or "").lower() == "dev2"], get_working_agents()),
    }


def get_builds_snapshot_cached():
    now = time.time()
    with _builds_cache_lock:
        if _builds_cache["data"] is not None and (now - _builds_cache["ts"]) < BUILDS_CACHE_TTL_SECONDS:
            return _builds_cache["data"]
    data = safe(build_builds_snapshot, {"error": "builds snapshot failed"}, "build_builds_snapshot")
    with _builds_cache_lock:
        _builds_cache["ts"] = time.time()
        _builds_cache["data"] = data
    return data


def get_snapshot_cached():
    now = time.time()
    with _cache_lock:
        if _cache["data"] is not None and (now - _cache["ts"]) < CACHE_TTL_SECONDS:
            return _cache["data"]
    # Build outside the lock so slow reads don't block other threads' cache checks
    # for longer than necessary; a tiny bit of duplicate work under a race is fine.
    data = safe(build_snapshot, {"error": "snapshot build failed"}, "build_snapshot")
    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return data


def invalidate_snapshot_cache():
    """Force the next /api/state (or SSE tick) to rebuild from scratch —
    used after a write action (board CRUD, cron action) so the change is
    visible immediately instead of waiting out the ~3s cache TTL."""
    with _cache_lock:
        _cache["ts"] = 0.0


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "MissionControl/1.0"
    # Transfer-Encoding: chunked (used by /api/chat/send to stream a live
    # agent turn) is an HTTP/1.1 mechanism — on the default HTTP/1.0,
    # browsers won't de-chunk the response and the raw chunk-size lines end
    # up as literal body text. Every other route here still sends an
    # explicit Content-Length, which remains valid under HTTP/1.1.
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        # Keep default stdlib logging but prefix for clarity.
        print(f"[http] {self.address_string()} - {fmt % args}")

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_html_file(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self._send_json({"error": "index.html not found"}, status=404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _read_raw_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # -- GET ---------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self._send_html_file(INDEX_HTML_PATH)
            return

        if path == "/template":
            self._send_html_file(TEMPLATE_HTML_PATH)
            return

        if path == "/api/state":
            self._send_json(get_snapshot_cached())
            return

        if path == "/api/builds":
            self._send_json(get_builds_snapshot_cached())
            return

        if path == "/api/token-spend":
            # Consolidated tokens/day (M) + est. USD; shared 5-min cache with the
            # Hermes dashboard token-spend plugin (~/token-spend/summary.py).
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "token_spend_summary", os.path.expanduser("~/token-spend/summary.py"))
                _mod = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                data = _mod.load(force=qs.get("refresh", ["0"])[0] in ("1", "true"))
                data.pop("history", None)
                self._send_json(data)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return

        if path == "/api/cron/output":
            job_id = (qs.get("id") or [""])[0]
            profile = (qs.get("profile") or ["root"])[0]
            self._send_json(read_cron_latest_output(job_id, profile))
            return

        if path == "/api/avatar":
            qs = parse_qs(urlparse(self.path).query)
            names = qs.get("agent") or []
            agent = (names[0] if names else "").lower()
            eff_avatars = get_effective_faos_agent_avatars()
            if agent in eff_avatars:
                asset_file = os.path.join(PROJECT_DIR, "assets", "agents", eff_avatars[agent])
                if os.path.isfile(asset_file):
                    try:
                        with open(asset_file, "rb") as f:
                            data = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "image/jpeg")
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    except OSError:
                        pass
            avatar_path = AGENT_AVATAR_PATHS.get(agent)
            if not avatar_path or not os.path.exists(avatar_path):
                self._send_json({"error": "avatar not found"}, status=404)
                return
            try:
                data = open(avatar_path, "rb").read()
            except OSError as e:
                self._send_json({"error": f"could not read avatar: {e}"}, status=500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/assets/agents/"):
            filename = os.path.basename(path)
            if not filename.lower().endswith(".jpg"):
                self._send_json({"error": "only .jpg served"}, status=403)
                return
            assets_dir = os.path.join(PROJECT_DIR, "assets", "agents")
            file_path = os.path.abspath(os.path.join(assets_dir, filename))
            if not file_path.startswith(os.path.abspath(assets_dir) + os.sep) or not os.path.isfile(file_path):
                self._send_json({"error": "not found"}, status=404)
                return
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
            except OSError as e:
                self._send_json({"error": f"could not read asset: {e}"}, status=500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(data)
            return

        if path.startswith("/apps/"):
            # Generic static-file server for standalone demo apps under apps/<name>/...
            # Path-traversal-safe: resolved path must stay inside apps_dir, and only a
            # small allow-list of static-asset extensions is ever served.
            apps_dir = os.path.join(PROJECT_DIR, "apps")
            rel = path[len("/apps/"):]
            file_path = os.path.abspath(os.path.join(apps_dir, rel))
            if not file_path.startswith(os.path.abspath(apps_dir) + os.sep) or not os.path.isfile(file_path):
                self._send_json({"error": "not found"}, status=404)
                return
            ext = os.path.splitext(file_path)[1].lower()
            content_types = {
                ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
                ".css": "text/css", ".js": "application/javascript",
                ".json": "application/json", ".svg": "image/svg+xml",
                ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".ico": "image/x-icon",
            }
            if ext not in content_types:
                self._send_json({"error": f"file type '{ext}' not served"}, status=403)
                return
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
            except OSError as e:
                self._send_json({"error": f"could not read file: {e}"}, status=500)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_types[ext])
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=300")
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/board":
            self._send_json({"tasks": board_list(), "columns": list(BOARD_COLUMNS)})
            return

        if path == "/api/board/task":
            task_id = (qs.get("id") or [None])[0]
            try:
                self._send_json(board_show(task_id))
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=404)
            except Exception as exc:
                _log_error("board_show", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/swdev/tasks":
            self._send_json({"tasks": swdev_list(), "columns": list(BOARD_COLUMNS)})
            return

        if path == "/api/swdev/task" or path.startswith("/api/swdev/task/"):
            task_id = path.rsplit("/", 1)[1] if path.startswith("/api/swdev/task/") else (qs.get("id") or [None])[0]
            try:
                self._send_json(swdev_show(task_id))
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=404)
            except Exception as exc:
                _log_error("swdev_show", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/workers" or path.startswith("/api/workers/"):
            status, body = faos_proxy_request("GET", path + (("?" + parsed.query) if parsed.query else ""))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/faos/audit-log":
            n = 50
            try:
                n = max(1, min(int((qs.get("limit") or ["50"])[0]), 500))
            except Exception:
                pass
            self._send_json({"rows": read_faos_audit_log(n)})
            return

        if path == "/api/faos/rings":
            self._send_json(faos_rings_payload())
            return

        if path == "/api/faos/ryzen":
            self._send_json(get_ryzen_fleet_payload())
            return

        if path == "/api/jv/diagnostics":
            self._send_json(jv_diagnostics.get_jv_diagnostics_payload())
            return

        if path == "/api/jv/nudges":
            import jv_watcher
            self._send_json(jv_watcher.get_watcher_status())
            return

        if path in ("/api/jv/watcher", "/api/jv/proactive"):
            self._send_json(jv_watcher.get_watcher_status())
            return

        if path in ("/api/jv/voice/status", "/api/jv/status"):
            self._send_json({
                "ok": True,
                "available": jv_voice.available(),
                "stt": "elevenlabs" if jv_voice.available() else "browser",
                "tts": "elevenlabs" if jv_voice.available() else "browser",
                "voice_id": jv_voice.voice_id() if jv_voice.available() else None,
            })
            return

        if path == "/api/dev/self-heal/status":
            enabled, reason = dev_self_heal.is_self_heal_enabled()
            history = dev_self_heal.SafetySandbox.load_history()
            applied_count = sum(1 for p in history if p.get("status") == "applied")
            self._send_json({
                "ok": True,
                "enabled": enabled,
                "switch_reason": reason,
                "total_patches": len(history),
                "active_applied_patches": applied_count,
                "crash_signal_present": os.path.exists(dev_self_heal.CRASH_SIGNAL_FILE),
            })
            return

        if path == "/api/dev/self-heal/history":
            self._send_json({
                "ok": True,
                "history": dev_self_heal.SafetySandbox.load_history(),
            })
            return

        if path == "/api/faos/ryzen/dispatch":
            run_id = (qs.get("run_id") or [None])[0]
            if run_id:
                item = poll_single_ryzen_dispatch(run_id)
                if item:
                    self._send_json({"ok": True, "dispatch": item})
                else:
                    self._send_json({"ok": False, "error": "dispatch not found"}, status=404)
            else:
                dispatches = read_ryzen_dispatches()
                dispatches = update_pending_ryzen_dispatches(dispatches)
                self._send_json({"ok": True, "dispatches": dispatches})
            return

        if path == "/api/alerts":
            n = 10
            try:
                n = max(1, min(int((qs.get("limit") or ["10"])[0]), ALERTS_MAX_STORED))
            except Exception:
                pass
            self._send_json({"alerts": read_alerts(n)})
            return

        if path == "/api/agents/secondary":
            rows = secondary_agents.list_secondary_agents()
            self._send_json({"ok": True, "agents": rows, "rows": rows})
            return

        if path == "/api/agent":
            key = (qs.get("key") or [None])[0]
            detail = safe(lambda: read_agent_detail(key), None, "read_agent_detail")
            if detail is None:
                self._send_json({"error": "agent not found"}, status=404)
            else:
                self._send_json(detail)
            return

        if path == "/api/chat/history":
            agent = (qs.get("agent") or [None])[0]
            result = read_chat_history(agent)
            if result is None:
                self._send_json({"error": "unknown agent"}, status=400)
            else:
                self._send_json({"telegram": result.get("telegram", False), "messages": result.get("messages", [])})
            return

        if path == "/api/content":
            docs = list_content_docs()
            self._send_json(docs)
            return

        if path == "/api/content/read":
            agent = (qs.get("agent") or [None])[0]
            filename = (qs.get("file") or [None])[0]
            try:
                text = read_content_doc(agent, filename)
                self._send_json({"text": text})
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=400)
            except FileNotFoundError:
                self._send_json({"error": "not found"}, status=404)
            except Exception as exc:
                _log_error("api_content_read", exc)
                self._send_json({"error": "internal error"}, status=500)
            return

        if path == "/upload":
            body = UPLOAD_PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/events":
            self._handle_sse()
            return

        self._send_json({"error": "not found"}, status=404)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                data = get_snapshot_cached()
                payload = json.dumps(data)
                chunk = f"event: state\ndata: {payload}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            _log_error("sse_stream", exc)
            return

    # -- POST ----------------------------------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/agents/secondary":
            body = self._read_json_body()
            if not body or not isinstance(body, dict):
                self._send_json({"ok": False, "error": "invalid JSON body"}, status=400)
                return
            p_name = body.get("profile_name")
            d_name = body.get("display_name")
            p_role = body.get("primary_role")
            s_roles = body.get("secondary_roles") or []
            src = body.get("source_profile")
            try:
                created = secondary_agents.create_secondary_agent(
                    profile_name=p_name,
                    display_name=d_name,
                    primary_role=p_role,
                    secondary_roles=s_roles,
                    source_profile=src
                )
                self._send_json({"ok": True, "agent": created, **created})
            except ValueError as ve:
                self._send_json({"ok": False, "error": str(ve)}, status=400)
            except Exception as exc:
                _log_error("create_secondary_agent", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path.startswith("/api/agents/secondary/") and (path.endswith("/delete") or qs.get("action") == ["delete"]):
            p_end = path[len("/api/agents/secondary/"): ]
            if p_end.endswith("/delete"):
                p_end = p_end[:-len("/delete")]
            profile_name = p_end.strip().lower()
            if profile_name in secondary_agents.PROTECTED_AGENTS:
                self._send_json({"ok": False, "error": f"Agent '{profile_name}' is protected and cannot be deleted"}, status=400)
                return
            try:
                ok = secondary_agents.delete_secondary_agent(profile_name)
                self._send_json({"ok": True, "deleted": profile_name})
            except Exception as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path.startswith("/api/workers/"):
            body_bytes = self._read_raw_body()
            status, body = faos_proxy_request("POST", path, body_bytes if body_bytes else b"{}")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/cron/action":
            body = self._read_json_body() if self.headers.get("Content-Length") else {}
            action = (qs.get("action") or [body.get("action")])[0]
            job_id = (qs.get("id") or [body.get("id") or body.get("job_id")])[0]
            try:
                ok, error = run_cron_action(action, job_id)
                if ok:
                    invalidate_snapshot_cache()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": error}, status=400)
            except Exception as exc:
                _log_error("api_cron_action", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/kanban/action":
            body = self._read_json_body() if self.headers.get("Content-Length") else {}
            action = (qs.get("action") or [body.get("action")])[0] or "run"
            task_id = (qs.get("id") or [body.get("task_id") or body.get("id")])[0]
            try:
                ok, error = run_kanban_task_action(action, task_id)
                if ok:
                    invalidate_snapshot_cache()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": error}, status=400)
            except Exception as exc:
                _log_error("api_kanban_action", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/mission/assign":
            body = self._read_json_body()
            board_task_id = (body.get("board_task_id") or "").strip()
            title = (body.get("title") or "").strip()
            notes = (body.get("notes") or "").strip()
            if not board_task_id or not title:
                self._send_json({"ok": False, "error": "board_task_id and title are required"}, status=400)
                return
            existing = missions_get_by_task(board_task_id)
            if existing and existing["status"] != "done":
                self._send_json({"ok": False, "error": "this task already has an active mission", "mission": existing}, status=409)
                return
            if not TELEGRAM_BOT_TOKEN or not TELEGRAM_GROUP_CHAT_ID:
                self._send_json({"ok": False, "error": "Telegram not configured on this server (TELEGRAM_BOT_TOKEN / TELEGRAM_GROUP_ALLOWED_CHATS)"}, status=500)
                return
            try:
                mission_id = str(uuid.uuid4())
                thread_id = create_mission_telegram_topic(title)
                note_path = _obsidian_mission_note_path(title, mission_id)
                _write_mission_prd_note(note_path, title, mission_id, board_task_id, thread_id)
                missions_create_row(mission_id, board_task_id, title, thread_id, TELEGRAM_GROUP_CHAT_ID, note_path)
                send_mission_brief(thread_id, title, notes, note_path)
                invalidate_snapshot_cache()
                self._send_json({"ok": True, "mission_id": mission_id, "telegram_thread_id": thread_id})
            except Exception as exc:
                _log_error("api_mission_assign", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/mission/approve":
            body = self._read_json_body()
            mission_id = (body.get("mission_id") or "").strip()
            if not mission_id:
                self._send_json({"ok": False, "error": "mission_id is required"}, status=400)
                return
            try:
                ok = approve_mission(mission_id, via="dashboard")
                if ok:
                    invalidate_snapshot_cache()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": "mission not found or not pending approval"}, status=400)
            except Exception as exc:
                _log_error("api_mission_approve", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/faos/ryzen/dispatch":
            body = self._read_json_body()
            target = (body.get("target") or "ryzen-jv").strip()
            task_text = (body.get("input") or body.get("task") or "").strip()
            if not task_text:
                self._send_json({"ok": False, "error": "input/task text is required"}, status=400)
                return
            try:
                result = dispatch_task_to_ryzen(target, task_text)
                if result.get("ok"):
                    self._send_json(result)
                else:
                    self._send_json(result, status=500)
            except Exception as exc:
                _log_error("api_faos_ryzen_dispatch", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/agent/model":
            body = self._read_json_body()
            agent = (body.get("agent") or "").strip()
            model_id = (body.get("model_id") or "").strip()
            if not agent or not model_id:
                self._send_json({"ok": False, "error": "agent and model_id are required"}, status=400)
                return
            try:
                ok, error = switch_agent_model(agent, model_id)
                if ok:
                    invalidate_snapshot_cache()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": False, "error": error}, status=400)
            except Exception as exc:
                _log_error("api_agent_model", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/content/save":
            agent = (qs.get("agent") or [None])[0]
            filename = (qs.get("file") or [None])[0]
            try:
                raw = self._read_raw_body()
                text = raw.decode("utf-8", errors="replace")
                save_content_doc(agent, filename, text)
                self._send_json({"ok": True, "agent": agent, "filename": filename})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("api_content_save", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/content/delete":
            agent = (qs.get("agent") or [None])[0]
            filename = (qs.get("file") or [None])[0]
            try:
                delete_content_doc(agent, filename)
                self._send_json({"ok": True})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except FileNotFoundError:
                self._send_json({"ok": False, "error": "not found"}, status=404)
            except Exception as exc:
                _log_error("api_content_delete", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/chat/send":
            payload = self._read_json_body()
            agent = payload.get("agent")
            text = (payload.get("text") or "").strip()

            if agent not in get_effective_chat_allowed_agents():
                self._send_json({"ok": False, "error": "unknown agent"}, status=400)
                return
            if not text:
                self._send_json({"ok": False, "error": "text is required"}, status=400)
                return

            # Stream the CLI's stdout straight through as it arrives, using
            # chunked transfer so the browser sees output incrementally
            # rather than waiting for the whole (up to 180s) turn to finish.
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

            def _write_chunk(text_chunk):
                if not text_chunk:
                    return
                data = text_chunk.encode("utf-8")
                try:
                    self.wfile.write(f"{len(data):x}\r\n".encode("ascii"))
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    raise

            try:
                for item in run_chat_send_stream(agent, text):
                    if isinstance(item, tuple):
                        kind, value = item
                        if kind == "__error__":
                            _write_chunk(f"\n[error] {value}\n")
                        # "__done__" carries the full meta-stripped reply, already
                        # streamed line-by-line above — nothing left to send.
                    else:
                        _write_chunk(item)
                # terminate the chunked stream
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                _log_error("api_chat_send", exc)
                try:
                    self.wfile.write(b"0\r\n\r\n")
                except Exception:
                    pass
            return

        if path == "/api/chat/speak":
            payload = self._read_json_body()
            text = (payload.get("text") or "").strip()
            agent = (payload.get("agent") or "dev").lower()
            voice = payload.get("voice") or AGENT_PIPER_VOICES.get(agent, "female")

            if not text:
                self._send_json({"ok": False, "error": "text is required"}, status=400)
                return

            try:
                wav_bytes = synthesize_piper_wav(text, voice_name=voice)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav_bytes)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(wav_bytes)
            except Exception as exc:
                _log_error("api_chat_speak", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path in ("/api/jv/speak", "/api/speak"):
            ctype = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
            if ctype == "application/json":
                payload = self._read_json_body()
                text = (payload.get("text") or "").strip()
            else:
                raw = self._read_raw_body()
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                    text = (payload.get("text") or "").strip()
                except Exception:
                    text = ""

            if not text:
                self._send_json({"ok": False, "error": "text is required"}, status=400)
                return

            try:
                self.send_response(200)
                self.send_header("Content-Type", "audio/mpeg")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                for chunk in jv_voice.speak_stream(text):
                    if chunk:
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii") + chunk + b"\r\n")
                        self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as exc:
                _log_error("api_jv_speak", exc)
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except Exception:
                    pass
            return

        if path in ("/api/jv/listen", "/api/listen"):
            ctype = self.headers.get("Content-Type", "audio/webm")
            raw_audio = self._read_raw_body()
            if not raw_audio:
                self._send_json({"ok": False, "error": "empty audio body", "text": ""}, status=400)
                return

            try:
                transcript = jv_voice.transcribe(raw_audio, mime=ctype)
                self._send_json({"ok": True, "text": transcript})
            except Exception as exc:
                _log_error("api_jv_listen", exc)
                self._send_json({"ok": False, "error": str(exc)[:300], "text": ""}, status=503)
            return

        if path in ("/api/jv/watcher/step", "/api/jv/watcher/trigger", "/api/jv/proactive/trigger"):
            try:
                daemon = jv_watcher.WatcherDaemon()
                res = daemon.tick(force_jev=True)
                self._send_json({"ok": True, "result": res})
            except Exception as exc:
                _log_error("api_jv_watcher_trigger", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/dev/self-heal/toggle":
            try:
                body = self._read_json_body()
                enabled = body.get("enabled", True)
                res = dev_self_heal.set_self_heal_enabled(enabled)
                self._send_json({"ok": True, "config": res})
            except Exception as exc:
                _log_error("api_dev_self_heal_toggle", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/dev/self-heal/rollback":
            try:
                body = self._read_json_body()
                patch_id = body.get("patch_id", "")
                ok, msg = dev_self_heal.SafetySandbox.rollback_patch(patch_id)
                self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            except Exception as exc:
                _log_error("api_dev_self_heal_rollback", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/dev/self-heal/heal":
            try:
                body = self._read_json_body()
                tb = body.get("traceback", "")
                err = body.get("error", "RuntimeError")
                topic = body.get("topic_id", 7505)
                res = dev_self_heal.AutoHealEngine.heal_traceback(tb, error_msg=err, topic_id=topic)
                self._send_json(res, status=200 if res.get("success") else 400)
            except Exception as exc:
                _log_error("api_dev_self_heal_heal", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return

        if path == "/api/upload":
            try:
                content_type = self.headers.get("Content-Type", "")
                if "multipart/form-data" not in content_type:
                    self._send_json({"ok": False, "error": "expected multipart/form-data"}, status=400)
                    return

                form = cgi.FieldStorage(
                    fp=self.rfile,
                    headers=self.headers,
                    environ={
                        "REQUEST_METHOD": "POST",
                        "CONTENT_TYPE": content_type,
                        "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                    },
                )

                if "file" not in form:
                    self._send_json({"ok": False, "error": "no 'file' field in upload"}, status=400)
                    return

                field = form["file"]
                if not getattr(field, "filename", None):
                    self._send_json({"ok": False, "error": "no filename provided"}, status=400)
                    return

                data_bytes = field.file.read() if hasattr(field, "file") else field.value
                safe_name, dest_path = save_uploaded_file(field.filename, data_bytes)
                self._send_json({"ok": True, "filename": safe_name, "path": dest_path})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("api_upload", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/board":
            payload = self._read_json_body()
            try:
                task_id = board_create(payload)
                self._send_json({"ok": True, "id": task_id})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("board_create", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/board/update":
            task_id = (qs.get("id") or [None])[0]
            payload = self._read_json_body()
            try:
                updated = board_update(task_id, payload)
                self._send_json({"ok": updated})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("board_update", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path in ("/api/board/comment", "/api/board/assign", "/api/board/block", "/api/board/unblock"):
            try:
                payload = self._read_json_body()
                payload["id"] = (qs.get("id") or [payload.get("id")])[0]
                self._send_json(board_action(path.rsplit("/", 1)[1], payload))
            except (ValueError, RuntimeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("board_action", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/board/delete":
            task_id = (qs.get("id") or [None])[0]
            try:
                deleted = board_delete(task_id)
                self._send_json({"ok": deleted})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("board_delete", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/swdev/create":
            payload = self._read_json_body()
            try:
                self._send_json({"ok": True, **swdev_create(payload)})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("swdev_create", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/swdev/move":
            payload = self._read_json_body()
            try:
                self._send_json({"ok": True, **swdev_move(payload)})
            except PermissionError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=403)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("swdev_move", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        if path == "/api/swdev/comment":
            payload = self._read_json_body()
            try:
                self._send_json({"ok": True, **swdev_comment(payload)})
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
            except Exception as exc:
                _log_error("swdev_comment", exc)
                self._send_json({"ok": False, "error": "internal error"}, status=500)
            return

        self._send_json({"error": "not found"}, status=404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/agents/secondary/"):
            profile_name = path[len("/api/agents/secondary/"): ].strip().lower()
            if profile_name in secondary_agents.PROTECTED_AGENTS:
                self._send_json({"ok": False, "error": f"Agent '{profile_name}' is protected and cannot be deleted"}, status=400)
                return
            try:
                ok = secondary_agents.delete_secondary_agent(profile_name)
                if ok:
                    self._send_json({"ok": True, "deleted": profile_name})
                else:
                    self._send_json({"ok": False, "error": "agent not found"}, status=404)
            except PermissionError as pe:
                self._send_json({"ok": False, "error": str(pe)}, status=400)
            except Exception as exc:
                _log_error("delete_secondary_agent", exc)
                self._send_json({"ok": False, "error": str(exc)}, status=500)
            return
        self._send_json({"error": "not found"}, status=404)


def main():
    board_init()
    missions_init()
    try:
        dev_self_heal.BootSentry.check_and_recover()
    except Exception as exc:
        _log_error("boot_sentry_startup", exc)
    add_alert("info", "Mission Control dashboard started", f"serving on http://{HOST}:{PORT}")
    watcher = threading.Thread(target=cron_watch_loop, daemon=True)
    watcher.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[mission-control] serving on http://{HOST}:{PORT} (Hermes home: {HERMES_HOME})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
