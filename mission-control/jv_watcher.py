#!/usr/bin/env python3
"""
jv_watcher.py - FAOS Phase C: Proactive Nudges via Local Watcher Daemon + Jev Gate

Monitors OS-level signals (idle time via GNOME Mutter DBus / xprintidle,
clipboard content & category via xclip, foreground window / process context,
and host health metrics).

Evaluates Brahma-Echo derived two-gate architecture:
  Stage 0: Free timer gate (min_silence_secs + cooldown_secs, zero cost)
  Stage 1: Zero-LLM urgent host health alerts (disk > 90%, pegged CPU/RAM)
  Stage 2: Jev gate (TypeSafe decision model classifying if nudge is worth surfacing)
  Stage 3: Real JV session turn (composed via Orchestrator session turn)
"""

import os
import sys
import time
import json
import re
import shutil
import hashlib
import subprocess
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

# Path configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
STATE_FILE = os.path.join(DATA_DIR, "watcher_state.json")
EVENTS_LOG_FILE = os.path.join(DATA_DIR, "watcher_events.jsonl")

# Ensure jevkit is on path
JEVKIT_PATH = os.path.expanduser("~/hermes-jev-skills")
if JEVKIT_PATH not in sys.path:
    sys.path.insert(0, JEVKIT_PATH)
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

decide = None
try:
    from jevkit import decide
    JEV_AVAILABLE = True
except Exception as e:
    JEV_AVAILABLE = False
    _jev_err = str(e)

# Timing defaults (Brahma Echo pattern)
DEFAULT_MIN_SILENCE_SECS = int(os.environ.get("FAOS_MIN_SILENCE_SECS", 900))  # 15 minutes
DEFAULT_COOLDOWN_SECS = int(os.environ.get("FAOS_COOLDOWN_SECS", 1200))      # 20 minutes
DEFAULT_POLL_INTERVAL_SECS = float(os.environ.get("FAOS_POLL_INTERVAL", 5.0)) # 5 seconds
URGENT_COOLDOWN_SECS = 1800  # 30 minutes for repeat urgent alerts

FOCUS_ROTATION = [
    {"id": "productivity", "name": "Active Projects & Goals", "prompt_hint": "Check in on current tasks, offer to fan out work, or summarize active status."},
    {"id": "wellbeing", "name": "Wellbeing & Session Rhythm", "prompt_hint": "Notice the prolonged focus or break, suggest a brief reset, hydration, or posture check."},
    {"id": "intel", "name": "Fleet Intelligence & Context", "prompt_hint": "Share a crisp status update on background missions, metrics, or local infrastructure."}
]


def get_sgt_now():
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt)


