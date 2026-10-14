#!/usr/bin/env python3
"""Bridge between agent-mission-control's personal Tasks tab and REAL Hermes Kanban.

Runs under the Hermes install's own venv (not the dashboard's stdlib-only python,
which can't import hermes_cli). Invoked as a subprocess by server.py's board_*
functions: one JSON command on argv[1], one JSON result on stdout.

Board: the fixed slug "personal" (created via `hermes kanban boards create personal`).
Kept entirely separate from the "default" board agents actually work on, so this
never spawns a worker or interferes with real agent kanban traffic.

The UI uses REAL kanban statuses (triage/todo/scheduled/ready/running/blocked/review/done).
Status writes are explicit and reuse the Hermes Kanban plugin's own helpers
(_drag_to) and kanban_db verbs (complete_task/block_task/unblock_task/...), so
events are recorded; moves into 'running' are refused (dispatcher-owned) and a
move to the current status is a no-op.
"""
import json
import sys
import time

sys.path.insert(0, "/home/hermes/.hermes/hermes-agent")

from hermes_cli import kanban_db as kdb  # noqa: E402
from hermes_cli import kanban_db_connect as kbc  # noqa: E402

BOARD = "personal"

# The UI now speaks REAL kanban statuses (no more todo/doing/done collapse).
# Column order mirrors plugins/kanban/dashboard/plugin_api.py BOARD_COLUMNS.
BOARD_COLUMNS = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]
PROFILES = ["default", "scout", "scribe", "reach", "dev", "dev2"]
# Statuses a UI *move* may target. "running" is deliberately absent: the
# dispatcher/claim path owns it (plugin_api rejects it the same way).
MOVE_TARGETS = {"triage", "todo", "scheduled", "ready", "blocked", "review", "done"}
ACTOR = "owner"


def _load_plugin_api():
    """Import the Hermes Kanban dashboard plugin's own status-write helpers
    (_drag_to / _set_status_direct) so UI moves take the exact same path as
    the real Hermes Kanban board — no second write path."""
    import importlib.util
    path = "/home/hermes/.hermes/hermes-agent/plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_kanban_plugin_api", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_PLUGIN = None


def _plugin():
    global _PLUGIN
    if _PLUGIN is None:
        _PLUGIN = _load_plugin_api()
    return _PLUGIN


def _conn():
    kdb.init_db(board=BOARD)
    return kbc.connect(board=BOARD)


def _block_reasons(conn, ids):
    """Latest 'blocked' event reason per task id (only meaningful for blocked tasks)."""
    out = {}
    if not ids:
        return out
    q = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT task_id, payload FROM task_events WHERE kind IN ('blocked','scheduled') "
        f"AND task_id IN ({q}) ORDER BY id ASC", list(ids)).fetchall()
    for r in rows:
        try:
            out[r["task_id"]] = (json.loads(r["payload"]) or {}).get("reason")
        except Exception:  # noqa: BLE001
            pass
    return out


def _task_to_ui(d: dict, reason=None, ncomments=0, run_started=None) -> dict:
    ts = d.get("completed_at") or d.get("started_at") or d.get("created_at")
    return {
        "id": d["id"],
        "title": d["title"],
        "status": d["status"],
        "priority": int(d.get("priority") or 0),
        "assignee": d.get("assignee"),
        "notes": d.get("body") or "",
        "block_reason": reason if d["status"] in ("blocked", "scheduled") else None,
        "block_kind": d.get("block_kind"),
        "comment_count": ncomments,
        "run_started_at": run_started,
        "worker_pid": d.get("worker_pid"),
        "created_at": d.get("created_at"),
        "updated_at": ts,
    }


def _row_dict(task):
    return dict(task) if not isinstance(task, dict) else task


def cmd_list(_payload: dict) -> list:
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived' "
            "ORDER BY priority DESC, created_at ASC"
        ).fetchall()
        ids = [r["id"] for r in rows]
        reasons = _block_reasons(conn, ids)
        counts = {}
        if ids:
            for r in conn.execute("SELECT task_id, COUNT(*) c FROM task_comments GROUP BY task_id"):
                counts[r["task_id"]] = r["c"]
        out = []
        for r in rows:
            d = dict(r)
            run_started = None
            if d["status"] == "running":
                run_started = d.get("started_at")
            out.append(_task_to_ui(d, reasons.get(d["id"]), counts.get(d["id"], 0), run_started))
        return out
    finally:
        conn.close()


def cmd_show(payload: dict) -> dict:
    task_id = payload.get("id")
    conn = _conn()
    try:
        task = kdb.get_task(conn, task_id)
        if not task:
            raise ValueError(f"task not found: {task_id}")
        d = task.__dict__
        reasons = _block_reasons(conn, [task_id])
        comments = [c.__dict__ for c in kdb.list_comments(conn, task_id)]  # oldest first -> newest last
        runs = [r.__dict__ for r in kdb.list_runs(conn, task_id)]
        events = [e.__dict__ for e in kdb.list_events(conn, task_id)]
        ui = _task_to_ui(d, reasons.get(task_id), len(comments), d.get("started_at") if d["status"] == "running" else None)
        ui["result"] = d.get("result")
        ui["last_failure_error"] = d.get("last_failure_error")
        return {"task": ui, "comments": comments, "runs": runs, "events": events}
    finally:
        conn.close()


