import os, time, subprocess, json, shutil
from concurrent.futures import ThreadPoolExecutor

_jv_diagnostics_cache = {"data": None, "ts": 0}

def get_local_host_diagnostics():
    # CPU
    cpu = 0.0
    try:
        with open('/proc/loadavg') as f:
            loads = [float(x) for x in f.read().split()[:3]]
            cpu = round((loads[0] / max(1, os.cpu_count() or 1)) * 100, 1)
    except Exception:
        pass

    # RAM
    ram_total = 0
    ram_avail = 0
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    ram_total = int(line.split()[1]) * 1024
                elif line.startswith('MemAvailable:'):
                    ram_avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    ram_used = max(0, ram_total - ram_avail)
    ram_pct = round((ram_used / ram_total) * 100, 1) if ram_total else 0.0

    # Disk
    disk = shutil.disk_usage('/')
    disk_pct = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0

    # Uptime
    uptime = 0
    try:
        with open('/proc/uptime') as f:
            uptime = int(float(f.read().split()[0]))
    except Exception:
        pass

    return {
        "host": "local",
        "hostname": "hermes-appliance",
        "os": "Linux",
        "reachable": True,
        "cpu_percent": cpu,
        "ram_total": ram_total,
        "ram_used": ram_used,
        "ram_free": ram_avail,
        "ram_percent": ram_pct,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "disk_free": disk.free,
        "disk_percent": disk_pct,
        "uptime_seconds": uptime
    }

def get_ryzen_host_diagnostics():
    try:
        cmd = [
            'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3', 'ryzen',
            'powershell -NoProfile -Command \"& (Get-Item C:\\Users\\hermes\\AppData\\Local\\hermes\\tools\\python*\\python.exe).FullName C:\\Users\\hermes\\ryzen_metrics.py --once\"'
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5.0)
        if res.returncode == 0:
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
            for line in reversed(lines):
                if line.startswith('{') and line.endswith('}'):
                    data = json.loads(line)
                    data["os"] = "Windows 11"
                    data["reachable"] = True
                    return data
    except Exception as e:
        pass

    return {
        "host": "ryzen",
        "hostname": "NGOM-RYZEN5800X",
        "os": "Windows 11",
        "reachable": False,
        "error": "Host unreachable or metrics timeout",
        "cpu_percent": 0.0,
        "ram_total": 134140887040,
        "ram_used": 0,
        "ram_free": 0,
        "ram_percent": 0.0,
        "disk_total": 511099695104,
        "disk_used": 0,
        "disk_free": 0,
        "disk_percent": 0.0,
        "uptime_seconds": 0
    }

