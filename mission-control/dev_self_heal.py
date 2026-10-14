#!/usr/bin/env python3
"""
FAOS Phase D: Fleet Agent Self-Healing Engine (Pilot on Dev Profile Only)

Ported from Brahma Echo's real mechanism (actions/auto_heal_engine.py, core/boot_sentry.py)
Adapted with owner-locked requirements (2026-10-04):
  1. Patch-then-notify, not silent (immediate Telegram alert with diff + explanation)
  2. Stricter protected-files list (.env, SOUL.md, secrets, credentials, tokens, own source)
  3. Dedicated instant ON/OFF switch (env var DEV_SELF_HEAL_ENABLED + config JSON)
  4. Zero-brick guarantee: AST parse full-file validation + timestamped .bak backups +
     BootSentry auto-rollback on crash signal
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths & Environment
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups", "self_heal")

CONFIG_FILE = os.path.join(DATA_DIR, "dev_self_heal_config.json")
PATCH_HISTORY_FILE = os.path.join(DATA_DIR, "dev_patch_history.json")
CRASH_SIGNAL_FILE = os.path.join(DATA_DIR, "dev_crash_signal.log")
HERMES_ENV_PATH = os.path.expanduser("~/.hermes/.env")

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Stricter Protected Files (Owner-Locked)
# ---------------------------------------------------------------------------
# Hardcoded set of files that must NEVER be modified by self-healing under any circumstances.
PROTECTED_FILENAMES = {
    "soul.md",
    "dev_self_heal.py",
    "config.yaml",
    "kanban.db",
}

PROTECTED_KEYWORDS = [
    "secret",
    "credential",
    "token",
    "password",
    "api_key",
    ".env",
]


def is_file_protected(file_path: str) -> Tuple[bool, str]:
    """
    Checks whether a file path is strictly protected against self-healing modifications.
    Returns (is_protected, refusal_reason).
    """
    if not file_path:
        return True, "Empty file path"

    norm_path = os.path.realpath(os.path.expanduser(file_path))
    base_name = os.path.basename(norm_path)
    lower_path = norm_path.lower()
    lower_base = base_name.lower()

    # 1. Any .env file anywhere
    if (
        lower_base == ".env"
        or lower_base.startswith(".env.")
        or lower_base.endswith(".env")
        or "/.env" in lower_path
    ):
        return True, f"Explicitly protected: .env files cannot be modified ({base_name})"

    # 2. SOUL.md anywhere
    if lower_base == "soul.md":
        return True, "Explicitly protected: SOUL.md cannot be modified by self-healing"

    # 3. This module's own source file (prevents disabling safety checks)
    own_file = os.path.realpath(__file__)
    if norm_path == own_file or lower_base == "dev_self_heal.py":
        return True, "Explicitly protected: dev_self_heal.py cannot modify its own source"

    # 4. Core protected filenames
    if lower_base in PROTECTED_FILENAMES:
        return True, f"Explicitly protected: {base_name} is in hardcoded protected files list"

    # 5. Sensitive keywords anywhere in path
    for kw in PROTECTED_KEYWORDS:
        if kw in lower_path:
            return True, f"Explicitly protected: path contains sensitive keyword '{kw}'"

    return False, ""


# ---------------------------------------------------------------------------
# ON / OFF Kill-Switch
# ---------------------------------------------------------------------------
def is_self_heal_enabled() -> Tuple[bool, str]:
    """
    Evaluates the ON/OFF switch. Checked at the very top of all healing logic.
    Returns (is_enabled, reason).
    """
    # Check environment variable first
    env_val = os.environ.get("DEV_SELF_HEAL_ENABLED", "").strip().lower()
    if env_val in ("0", "false", "no", "off", "disable", "disabled"):
        return False, "Self-healing disabled via DEV_SELF_HEAL_ENABLED environment variable"

    # Check config file
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not data.get("enabled", True):
                    return False, f"Self-healing disabled via config file {CONFIG_FILE}"
        except Exception as exc:
            # Fail closed for safety
            return False, f"Config read failure ({exc}); failing closed for safety"

    return True, "Self-healing enabled"


def set_self_heal_enabled(enabled: bool) -> dict:
    """Sets the persistent enabled state in dev_self_heal_config.json."""
    payload = {
        "enabled": bool(enabled),
        "updated_at": time.time(),
        "updated_at_iso": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload


# ---------------------------------------------------------------------------
# Telegram Notification Helper
# ---------------------------------------------------------------------------
def _load_hermes_env() -> Dict[str, str]:
    """Reads ~/.hermes/.env stdlib-only."""
    vals: Dict[str, str] = {}
    if os.path.exists(HERMES_ENV_PATH):
        try:
            with open(HERMES_ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    vals[k.strip()] = v.strip().strip('"').strip("'")
        except Exception:
            pass
    return vals


def send_telegram_notification(text: str, topic_id: Optional[int] = None) -> Tuple[bool, str]:
    """
    Posts diff + explanation to Telegram via Telegram Bot API.
    Primary target: TELEGRAM_HOME_CHANNEL (Ngom direct, e.g. 510041509)
    Secondary target: group chat thread (e.g. topic 7505 on -1003940830491)
    """
    env = _load_hermes_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False, "TELEGRAM_BOT_TOKEN not configured in env or ~/.hermes/.env"

    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL") or env.get("TELEGRAM_HOME_CHANNEL") or "510041509"
    group_chat_id = os.environ.get("TELEGRAM_GROUP_CHAT_ID") or "-1003940830491"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    successes = []
    errors = []

    # Attempt 1: Direct home channel (Ngom's direct notification bot channel)
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            res = json.loads(resp.read().decode("utf-8"))
            if res.get("ok"):
                successes.append(f"direct:{chat_id}")
            else:
                errors.append(f"direct:{res}")
    except Exception as exc:
        errors.append(f"direct_exc:{exc}")

    # Attempt 2: If topic_id given (e.g. FAOS topic 7505), mirror to group thread
    if topic_id:
        try:
            payload = {
                "chat_id": group_chat_id,
                "message_thread_id": topic_id,
                "text": text,
                "parse_mode": "HTML",
            }
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                res = json.loads(resp.read().decode("utf-8"))
                if res.get("ok"):
                    successes.append(f"thread:{topic_id}")
        except Exception as exc:
            errors.append(f"thread_exc:{exc}")

    if successes:
        return True, f"Delivered to {', '.join(successes)}"
    return False, f"Failed to deliver: {'; '.join(errors)}"


# ---------------------------------------------------------------------------
# TracebackAnalyzer (Ported from Brahma Echo)
# ---------------------------------------------------------------------------
class TracebackAnalyzer:
    """
    Parses exception tracebacks innermost-first.
    Skips third-party, venv, and system frames.
    Enforces protected-files policy.
    """
    FRAME_REGEX = re.compile(r'File\s+"(?P<file>.+?)",\s+line\s+(?P<line>\d+)(?:,\s+in\s+(?P<func>.+))?')

    @classmethod
    def parse(cls, traceback_str: str) -> dict:
        matches = list(cls.FRAME_REGEX.finditer(traceback_str))
        if not matches:
            return {
                "status": "no_valid_frame",
                "file": None,
                "line": None,
                "func": None,
                "reason": "No 'File \"...\", line N' frames found in traceback",
            }

        # Walk innermost-first
        for m in reversed(matches):
            raw_file = m.group("file")
            line_no = int(m.group("line"))
            func_name = (m.group("func") or "").strip()

            lower_raw = raw_file.lower()
            if any(skip in lower_raw for skip in [
                "/site-packages/",
                "/.venv/",
                "/venv/",
                "/dist-packages/",
                "/lib/python",
                "<frozen ",
                "<string>",
                "<stdin>",
            ]):
                continue

            resolved = os.path.realpath(os.path.expanduser(raw_file))

            # Check protected files immediately
            is_prot, prot_reason = is_file_protected(resolved)
            if is_prot:
                return {
                    "status": "protected_refusal",
                    "file": resolved,
                    "line": line_no,
                    "func": func_name,
                    "reason": f"PROTECTED_FILE_REFUSAL: {prot_reason}",
                }

            if not os.path.isfile(resolved):
                continue

            return {
                "status": "ok",
                "file": resolved,
                "line": line_no,
                "func": func_name,
                "reason": None,
            }

        return {
            "status": "no_valid_frame",
            "file": None,
            "line": None,
            "func": None,
            "reason": "All frames were third-party, virtualenv, or non-existent files",
        }


# ---------------------------------------------------------------------------
# SafetySandbox (Ported from Brahma Echo)
# ---------------------------------------------------------------------------
class SafetySandbox:
    """
    Manages timestamped backups, AST full-file validation, patch history, and rollbacks.
    """

    @staticmethod
    def create_backup(target_file: str, patch_id: str) -> str:
        """Creates both an adjacent .bak and a centralized backup in backups/self_heal/."""
        ts = int(time.time())
        base = os.path.basename(target_file)
        central_backup = os.path.join(BACKUPS_DIR, f"{base}.bak_{ts}_{patch_id}")
        shutil.copy2(target_file, central_backup)

        # Also create adjacent .bak for convenience
        adjacent_backup = f"{target_file}.bak_{ts}_{patch_id}"
        try:
            shutil.copy2(target_file, adjacent_backup)
        except Exception:
            pass
        return central_backup

    @staticmethod
    def validate_code(full_source: str) -> Tuple[bool, str]:
        """
        Validates the candidate FULL staged source using ast.parse().
        Any syntax error aborts the patch before anything is written to disk.
        """
        try:
            ast.parse(full_source)
            return True, ""
        except SyntaxError as e:
            return False, f"SyntaxError at line {e.lineno}, col {e.offset}: {e.msg}"
        except Exception as e:
            return False, f"AST validation error: {e}"

    @staticmethod
    def load_history() -> List[dict]:
        if not os.path.exists(PATCH_HISTORY_FILE):
            return []
        try:
            with open(PATCH_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    @classmethod
    def log_patch(cls, entry: dict) -> None:
        history = cls.load_history()
        history.append(entry)
        tmp_file = f"{PATCH_HISTORY_FILE}.tmp_{os.getpid()}"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp_file, PATCH_HISTORY_FILE)

    @classmethod
    def update_patch_status(cls, patch_id: str, new_status: str, extra: Optional[dict] = None) -> bool:
        history = cls.load_history()
        updated = False
        for item in history:
            if item.get("patch_id") == patch_id:
                item["status"] = new_status
                if extra:
                    item.update(extra)
                updated = True
                break
        if updated:
            tmp_file = f"{PATCH_HISTORY_FILE}.tmp_{os.getpid()}"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            os.replace(tmp_file, PATCH_HISTORY_FILE)
        return updated

    @classmethod
    def rollback_patch(cls, patch_id: str) -> Tuple[bool, str]:
        """Restores file from backup for a given patch id."""
        history = cls.load_history()
        target_entry = None
        for item in reversed(history):
            if item.get("patch_id") == patch_id:
                target_entry = item
                break

        if not target_entry:
            return False, f"Patch ID '{patch_id}' not found in history"

        backup_path = target_entry.get("backup_path")
        target_file = target_entry.get("target_file")

        if not backup_path or not os.path.exists(backup_path):
            return False, f"Backup file '{backup_path}' does not exist on disk"
        if not target_file:
            return False, "Target file path missing from patch entry"

        try:
            shutil.copy2(backup_path, target_file)
            cls.update_patch_status(
                patch_id,
                "rolled_back",
                {"rolled_back_at": time.time(), "rolled_back_at_iso": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())},
            )
            return True, f"Successfully restored {target_file} from {backup_path}"
        except Exception as exc:
            return False, f"Rollback error copying backup: {exc}"


# ---------------------------------------------------------------------------
# Boot Sentry (Zero-Brick Guarantee)
# ---------------------------------------------------------------------------
class BootSentry:
    """
    Checks for unhandled crash signals on startup.
    If a crash signal exists AND the last patch is 'applied', auto-restores the .bak
    and marks it 'rolled_back_on_boot' before normal operations resume.
    """

    @staticmethod
    def record_crash_signal(error_info: str) -> None:
        with open(CRASH_SIGNAL_FILE, "w", encoding="utf-8") as f:
            f.write(f"CRASH_TIME: {time.time()}\nDETAILS:\n{error_info}\n")

    @staticmethod
    def clear_crash_signal() -> None:
        if os.path.exists(CRASH_SIGNAL_FILE):
            try:
                os.remove(CRASH_SIGNAL_FILE)
            except Exception:
                pass

    @classmethod
    def check_and_recover(cls) -> dict:
        if not os.path.exists(CRASH_SIGNAL_FILE):
            return {"status": "healthy", "action": "none", "message": "No crash signal found"}

        history = SafetySandbox.load_history()
        last_applied = None
        for item in reversed(history):
            if item.get("status") == "applied":
                last_applied = item
                break

        if not last_applied:
            cls.clear_crash_signal()
            return {"status": "healthy", "action": "cleared_stale_signal", "message": "Crash signal present but no active un-reverted patch found"}

        patch_id = last_applied["patch_id"]
        target_file = last_applied["target_file"]
        backup_path = last_applied.get("backup_path")

        if backup_path and os.path.exists(backup_path):
            shutil.copy2(backup_path, target_file)
            SafetySandbox.update_patch_status(
                patch_id,
                "rolled_back_on_boot",
                {"rolled_back_on_boot_at": time.time()},
            )
            cls.clear_crash_signal()

            # Notify owner of boot rollback
            notify_msg = (
                f"<b>[BOOT SENTRY AUTO-RECOVERY]</b>\n"
                f"A fatal crash signal was detected following patch <code>{patch_id}</code>.\n"
                f"<b>Target File:</b> <code>{target_file}</code>\n"
                f"<b>Action:</b> Zero-brick guarantee triggered. Automatically restored from backup: <code>{backup_path}</code>.\n"
                f"Status updated to <code>rolled_back_on_boot</code>."
            )
            send_telegram_notification(notify_msg, topic_id=7505)

            return {
                "status": "recovered",
                "action": "rolled_back_on_boot",
                "patch_id": patch_id,
                "target_file": target_file,
                "backup_path": backup_path,
            }

        return {
            "status": "warning",
            "action": "backup_missing",
            "patch_id": patch_id,
            "message": f"Backup path '{backup_path}' missing",
        }


# ---------------------------------------------------------------------------
# Patch Synthesizer via Model Call
# ---------------------------------------------------------------------------
def call_model_for_patch(
    target_file: str,
    line_no: int,
    context_snippet: str,
    traceback_str: str,
    error_msg: str,
) -> Tuple[Optional[dict], str]:
    """
    Calls LLM (via OpenRouter using stdlib urllib) to synthesize a surgical
    target_chunk -> replacement_chunk fix.
    """
    env = _load_hermes_env()
    api_key = os.environ.get("OPENROUTER_API_KEY") or env.get("OPENROUTER_API_KEY")
    if not api_key:
        return None, "OPENROUTER_API_KEY not found in env or ~/.hermes/.env"

    prompt = f"""You are an automated code self-healing engine for Python.