def _stamp_actor(conn, task_id, since):
    """Annotate the status event the plugin helper just wrote with who did it
    (the plugin's _set_status_direct records no actor)."""
    row = conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id=? AND kind='status' AND created_at>=? "
        "ORDER BY id DESC LIMIT 1", (task_id, since)).fetchone()
    if row:
        try:
            p = json.loads(row["payload"]) or {}
        except Exception:  # noqa: BLE001
            p = {}
        p["actor"] = ACTOR + " (mission-control)"
        with kdb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(p), row["id"]))


def _move(conn, task_id, target, reason=None):
    """Explicit status move through the real kanban verbs. Returns ok."""
    if target == "running":
        raise ValueError("Cannot move a task into 'running': the dispatcher owns that")
    if target not in MOVE_TARGETS:
        raise ValueError(f"invalid target status: {target}")
    task = kdb.get_task(conn, task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    if task.status == target:
        return True  # no-op: never write a redundant status event
    if task.status == "running":
        raise ValueError("Task is running: let the worker finish, or block it from the Hermes board")
    since = int(time.time())
    api = _plugin()
    if target == "done":
        ok = kdb.complete_task(conn, task_id, result="Marked done from Mission Control",
                               summary="Marked done from Mission Control board", force=True)
    elif target == "blocked":
        if not (reason or "").strip():
            raise ValueError("a block reason is required")
        ok = kdb.block_task(conn, task_id, reason=reason.strip())
    elif target == "scheduled":
        ok = kdb.schedule_task(conn, task_id, reason=(reason or None))
    elif target == "review":
        ok = kdb.request_review(conn, task_id, summary="Moved to review from Mission Control", force=True)
    else:  # triage / todo / ready
        ok = api._drag_to(conn, task_id, target)
    if ok:
        _stamp_actor(conn, task_id, since)
    return bool(ok)


def cmd_create(payload: dict) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    status = payload.get("status") or "todo"
    if status not in ("triage", "todo", "ready"):
        status = "todo"
    priority = int(payload.get("priority") or 1)
    notes = payload.get("notes") or ""
    conn = _conn()
    try:
        task_id = kdb.create_task(
            conn, title=title, body=notes or None, created_by="dashboard",
            priority=priority, board=BOARD,
        )
        _move(conn, task_id, status)
        task = kdb.get_task(conn, task_id)
        return {"id": task_id, "task": _task_to_ui(task.__dict__)}
    finally:
        conn.close()


def cmd_update(payload: dict) -> dict:
    """Edit title/notes/priority and/or move status (explicit 'status' only)."""
    task_id = payload.get("id")
    if not task_id:
        raise ValueError("id is required")
    conn = _conn()
    try:
        ok = True
        if "status" in payload:
            ok = _move(conn, task_id, payload["status"], payload.get("reason"))
        edit_kwargs = {}
        if "title" in payload:
            edit_kwargs["title"] = (payload.get("title") or "").strip()
        if "notes" in payload:
            edit_kwargs["body"] = payload.get("notes") or ""
        if "priority" in payload:
            edit_kwargs["priority"] = int(payload.get("priority") or 1)
        if edit_kwargs:
            ok = kdb.edit_task(conn, task_id, board=BOARD, **edit_kwargs) and ok
        return {"ok": bool(ok)}
    finally:
        conn.close()


def cmd_comment(payload: dict) -> dict:
    conn = _conn()
    try:
        cid = kdb.add_comment(conn, payload.get("id"), ACTOR, payload.get("body") or "")
        return {"ok": True, "comment_id": cid}
    finally:
        conn.close()


def cmd_assign(payload: dict) -> dict:
    prof = payload.get("assignee")
    if prof in (None, "", "none"):
        prof = None
    elif prof not in PROFILES:
        raise ValueError(f"unknown profile: {prof}")
    conn = _conn()
    try:
        return {"ok": bool(kdb.assign_task(conn, payload.get("id"), prof))}
    finally:
        conn.close()


def cmd_block(payload: dict) -> dict:
    conn = _conn()
    try:
        return {"ok": _move(conn, payload.get("id"), "blocked", payload.get("reason"))}
    finally:
        conn.close()


def cmd_unblock(payload: dict) -> dict:
    conn = _conn()
    try:
        task = kdb.get_task(conn, payload.get("id"))
        if not task or task.status not in ("blocked", "scheduled"):
            raise ValueError("task is not blocked/scheduled")
        return {"ok": bool(kdb.unblock_task(conn, payload.get("id")))}
    finally:
        conn.close()


def cmd_delete(payload: dict) -> dict:
    """UI delete = archive (reversible, hidden from board). Hard delete only
    for already-archived tasks via 'purge'."""
    task_id = payload.get("id")
    if not task_id:
        raise ValueError("id is required")
    conn = _conn()
    try:
        return {"ok": bool(kdb.archive_task(conn, task_id))}
    finally:
        conn.close()


def cmd_purge(payload: dict) -> dict:
    conn = _conn()
    try:
        return {"ok": bool(kdb.delete_archived_task(conn, payload.get("id")))}
    finally:
        conn.close()


COMMANDS = {"list": cmd_list, "show": cmd_show, "create": cmd_create, "update": cmd_update,
            "comment": cmd_comment, "assign": cmd_assign, "block": cmd_block,
            "unblock": cmd_unblock, "delete": cmd_delete, "purge": cmd_purge}


def main() -> None:
    raw = sys.stdin.read()
    try:
        req = json.loads(raw) if raw.strip() else {}
        cmd = req.get("cmd")
        fn = COMMANDS.get(cmd)
        if fn is None:
            raise ValueError(f"unknown command: {cmd}")
        result = fn(req.get("payload") or {})
        print(json.dumps({"ok": True, "result": result}))
    except Exception as exc:  # noqa: BLE001 — bridge must always emit valid JSON
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