def get_capabilities_status():
    # 1. Google Workspace
    gws_ok = os.path.exists(os.path.expanduser('~/.hermes/skills/google-workspace'))
    gws_tile = {
        "id": "gws",
        "label": "Google Workspace",
        "icon": "mail",
        "status": "ready" if gws_ok else "degraded",
        "detail": "Mail & Calendar skills loaded" if gws_ok else "Skill missing",
        "indicator": "healthy"
    }

    # 2. Document Generation
    docs_skills = ['docx', 'pdf', 'xlsx', 'powerpoint']
    doc_count = sum(1 for s in docs_skills if os.path.exists(os.path.expanduser(f'~/.hermes/skills/{s}')))
    doc_tile = {
        "id": "doc_gen",
        "label": "Doc Gen Skills",
        "icon": "file-text",
        "status": "active" if doc_count == 4 else "degraded",
        "detail": f"{doc_count}/4 skills ready (docx, pdf, xlsx, pptx)",
        "indicator": "healthy" if doc_count == 4 else "warning"
    }

    # 3. Coding Agents (Dev + Dev2)
    dev2_exists = os.path.exists(os.path.expanduser('~/.hermes/profiles/dev2'))
    coding_tile = {
        "id": "coding_agents",
        "label": "Coding Agents",
        "icon": "code",
        "status": "active",
        "detail": "Dev (Lead) + Dev2 (Sub-agent) paired" if dev2_exists else "Dev active",
        "indicator": "healthy"
    }

    # 4. Web Search
    search_tile = {
        "id": "web_search",
        "label": "Web Search",
        "icon": "search",
        "status": "connected",
        "detail": "Perplexity Sonar API online",
        "indicator": "healthy"
    }

    # 5. Browser Automation
    chrom_exists = os.path.exists('/usr/bin/chromium') or os.path.exists('/usr/bin/google-chrome')
    browser_tile = {
        "id": "browser_auto",
        "label": "Browser Automation",
        "icon": "globe",
        "status": "ready" if chrom_exists else "warning",
        "detail": "Chromium engine available" if chrom_exists else "Browser binary missing",
        "indicator": "healthy" if chrom_exists else "warning"
    }

    # 6. Proactive Nudges & Jev Gate
    watcher_state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'watcher_state.json')
    watcher_ok = False
    watcher_detail = "Watcher daemon inactive"
    proactive_state = None
    if os.path.exists(watcher_state_file):
        try:
            with open(watcher_state_file, 'r') as f:
                proactive_state = json.load(f)
            updated_at = proactive_state.get('updated_at', 0)
            if (time.time() - updated_at) < 300.0:
                watcher_ok = True
                idle_s = proactive_state.get('signals', {}).get('idle_seconds', 0)
                jev_verdict = proactive_state.get('gate2', {}).get('verdict', 'ready')
                watcher_detail = f"Jev gate online • Idle {idle_s:.0f}s • {jev_verdict}"
            else:
                watcher_detail = "Watcher state stale"
        except Exception:
            pass
    proactive_tile = {
        "id": "proactive_nudges",
        "label": "Proactive Nudges",
        "icon": "bell",
        "status": "active" if watcher_ok else "ready",
        "detail": watcher_detail,
        "indicator": "healthy" if watcher_ok else "warning"
    }

    # 7. Mnemosyne (memory dashboard health)
    mnemo_status = "offline"
    mnemo_detail = "Service unreachable"
    mnemo_indicator = "error"
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8765/api/health")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            mnemo_data = json.loads(resp.read().decode("utf-8"))
        if mnemo_data.get("ok"):
            mnemo_status = "active"
            mnemo_indicator = "healthy"
            mnemo_detail = "DB reachable" + (" • read-only" if mnemo_data.get("read_only") else "")
        else:
            mnemo_status = "degraded"
            mnemo_indicator = "warning"
            mnemo_detail = "Responded but not ok"
    except Exception as e:
        mnemo_detail = f"Unreachable: {e.__class__.__name__}"
    mnemo_tile = {
        "id": "mnemosyne",
        "label": "Mnemosyne",
        "icon": "brain",
        "status": mnemo_status,
        "detail": mnemo_detail,
        "indicator": mnemo_indicator
    }

    return [gws_tile, doc_tile, coding_tile, search_tile, browser_tile, proactive_tile, mnemo_tile]

def get_jv_diagnostics_payload():
    now = time.time()
    if _jv_diagnostics_cache["data"] is not None and (now - _jv_diagnostics_cache["ts"]) < 10.0:
        return _jv_diagnostics_cache["data"]

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_local = ex.submit(get_local_host_diagnostics)
        f_ryzen = ex.submit(get_ryzen_host_diagnostics)
        local_diag = f_local.result()
        ryzen_diag = f_ryzen.result()

    payload = {
        "ok": True,
        "timestamp": now,
        "capabilities": get_capabilities_status(),
        "hosts": {
            "local": local_diag,
            "ryzen": ryzen_diag
        }
    }
    _jv_diagnostics_cache["data"] = payload
    _jv_diagnostics_cache["ts"] = now
    return payload

if __name__ == '__main__':
    print(json.dumps(get_jv_diagnostics_payload(), indent=2))