Target file: {target_file}
Failing line: {line_no}
Error summary: {error_msg}

Traceback:
{traceback_str}

File Context (lines around failure):
{context_snippet}

Synthesize a minimal, surgical fix. Do NOT rewrite the whole file.
Return ONLY a valid JSON object matching this schema with no markdown fences, no extra text:
{{
  "target_chunk": "<EXACT substring in the target file to replace - must match existing code verbatim>",
  "replacement_chunk": "<REPLACEMENT substring to insert in place of target_chunk>",
  "explanation": "<1-3 sentences plain English explaining what broke and what changed>"
}}
"""

    models_to_try = [
        "google/gemini-2.5-flash",
        "deepseek/deepseek-v4.1-flash",
        "openai/gpt-4o-mini",
    ]

    url = "https://openrouter.ai/api/v1/chat/completions"
    last_err = ""

    for model_id in models_to_try:
        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": "You are an expert Python engineer performing surgical auto-healing patches. Output pure JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1500,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                choice = res_json.get("choices", [{}])[0]
                content = choice.get("message", {}).get("content", "") or ""
                # Strip optional markdown code fences
                content_clean = content.strip()
                if content_clean.startswith("```"):
                    content_clean = re.sub(r"^```(?:json)?\s*", "", content_clean)
                    content_clean = re.sub(r"\s*```$", "", content_clean)
                parsed = json.loads(content_clean)
                if "target_chunk" in parsed and "replacement_chunk" in parsed:
                    return parsed, ""
                last_err = f"Model {model_id} returned JSON missing required keys: {content[:100]}"
        except Exception as exc:
            last_err = f"Model {model_id} call failed: {exc}"
            continue

    return None, f"All model synthesis attempts failed. Last error: {last_err}"


# ---------------------------------------------------------------------------
# AutoHealEngine: The Complete Orchestration
# ---------------------------------------------------------------------------
class AutoHealEngine:
    """
    Orchestrates the complete self-healing workflow:
    1. Checks ON/OFF switch
    2. Analyzes traceback (TracebackAnalyzer)
    3. Reads source and extracts ±25 lines context
    4. Synthesizes minimal patch via model call
    5. Verifies target_chunk exists verbatim
    6. Full AST validation on candidate code
    7. Backs up original file (.bak)
    8. Writes patched file & logs history
    9. Patch-then-notify: Telegram notification immediately
    """

    @classmethod
    def heal_traceback(
        cls,
        traceback_str: str,
        error_msg: str = "",
        custom_context: Optional[str] = None,
        topic_id: int = 7505,
    ) -> dict:
        # 1. Kill-Switch check
        enabled, switch_reason = is_self_heal_enabled()
        if not enabled:
            return {
                "status": "disabled",
                "success": False,
                "reason": switch_reason,
            }

        # 2. Traceback analysis
        frame_info = TracebackAnalyzer.parse(traceback_str)
        if frame_info["status"] != "ok":
            return {
                "status": frame_info["status"],
                "success": False,
                "file": frame_info.get("file"),
                "line": frame_info.get("line"),
                "reason": frame_info.get("reason"),
            }

        target_file = frame_info["file"]
        line_no = frame_info["line"]

        # 3. Read target file & extract context
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                full_source = f.read()
        except Exception as exc:
            return {
                "status": "io_error",
                "success": False,
                "reason": f"Cannot read target file '{target_file}': {exc}",
            }

        lines = full_source.splitlines(keepends=True)
        start_line = max(1, line_no - 25)
        end_line = min(len(lines), line_no + 25)
        context_lines = []
        for idx in range(start_line - 1, end_line):
            context_lines.append(f"{idx + 1:4d} | {lines[idx]}")
        context_snippet = "".join(context_lines)

        # 4. Synthesize patch via LLM
        patch_data, synth_err = call_model_for_patch(
            target_file=target_file,
            line_no=line_no,
            context_snippet=context_snippet,
            traceback_str=traceback_str,
            error_msg=error_msg,
        )
        if not patch_data:
            return {
                "status": "synthesis_failed",
                "success": False,
                "target_file": target_file,
                "line": line_no,
                "reason": synth_err,
            }

        target_chunk = patch_data["target_chunk"]
        replacement_chunk = patch_data["replacement_chunk"]
        explanation = patch_data.get("explanation", "Automated patch applied")

        # 5. Verify target chunk exists verbatim
        if target_chunk not in full_source:
            return {
                "status": "target_chunk_not_found",
                "success": False,
                "target_file": target_file,
                "reason": "Target chunk does not exist verbatim in current file content",
                "target_chunk": target_chunk,
            }

        # 6. Apply replacement & validate with ast.parse()
        staged_source = full_source.replace(target_chunk, replacement_chunk, 1)
        is_valid, val_err = SafetySandbox.validate_code(staged_source)
        if not is_valid:
            return {
                "status": "ast_validation_failed",
                "success": False,
                "target_file": target_file,
                "reason": f"Validation failed on candidate full content: {val_err}",
                "validation_error": val_err,
            }

        # Generate unified diff
        diff_lines = list(difflib.unified_diff(
            full_source.splitlines(keepends=True),
            staged_source.splitlines(keepends=True),
            fromfile=f"a/{os.path.basename(target_file)}",
            tofile=f"b/{os.path.basename(target_file)}",
        ))
        diff_text = "".join(diff_lines)

        # 7. Create backup (.bak)
        patch_id = f"patch_{uuid.uuid4().hex[:8]}"
        backup_path = SafetySandbox.create_backup(target_file, patch_id)

        # 8. Write staged file
        try:
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(staged_source)
        except Exception as exc:
            return {
                "status": "write_failed",
                "success": False,
                "target_file": target_file,
                "reason": f"Failed writing staged patch to {target_file}: {exc}",
            }

        # Log history
        entry = {
            "patch_id": patch_id,
            "target_file": target_file,
            "line_number": line_no,
            "error_msg": error_msg,
            "explanation": explanation,
            "target_chunk": target_chunk,
            "replacement_chunk": replacement_chunk,
            "diff": diff_text,
            "backup_path": backup_path,
            "status": "applied",
            "timestamp": time.time(),
            "timestamp_iso": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            "notified": False,
        }
        SafetySandbox.log_patch(entry)

        # 9. Patch-then-notify (Immediate Telegram notification)
        safe_diff = diff_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        safe_expl = explanation.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        notify_text = (
            f"<b>[DEV FLEET AUTO-HEAL PATCH APPLIED]</b>\n\n"
            f"<b>Patch ID:</b> <code>{patch_id}</code>\n"
            f"<b>Target File:</b> <code>{target_file}:{line_no}</code>\n"
            f"<b>Trigger Error:</b> <code>{error_msg}</code>\n\n"
            f"<b>Explanation:</b> {safe_expl}\n\n"
            f"<b>Diff:</b>\n<pre><code>{safe_diff}</code></pre>\n\n"
            f"<b>Status:</b> AST-validated &amp; written to disk\n"
            f"<b>Backup:</b> <code>{backup_path}</code>"
        )
        notify_ok, notify_msg = send_telegram_notification(notify_text, topic_id=topic_id)
        SafetySandbox.update_patch_status(patch_id, "applied", {"notified": notify_ok, "notify_detail": notify_msg})

        return {
            "status": "applied",
            "success": True,
            "patch_id": patch_id,
            "target_file": target_file,
            "line_number": line_no,
            "explanation": explanation,
            "diff": diff_text,
            "backup_path": backup_path,
            "notified": notify_ok,
            "notify_detail": notify_msg,
        }


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------
def main():
    import argparse

    parser = argparse.ArgumentParser(description="FAOS Phase D Dev Fleet Agent Self-Healing Engine")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Check self-healing switch and health status")
    sub.add_parser("history", help="View patch history")
    sub.add_parser("enable", help="Enable self-healing")
    sub.add_parser("disable", help="Disable self-healing (kill-switch)")
    sub.add_parser("boot-check", help="Run BootSentry crash-recovery check")

    rb_parser = sub.add_parser("rollback", help="Rollback a patch by ID")
    rb_parser.add_argument("patch_id", help="The patch ID to roll back")

    heal_parser = sub.add_parser("heal", help="Trigger heal on a traceback string or file")
    heal_parser.add_argument("--traceback", required=False, help="Raw traceback string")
    heal_parser.add_argument("--file", required=False, help="File containing traceback")
    heal_parser.add_argument("--error", default="RuntimeError", help="Short error description")
    heal_parser.add_argument("--topic", type=int, default=7505, help="Telegram topic thread ID")

    args = parser.parse_args()

    if args.command == "status":
        enabled, reason = is_self_heal_enabled()
        history = SafetySandbox.load_history()
        applied_count = sum(1 for p in history if p.get("status") == "applied")
        print(json.dumps({
            "enabled": enabled,
            "switch_reason": reason,
            "total_patches": len(history),
            "active_applied_patches": applied_count,
            "crash_signal_present": os.path.exists(CRASH_SIGNAL_FILE),
        }, indent=2))

    elif args.command == "enable":
        res = set_self_heal_enabled(True)
        print(json.dumps({"ok": True, "config": res}, indent=2))

    elif args.command == "disable":
        res = set_self_heal_enabled(False)
        print(json.dumps({"ok": True, "config": res}, indent=2))

    elif args.command == "history":
        history = SafetySandbox.load_history()
        print(json.dumps(history, indent=2))

    elif args.command == "rollback":
        ok, msg = SafetySandbox.rollback_patch(args.patch_id)
        print(json.dumps({"ok": ok, "message": msg}, indent=2))

    elif args.command == "boot-check":
        res = BootSentry.check_and_recover()
        print(json.dumps(res, indent=2))

    elif args.command == "heal":
        tb = args.traceback
        if not tb and args.file and os.path.exists(args.file):
            with open(args.file, "r", encoding="utf-8") as f:
                tb = f.read()
        if not tb:
            print(json.dumps({"error": "Must provide --traceback or --file"}))
            sys.exit(1)
        res = AutoHealEngine.heal_traceback(tb, error_msg=args.error, topic_id=args.topic)
        print(json.dumps(res, indent=2))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
