import os
import re
import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_HOME = os.path.expanduser("~/.hermes")
AGENT_LOGS_DB_PATH = os.path.join(PROJECT_DIR, "agent-logs.db")

# The 7 main agents can NEVER be deleted through this feature — hardcoded protection.
PROTECTED_AGENTS = frozenset(["orchestrator", "scout", "scribe", "reach", "dev", "dev2"])

FAOS_SPARE_AVATARS = ["Spare1.jpg", "Spare2.jpg", "Spare3.jpg"]


def _db_conn(read_only=False):
    if read_only:
        uri = f"file:{AGENT_LOGS_DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only = ON;")
    else:
        conn = sqlite3.connect(AGENT_LOGS_DB_PATH, timeout=10)
    return conn


def init_secondary_agents_table():
    try:
        conn = _db_conn(read_only=False)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS secondary_agents (
                    profile_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    primary_role TEXT NOT NULL,
                    secondary_roles TEXT NOT NULL,
                    source_profile TEXT,
                    created_at TEXT NOT NULL,
                    avatar TEXT NOT NULL
                )
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[secondary_agents] init table error: {e}")


def list_secondary_agents():
    if not os.path.isfile(AGENT_LOGS_DB_PATH):
        return []
    try:
        conn = _db_conn(read_only=True)
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT profile_name, display_name, primary_role, secondary_roles,
                       source_profile, created_at, avatar
                FROM secondary_agents ORDER BY created_at ASC
            """)
            rows = []
            for r in cur.fetchall():
                sec_roles = []
                if r[3]:
                    try:
                        sec_roles = json.loads(r[3])
                    except Exception:
                        sec_roles = []
                rows.append({
                    "profile_name": r[0],
                    "display_name": r[1],
                    "primary_role": r[2],
                    "secondary_roles": sec_roles,
                    "source_profile": r[4],
                    "created_at": r[5],
                    "avatar": r[6] or "Spare1.jpg",
                })
            return rows
        finally:
            conn.close()
    except Exception as e:
        print(f"[secondary_agents] list error: {e}")
        return []


def ensure_venvs_have_mnemosyne():
    uv_bin = os.path.expanduser("~/.hermes/bin/uv")
    if not os.path.isfile(uv_bin):
        uv_bin = shutil.which("uv")
    if not uv_bin or not os.path.isfile(uv_bin):
        return
    import glob
    for py in glob.glob(os.path.expanduser("~/.hermes/installs/**/bin/python"), recursive=True):
        res = subprocess.run([py, "-c", "import mnemosyne, mnemosyne_hermes"], capture_output=True)
        if res.returncode != 0:
            print(f"[secondary_agents] repairing mnemosyne in {py}...")
            subprocess.run([
                uv_bin, "pip", "install", "--python", py,
                "--exclude-newer-package", "mnemosyne-memory=false",
                "mnemosyne-memory[embeddings]==4.0.0b4",
                "mnemosyne-hermes==0.7.4"
            ], capture_output=True, timeout=120)


def create_secondary_agent(profile_name, display_name, primary_role, secondary_roles=None, source_profile=None):
    init_secondary_agents_table()
    profile_name = (profile_name or "").strip().lower()
    display_name = (display_name or "").strip()
    primary_role = (primary_role or "").strip()
    if secondary_roles is None:
        secondary_roles = []
    elif isinstance(secondary_roles, str):
        try:
            secondary_roles = json.loads(secondary_roles)
        except Exception:
            secondary_roles = [secondary_roles]

    if not re.match(r"^[a-z0-9]+$", profile_name):
        raise ValueError("profile_name must be lowercase alphanumeric with no spaces or symbols")

    if profile_name in PROTECTED_AGENTS:
        raise ValueError(f"'{profile_name}' is a protected main agent name")

    existing = list_secondary_agents()
    if any(s["profile_name"] == profile_name for s in existing):
        raise ValueError(f"Secondary agent '{profile_name}' already exists")

    profile_dir = os.path.join(HERMES_HOME, "profiles", profile_name)
    if os.path.exists(profile_dir):
        raise ValueError(f"Profile directory for '{profile_name}' already exists on disk")

    if not display_name:
        raise ValueError("display_name is required")
    if not primary_role:
        raise ValueError("primary_role is required")

    if source_profile:
        source_profile = source_profile.strip().lower()
        if source_profile not in PROTECTED_AGENTS and source_profile != "default":
            raise ValueError(f"Invalid source_profile '{source_profile}'. Must be one of the 7 main agents or empty.")

    # Step 1: Run hermes profile create
    hermes_bin = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    if source_profile:
        clone_src = "default" if source_profile == "orchestrator" else source_profile
        create_cmd = [hermes_bin, "profile", "create", profile_name, "--clone-from", clone_src]
    else:
        create_cmd = [hermes_bin, "profile", "create", profile_name]

    try:
        p_res = subprocess.run(create_cmd, capture_output=True, text=True, timeout=120)
        if p_res.returncode != 0 and not os.path.isdir(profile_dir):
            raise RuntimeError(f"hermes profile create failed (code {p_res.returncode}): {p_res.stderr or p_res.stdout}")

        if not os.path.isdir(profile_dir):
            raise RuntimeError(f"Profile directory '{profile_dir}' was not created")

        # Step 2: Configure config.yaml for Mnemosyne with profile_isolation
        config_path = os.path.join(profile_dir, "config.yaml")
        config_txt = ""
        if os.path.isfile(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                config_txt = f.read()

        mem_block = (
            "\nmemory:\n"
            "  memory_enabled: true\n"
            "  user_profile_enabled: true\n"
            "  memory_char_limit: 2200\n"
            "  user_char_limit: 1375\n"
            "  nudge_interval: 10\n"
            "  provider: mnemosyne\n"
            "  mnemosyne:\n"
            "    profile_isolation: true\n"
        )
        if re.search(r"^memory:.*?(?=\n\S|\Z)", config_txt, re.M | re.S):
            config_txt = re.sub(r"^memory:.*?(?=\n\S|\Z)", mem_block.strip(), config_txt, flags=re.M | re.S)
        else:
            config_txt += mem_block

        with open(config_path, "w", encoding="utf-8") as f:
            f.write(config_txt)

        # Step 3: Enable mnemosyne plugin
        plug_res = subprocess.run(
            [hermes_bin, "--profile", profile_name, "plugins", "enable", "mnemosyne", "--no-allow-tool-override"],
            capture_output=True, text=True, timeout=60,
        )
        if plug_res.returncode != 0:
            # Fallback attempt to hermes-mnemosyne
            subprocess.run(
                [hermes_bin, "--profile", profile_name, "plugins", "enable", "hermes-mnemosyne", "--no-allow-tool-override"],
                capture_output=True, text=True, timeout=60,
            )

        # Step 3b: Ensure per-call venvs have required mnemosyne packages
        ensure_venvs_have_mnemosyne()

        # Step 4: Live verify Mnemosyne stats on the newly created profile
        py_bin = os.path.expanduser("~/.hermes/mnemosyne-venv/bin/python3")
        verify_script = (
            "from mnemosyne.mcp_tools import handle_tool_call; "
            "res = handle_tool_call('mnemosyne_stats', {}); "
            "db = (res.get('stats') or {}).get('database', ''); "
            "assert db and 'mnemosyne.db' in db, f'invalid db path: {db}'; "
            "print('VERIFIED:' + db)"
        )
        verify_env = dict(os.environ)
        verify_env["HERMES_HOME"] = profile_dir
        v_res = subprocess.run(
            [py_bin, "-c", verify_script],
            capture_output=True, text=True, env=verify_env, timeout=30,
        )
        if v_res.returncode != 0 or "VERIFIED:" not in v_res.stdout:
            raise RuntimeError(f"Mnemosyne live verification failed: {v_res.stderr or v_res.stdout}")

        # Step 5: Assign avatar and insert registry row
        avatar_idx = len(existing) % len(FAOS_SPARE_AVATARS)
        avatar = FAOS_SPARE_AVATARS[avatar_idx]
        created_at = datetime.now(timezone.utc).isoformat()

        conn = _db_conn(read_only=False)
        try:
            conn.execute("""
                INSERT INTO secondary_agents (profile_name, display_name, primary_role, secondary_roles, source_profile, created_at, avatar)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (profile_name, display_name, primary_role, json.dumps(secondary_roles), source_profile, created_at, avatar))
            conn.commit()
        finally:
            conn.close()

        code = f"A-{7 + len(existing):02d}"
        initials = (display_name[:2] if display_name else profile_name[:2]).upper()
        short = (display_name or profile_name).upper()[:4]

        return {
            "profile_name": profile_name,
            "key": profile_name,
            "code": code,
            "initials": initials,
            "role": primary_role,
            "secondaryRoles": secondary_roles,
            "channel": f"#{profile_name}",
            "name": display_name,
            "short": short,
            "source_profile": source_profile,
            "created_at": created_at,
            "avatar": avatar,
            "isSecondary": True,
            "tasksToday": 0,
            "success": 100.0,
            "defaultModel": "",
            "share": 0,
            "load": 0,
            "tokens": "0 tasks",
            "latency": "—",
            "state": "IDLE",
            "opState": "idle",
            "task": "",
        }

    except Exception as exc:
        # Rollback on any failure
        print(f"[secondary_agents] create failed, rolling back {profile_name}: {exc}")
        delete_secondary_agent(profile_name, ignore_protection=True)
        raise