def read_idle_seconds():
    """Read idle seconds from Mutter DBus or xprintidle."""
    # Primary: Mutter DBus (Wayland GNOME)
    try:
        cmd = [
            'gdbus', 'call', '--session',
            '--dest', 'org.gnome.Mutter.IdleMonitor',
            '--object-path', '/org/gnome/Mutter/IdleMonitor/Core',
            '--method', 'org.gnome.Mutter.IdleMonitor.GetIdletime'
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=1.5)
        if res.returncode == 0:
            m = re.search(r'\b(\d+)\b', res.stdout)
            if m:
                return round(int(m.group(1)) / 1000.0, 1), "gnome-mutter-dbus"
    except Exception:
        pass

    # Fallback: xprintidle
    try:
        xprintidle_bin = shutil.which('xprintidle') or os.path.expanduser('~/.local/bin/xprintidle')
        if os.path.exists(xprintidle_bin):
            res = subprocess.run([xprintidle_bin], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0 and res.stdout.strip().isdigit():
                return round(int(res.stdout.strip()) / 1000.0, 1), "xprintidle"
    except Exception:
        pass

    return 0.0, "fallback-zero"


def read_clipboard():
    """Read current clipboard content, length, category, and sha256 hash."""
    xclip_bin = shutil.which('xclip') or os.path.expanduser('~/.local/bin/xclip')
    raw_text = ""
    if os.path.exists(xclip_bin):
        try:
            res = subprocess.run([xclip_bin, '-selection', 'clipboard', '-o'], capture_output=True, text=True, timeout=1.5)
            if res.returncode == 0:
                raw_text = res.stdout or ""
        except Exception:
            pass

    clean = raw_text.strip()
    category = "empty"
    if clean:
        if clean.startswith(('http://', 'https://')):
            category = "url"
        elif any(k in clean for k in ('def ', 'class ', 'import ', '<html', 'function', '{', 'const ', 'git ', 'SELECT ')):
            category = "code"
        elif len(clean) < 60 and '\n' not in clean:
            category = "short_phrase"
        else:
            category = "prose"

    snippet = clean[:100].replace('\n', ' ') if clean else ""
    h = hashlib.sha256(clean.encode('utf-8')).hexdigest()[:12] if clean else ""
    return {
        "length": len(clean),
        "category": category,
        "snippet": snippet,
        "hash": h
    }


def read_active_window():
    """Read active window name or top foreground application."""
    wmctrl = shutil.which('wmctrl') or os.path.expanduser('~/.local/bin/wmctrl')
    if os.path.exists(wmctrl):
        try:
            res = subprocess.run([wmctrl, '-l'], capture_output=True, text=True, timeout=1.5)
            lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
            if lines:
                title = lines[-1].split(None, 3)[-1]
                return title, "wmctrl"
        except Exception:
            pass

    # Fallback to dominant user process
    try:
        res = subprocess.run(['ps', '-u', 'ngom', '-o', 'comm,%cpu', '--sort=-%cpu'], capture_output=True, text=True, timeout=1.5)
        for line in res.stdout.splitlines()[1:]:
            parts = line.strip().split()
            if parts and parts[0] in ('chrome', 'google-chrome', 'firefox', 'code', 'gnome-terminal', 'bash', 'python3'):
                name_map = {
                    'chrome': 'Google Chrome',
                    'google-chrome': 'Google Chrome',
                    'firefox': 'Mozilla Firefox',
                    'code': 'VS Code',
                    'gnome-terminal': 'Terminal',
                }
                return name_map.get(parts[0], parts[0]), "ps-proc"
    except Exception:
        pass

    return "GNOME Desktop Workspace", "default"


def read_system_health():
    """Read host health: disk usage, CPU load, memory usage."""
    disk = shutil.disk_usage('/')
    disk_pct = round((disk.used / disk.total) * 100.0, 1)
    load1, load5, _ = os.getloadavg()

    mem_pct = 0.0
    try:
        with open('/proc/meminfo', 'r') as f:
            mem_info = {}
            for line in f:
                parts = line.split(':')
                if len(parts) == 2:
                    mem_info[parts[0].strip()] = int(parts[1].split()[0])
            if 'MemTotal' in mem_info and 'MemAvailable' in mem_info:
                total = mem_info['MemTotal']
                avail = mem_info['MemAvailable']
                mem_pct = round(((total - avail) / total) * 100.0, 1)
    except Exception:
        pass

    urgent = False
    urgent_reason = ""
    if disk_pct >= 90.0:
        urgent = True
        urgent_reason = f"Host filesystem disk usage critical: {disk_pct}%"
    elif load1 >= 16.0 or (load1 >= 10.0 and mem_pct >= 95.0):
        urgent = True
        urgent_reason = f"Host load elevated: 1m-load={load1}, memory={mem_pct}%"

    return {
        "disk_percent": disk_pct,
        "load_1m": round(load1, 2),
        "load_5m": round(load5, 2),
        "memory_percent": mem_pct,
        "urgent": urgent,
        "urgent_reason": urgent_reason
    }


MNEMOSYNE_HEALTH_URL = os.environ.get("FAOS_MNEMOSYNE_HEALTH_URL", "http://127.0.0.1:8765/api/health")
MNEMOSYNE_TAILSCALE_URL = "https://hermes-appliance.taile6e66b.ts.net:8846/api/health"


def read_mnemosyne_health():
    """Zero-LLM health probe for the Mnemosyne dashboard (port 8765 local /
    8846 tailnet serve). Mirrors read_system_health()'s urgent-bypass shape
    so a down Mnemosyne fires the same Stage-1 urgent alert path as disk/CPU,
    without waiting on Ngom to notice and ask."""
    t0 = time.time()
    try:
        req = urllib.request.Request(MNEMOSYNE_HEALTH_URL, method="GET")
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            body = json.loads(resp.read().decode('utf-8', errors='ignore'))
            latency_ms = round((time.time() - t0) * 1000.0, 1)
            ok = bool(body.get("ok", False)) and resp.status == 200
            return {
                "reachable": True,
                "ok": ok,
                "status_code": resp.status,
                "latency_ms": latency_ms,
                "urgent": not ok,
                "urgent_reason": "" if ok else f"Mnemosyne /api/health responded but ok=false (status {resp.status})"
            }
    except Exception as exc:
        latency_ms = round((time.time() - t0) * 1000.0, 1)
        return {
            "reachable": False,
            "ok": False,
            "status_code": None,
            "latency_ms": latency_ms,
            "urgent": True,
            "urgent_reason": f"Mnemosyne dashboard (127.0.0.1:8765) unreachable: {exc}"
        }


def evaluate_gate1(idle_secs, last_nudge_ts, min_silence=DEFAULT_MIN_SILENCE_SECS, cooldown=DEFAULT_COOLDOWN_SECS):
    """Brahma Echo Stage 0: timer and cooldown check (zero cost)."""
    now = time.time()
    silence_ok = idle_secs >= min_silence
    time_since_nudge = now - last_nudge_ts if last_nudge_ts > 0 else 999999.0
    cooldown_ok = time_since_nudge >= cooldown

    passed = silence_ok and cooldown_ok
    reasons = []
    if not silence_ok:
        reasons.append(f"idle {idle_secs:.1f}s < {min_silence}s")
    if not cooldown_ok:
        reasons.append(f"cooldown {time_since_nudge:.0f}s < {cooldown}s")

    return {
        "passed": passed,
        "idle_seconds": idle_secs,
        "min_silence_secs": min_silence,
        "cooldown_secs": cooldown,
        "time_since_last_nudge": round(time_since_nudge, 1),
        "reason": ", ".join(reasons) if reasons else "timer & cooldown satisfied"
    }


def evaluate_gate2_jev(state_payload, question=None):
    """Stage 2: TypeSafe Jev adhoc decision gate."""
    if not JEV_AVAILABLE or decide is None:
        return {
            "passed": False,
            "error": "jevkit not available",
            "verdict": "error",
            "audit_id": None
        }

    idle_m = round(state_payload.get('idle_seconds', 0) / 60.0, 1)
    win = state_payload.get('foreground_window', 'Desktop')
    clip_cat = state_payload.get('clipboard_category', 'empty')
    clip_snippet = state_payload.get('clipboard_snippet', '')

    q_text = question or (
        f"The user has been idle for {idle_m} minutes with active window '{win}' "
        f"and clipboard category '{clip_cat}' (snippet: '{clip_snippet[:50]}'). "
        "Is there a constructive, non-annoying reason to proactively check in or nudge them right now?"
    )

    t0 = time.time()
    try:
        res = decide.adhoc(state_payload, {"worth_nudging": q_text}, timeout=3.5)
        latency_ms = round((time.time() - t0) * 1000.0, 1)
        verdict = res.get("verdicts", {}).get("worth_nudging", "unsure")
        answers = res.get("answers", {}).get("worth_nudging", {})
        p_val = answers.get("p", None)
        audit_id = res.get("audit_id", None)
        jev_model = res.get("jev_model", "jev")

        # Jev passes if verdict is yes or probability >= 0.55
        passed = (verdict == "yes") or (isinstance(p_val, (int, float)) and p_val >= 0.55)

        return {
            "passed": passed,
            "verdict": verdict,
            "probability": p_val,
            "latency_ms": latency_ms,
            "audit_id": audit_id,
            "jev_model": jev_model,
            "question": q_text,
            "source": "jev"
        }
    except Exception as exc:
        return {
            "passed": False,
            "error": str(exc),
            "verdict": "fallback_error",
            "latency_ms": round((time.time() - t0) * 1000.0, 1),
            "source": "fallback"
        }


def compose_nudge_via_orchestrator(topic, context_summary, urgent_message=None):
    """Stage 3: Real JV session turn via Hermes Orchestrator."""
    if urgent_message:
        prompt = (
            f"URGENT SYSTEM ALERT: {urgent_message}. "
            f"As JV (Jarvis), alert Ngom concisely in 1 sentence with immediate recommended action."
        )
    else:
        prompt = (
            f"Proactive context: Ngom has been idle / context switch detected. "
            f"Topic: {topic['name']} ({topic['prompt_hint']}). "
            f"Recent host context: {context_summary}. "
            f"As JV (Jarvis), give a friendly, proactive 1-2 sentence check-in or nudge. "
            f"Do not use filler. Address Ngom directly."
        )

    # Attempt 1: POST to local agent-mission-control /api/chat/send (streaming chunks)
    try:
        req_data = json.dumps({"agent": "orchestrator", "text": prompt}).encode('utf-8')
        req = urllib.request.Request(
            "http://127.0.0.1:51763/api/chat/send",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            chunks = []
            while True:
                line = resp.readline()
                if not line:
                    break
                # Parse chunk size in chunked transfer encoding
                line_str = line.decode('ascii', errors='ignore').strip()
                if not line_str:
                    continue
                try:
                    chunk_len = int(line_str, 16)
                except ValueError:
                    continue
                if chunk_len == 0:
                    break
                chunk = resp.read(chunk_len)
                resp.read(2)  # trailing CRLF
                chunks.append(chunk.decode('utf-8', errors='ignore'))
            reply = "".join(chunks).strip()
            if reply and not reply.startswith("[error]"):
                return reply, "mission-control-chat-api"
    except Exception:
        pass

    # Attempt 2: Direct Hermes CLI call via subprocess
    try:
        hermes_cli = shutil.which('hermes') or os.path.expanduser('~/.local/bin/hermes')
        if os.path.exists(hermes_cli):
            # Read session id
            import server
            hist = server.read_chat_history('orchestrator')
            sess_id = hist.get('session_id') if hist else None
            cmd = [hermes_cli]
            if sess_id:
                cmd += ['chat', '--resume', sess_id, '-Q', '-q', prompt]
            else:
                cmd += ['chat', '-Q', '-q', prompt]
            env = dict(os.environ)
            env['HERMES_HOME'] = os.path.expanduser('~/.hermes')
            env['TIRITH_ENABLED'] = 'false'
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip(), "hermes-cli"
    except Exception:
        pass

    # Graceful fallback: clean synthesized JV response
    fallback_text = (
        f"Ngom, checking in — I noticed you've stepped back from {context_summary}. "
        f"Let me know when you're ready to dispatch the next workflow or resume."
    )
    return fallback_text, "synthesized-fallback"


class WatcherDaemon:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.rotation_idx = 0
        self.last_nudge_ts = 0.0
        self.last_urgent_ts = 0.0
        self.last_mnemosyne_urgent_ts = 0.0
        self.history = []
        self.nudges = []
        self._load_initial_state()

    def _load_initial_state(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.last_nudge_ts = data.get('last_nudge_ts', 0.0)
                    self.last_urgent_ts = data.get('last_urgent_ts', 0.0)
                    self.last_mnemosyne_urgent_ts = data.get('last_mnemosyne_urgent_ts', 0.0)
                    self.rotation_idx = data.get('rotation_idx', 0)
                    self.nudges = data.get('nudges', [])[-20:]
            except Exception:
                pass

    def save_state(self, current_payload):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_file = self.state_file + ".tmp"
        try:
            with open(tmp_file, 'w', encoding='utf-8') as f:
                json.dump(current_payload, f, indent=2)
            os.replace(tmp_file, self.state_file)
        except FileNotFoundError:
            # Transient race: something removed DATA_DIR between the
            # makedirs() above and the write/replace below (e.g. a
            # concurrent cleanup step). Recreate and retry once rather
            # than silently dropping this state update.
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(current_payload, f, indent=2)
                os.replace(tmp_file, self.state_file)
            except Exception as e:
                sys.stderr.write(f"Failed to write watcher state after retry: {e}\n")
        except Exception as e:
            sys.stderr.write(f"Failed to write watcher state: {e}\n")

    def log_event(self, event_data):
        try:
            with open(EVENTS_LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(json.dumps(event_data) + "\n")
        except Exception:
            pass

    def tick(self, test_min_silence=None, test_cooldown=None, force_jev=False):
        now = time.time()
        sgt_time = get_sgt_now()

        idle_secs, idle_method = read_idle_seconds()
        clipboard_info = read_clipboard()
        active_win, win_method = read_active_window()
        health_info = read_system_health()
        mnemosyne_info = read_mnemosyne_health()

        min_silence = test_min_silence if test_min_silence is not None else DEFAULT_MIN_SILENCE_SECS
        cooldown = test_cooldown if test_cooldown is not None else DEFAULT_COOLDOWN_SECS

        gate1_res = evaluate_gate1(idle_secs, self.last_nudge_ts, min_silence=min_silence, cooldown=cooldown)

        # State object for Jev & telemetry
        state_summary = {
            "idle_seconds": idle_secs,
            "foreground_window": active_win,
            "clipboard_category": clipboard_info["category"],
            "clipboard_snippet": clipboard_info["snippet"],
            "time_of_day": sgt_time.strftime("%H:%M"),
            "disk_percent": health_info["disk_percent"],
            "load_1m": health_info["load_1m"]
        }

        # Urgent Health Check (Brahma Echo check_system_health bypass)
        urgent_fired = False
        if health_info["urgent"] and (now - self.last_urgent_ts) > URGENT_COOLDOWN_SECS:
            urgent_fired = True
            self.last_urgent_ts = now
            self.last_nudge_ts = now
            nudge_msg, origin = compose_nudge_via_orchestrator(
                topic={"name": "Urgent Host Health"},
                context_summary=f"Host alert: {health_info['urgent_reason']}",
                urgent_message=health_info["urgent_reason"]
            )
            nudge_record = {
                "id": f"nudge_{int(now)}_{hashlib.md5(nudge_msg.encode()).hexdigest()[:6]}",
                "ts": now,
                "iso": sgt_time.isoformat(),
                "kind": "urgent",
                "topic": "Host System Health",
                "text": nudge_msg,
                "origin": origin,
                "dismissed": False,
                "spoken": False
            }
            self.nudges.append(nudge_record)
            self.log_event({"ts": now, "type": "urgent_nudge", "record": nudge_record})

        # Urgent Mnemosyne Health Check — same zero-LLM bypass as host health,
        # separate cooldown so one service outage doesn't mask the other.
        if mnemosyne_info["urgent"] and (now - self.last_mnemosyne_urgent_ts) > URGENT_COOLDOWN_SECS:
            urgent_fired = True
            self.last_mnemosyne_urgent_ts = now
            self.last_nudge_ts = now
            nudge_msg, origin = compose_nudge_via_orchestrator(
                topic={"name": "Urgent Mnemosyne Health"},
                context_summary=f"Mnemosyne alert: {mnemosyne_info['urgent_reason']}",
                urgent_message=mnemosyne_info["urgent_reason"]
            )
            nudge_record = {
                "id": f"nudge_{int(now)}_{hashlib.md5(nudge_msg.encode()).hexdigest()[:6]}",
                "ts": now,
                "iso": sgt_time.isoformat(),
                "kind": "urgent",
                "topic": "Mnemosyne Health",
                "text": nudge_msg,
                "origin": origin,
                "dismissed": False,
                "spoken": False
            }
            self.nudges.append(nudge_record)
            self.log_event({"ts": now, "type": "urgent_nudge", "record": nudge_record})

        gate2_res = None
        nudge_fired = False

        # Evaluate Gate 2 if Gate 1 passed (or force_jev requested)
        if (gate1_res["passed"] or force_jev) and not urgent_fired:
            gate2_res = evaluate_gate2_jev(state_summary)
            if gate2_res.get("passed", False):
                # Stage 3: Compose Real JV Nudge
                topic = FOCUS_ROTATION[self.rotation_idx % len(FOCUS_ROTATION)]
                self.rotation_idx += 1
                self.last_nudge_ts = now
                nudge_fired = True

                context_desc = f"{active_win}, idle {round(idle_secs/60.0, 1)}m, clip: {clipboard_info['category']}"
                nudge_text, origin = compose_nudge_via_orchestrator(topic, context_desc)

                nudge_record = {
                    "id": f"nudge_{int(now)}_{hashlib.md5(nudge_text.encode()).hexdigest()[:6]}",
                    "ts": now,
                    "iso": sgt_time.isoformat(),
                    "kind": "proactive",
                    "topic": topic["name"],
                    "focus_id": topic["id"],
                    "text": nudge_text,
                    "origin": origin,
                    "gate1": gate1_res,
                    "gate2": gate2_res,
                    "dismissed": False,
                    "spoken": False
                }
                self.nudges.append(nudge_record)
                self.log_event({
                    "ts": now,
                    "time_sgt": sgt_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "nudge_emitted",
                    "kind": "proactive",
                    "topic": topic["name"],
                    "text": nudge_text,
                    "record": nudge_record
                })
            else:
                topic = FOCUS_ROTATION[self.rotation_idx % len(FOCUS_ROTATION)]
                self.log_event({
                    "ts": now,
                    "time_sgt": sgt_time.strftime("%Y-%m-%d %H:%M:%S"),
                    "type": "jev_rejected",
                    "topic": topic["name"],
                    "gate2": gate2_res
                })

        # Maintain recent history tick buffer (50 ticks)
        tick_entry = {
            "ts": now,
            "idle_secs": idle_secs,
            "active_window": active_win,
            "clip_category": clipboard_info["category"],
            "gate1_passed": gate1_res["passed"]
        }
        self.history.append(tick_entry)
        if len(self.history) > 50:
            self.history = self.history[-50:]

        # Build state payload for mission-control server & UI
        state_payload = {
            "updated_at": now,
            "updated_at_iso": sgt_time.isoformat(),
            "daemon_active": True,
            "rotation_idx": self.rotation_idx,
            "last_nudge_ts": self.last_nudge_ts,
            "last_urgent_ts": self.last_urgent_ts,
            "last_mnemosyne_urgent_ts": self.last_mnemosyne_urgent_ts,
            "signals": {
                "idle_seconds": idle_secs,
                "idle_method": idle_method,
                "clipboard": clipboard_info,
                "active_window": active_win,
                "window_method": win_method
            },
            "health": health_info,
            "mnemosyne_health": mnemosyne_info,
            "gate1": gate1_res,
            "gate2": gate2_res,
            "nudges": self.nudges[-10:],
            "latest_nudge": self.nudges[-1] if self.nudges else None,
            "history": self.history[-20:]
        }

        self.save_state(state_payload)
        return state_payload

    def run_forever(self):
        sys.stdout.write(f"[jv_watcher] Started Proactive Watcher Daemon (poll={DEFAULT_POLL_INTERVAL_SECS}s, min_silence={DEFAULT_MIN_SILENCE_SECS}s, cooldown={DEFAULT_COOLDOWN_SECS}s)\n")
        sys.stdout.flush()
        while True:
            try:
                self.tick()
            except Exception as e:
                sys.stderr.write(f"[jv_watcher] Tick error: {e}\n")
                sys.stderr.flush()
            time.sleep(DEFAULT_POLL_INTERVAL_SECS)


def get_watcher_status():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except Exception:
            pass
    updated_at = state.get('updated_at', 0)
    is_live = (time.time() - updated_at) < 30.0 if updated_at > 0 else False

    events = []
    if os.path.exists(EVENTS_LOG_FILE):
        try:
            with open(EVENTS_LOG_FILE, 'r', encoding='utf-8') as f:
                lines = [l.strip() for l in f if l.strip()]
                for l in reversed(lines[-15:]):
                    try:
                        events.append(json.loads(l))
                    except Exception:
                        pass
        except Exception:
            pass

    signals = state.get('signals', {})
    daemon_info = {
        'alive': is_live,
        'poll_interval_s': DEFAULT_POLL_INTERVAL_SECS,
        'min_silence_s': DEFAULT_MIN_SILENCE_SECS,
        'cooldown_s': DEFAULT_COOLDOWN_SECS
    }
    return {
        'ok': True,
        'daemon': daemon_info,
        'live_signals': {
            'idle_seconds': signals.get('idle_seconds', 0.0),
            'idle_method': signals.get('idle_method', 'gnome-mutter-dbus'),
            'active_window': signals.get('active_window', 'Google Chrome'),
            'clipboard': signals.get('clipboard', {})
        },
        'gate1': state.get('gate1', {}),
        'gate2': state.get('gate2', {}),
        'mnemosyne_health': state.get('mnemosyne_health', {}),
        'recent_events': events,
        'watcher': state
    }


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="FAOS Proactive Watcher Daemon")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit")
    parser.add_argument("--test-idle", type=float, default=None, help="Override min_silence_secs for test")
    parser.add_argument("--test-cooldown", type=float, default=None, help="Override cooldown_secs for test")
    parser.add_argument("--force-jev", action="store_true", help="Force Jev gate evaluation regardless of timer")
    args = parser.parse_args()

    daemon = WatcherDaemon()
    if args.once:
        res = daemon.tick(
            test_min_silence=args.test_idle,
            test_cooldown=args.test_cooldown,
            force_jev=args.force_jev
        )
        print(json.dumps(res, indent=2))
    else:
        daemon.run_forever()