def delete_secondary_agent(profile_name, ignore_protection=False):
    profile_name = (profile_name or "").strip().lower()
    if not profile_name:
        return False

    if not ignore_protection and profile_name in PROTECTED_AGENTS:
        raise PermissionError(f"Agent '{profile_name}' is one of the 7 protected main agents and can NEVER be deleted.")

    hermes_bin = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
    try:
        subprocess.run([hermes_bin, "profile", "delete", profile_name, "-y"], capture_output=True, text=True, timeout=60)
    except Exception as e:
        print(f"[secondary_agents] hermes profile delete error: {e}")

    # Ensure profile directory is completely wiped
    profile_dir = os.path.join(HERMES_HOME, "profiles", profile_name)
    if os.path.exists(profile_dir):
        shutil.rmtree(profile_dir, ignore_errors=True)

    # Remove CLI alias if leftover
    alias_path = os.path.expanduser(f"~/.local/bin/{profile_name}")
    if os.path.isfile(alias_path):
        try:
            os.remove(alias_path)
        except OSError:
            pass

    # Delete agent_logs rows and registry row from agent-logs.db
    if os.path.isfile(AGENT_LOGS_DB_PATH):
        try:
            conn = _db_conn(read_only=False)
            try:
                conn.execute("DELETE FROM agent_logs WHERE agent_name = ?", (profile_name,))
                conn.execute("DELETE FROM secondary_agents WHERE profile_name = ?", (profile_name,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            print(f"[secondary_agents] db delete error: {e}")

    return True
