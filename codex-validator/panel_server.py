from collections import OrderedDict
import contextlib
import json
from pathlib import Path
import re
import sqlite3
import sys

import history
import review_items
import validate


PANEL_URI = "ui://codex-validator/report"
PANEL_MIME = "text/html;profile=mcp-app"
RESOURCE = {"uri": PANEL_URI, "name": "اعتبارسنجی مستقل", "mimeType": PANEL_MIME}
ACTIVITY_ID = re.compile(r"(?:[0-9a-f]{20}|history)\.[0-9a-f]{64}")
THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
RECENT_REVIEWS = OrderedDict()
TOOLS = [
    {
        "name": "validation_panel", "title": "اعتبارسنجی مستقل",
        "description": "Open validation for the Codex conversation hosting this panel.",
        "inputSchema": {"type": "object", "additionalProperties": False},
        "_meta": {"ui": {"resourceUri": PANEL_URI, "visibility": ["app"]},
                   "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]}},
    },
    {
        "name": "validation_report", "description": "Read or rerun the review for this conversation's latest activity.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["activity_id"],
                        "properties": {"activity_id": {"type": "string"}, "recheck": {"type": "boolean"}}},
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "validation_models", "description": "Save the user's reviewer model choices.",
        "inputSchema": {"type": "object", "additionalProperties": False, "required": ["models"],
                        "properties": {"models": {"type": "array", "minItems": 1, "maxItems": 2,
                                                  "items": {"type": "string"}}}},
        "_meta": {"ui": {"visibility": ["app"]}},
    },
]


def active_evidence(turn):
    return turn.get("completed_evidence") or turn.get("latest_evidence")


def get_turns(database, thread_id):
    if not database.is_file() or database.is_symlink():
        return
    with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        rows = db.execute(
            """SELECT id, started, data FROM turns
               WHERE json_extract(data, '$.session') = ? ORDER BY started DESC""",
            (thread_id,),
        )
        for turn_id, started, data in rows:
            turn = json.loads(data)
            if not turn.get("root") or not active_evidence(turn):
                continue
            yield turn_id, started, turn
            return


def latest_turns(thread_id):
    if not thread_id:
        return []
    latest = None
    for database in validate.evidence_databases():
        # An old or still-initializing store must not prevent model selection or other reports.
        with contextlib.suppress(OSError, sqlite3.Error, ValueError):
            for turn_id, started, turn in get_turns(database, thread_id):
                prefer_new = latest is None or latest[2] < started
                if latest and latest[1] == turn_id:
                    # In older stores, a tool workdir could split one turn into two project records.
                    captured = validate.has_prompt(turn)
                    earlier_captured = validate.has_prompt(latest[3])
                    if captured != earlier_captured:
                        prefer_new = captured
                if prefer_new:
                    latest = (database, turn_id, started, turn)
    with contextlib.suppress(OSError, sqlite3.Error, ValueError, KeyError, TypeError, RuntimeError):
        latest = history.latest(thread_id, latest) or latest
    return [latest] if latest else []


def activity_key(database, turn_id):
    return f"{database.parent.name if database else 'history'}.{turn_id}"


def activities(thread_id):
    return [
        {"id": activity_key(database, turn_id), "started": started,
         "project": Path(turn["root"]).name, "chat": turn["session"][-8:],
         "chatKey": validate.digest(turn["session"]),
         "evidenceKey": validate.digest(json.dumps(active_evidence(turn))),
         "request": " ".join(validate.request_text(
             turn.get("earlier_work", {}).get("request") or turn["request"]).split())[:100] or "درخواست ثبت نشده"}
        for database, turn_id, started, turn in latest_turns(thread_id)
    ]


def model_options(saved):
    options = {}
    # shortcut: use the current on-device catalog, use a live provider picker if that cache is removed.
    try:
        cache = validate.codex_home() / "models_cache.json"
        entries = json.loads(cache.read_text(encoding="utf-8")).get("models", [])
        for item in entries:
            if not isinstance(item, dict) or item.get("visibility") != "list":
                continue
            slug = item.get("slug")
            if isinstance(slug, str) and slug:
                options[slug] = item.get("display_name") or slug
    except (OSError, ValueError, AttributeError, TypeError, RuntimeError):
        pass
    for slug in saved:
        options.setdefault(slug, slug)
    return [{"id": slug, "label": label} for slug, label in options.items()]


def display_changes(parts):
    return [
        {"path": c["path"], "status": c["status"], "incomplete": bool(c.get("incomplete"))}
        for part in parts for c in part.get("activity_changes", [])
    ]


def report(activity_id, recheck, *, thread_id):
    if not thread_id:
        raise ValueError("اعتبارسنجی را از پنل کنار همان گفت‌وگو باز کن.")
    if not ACTIVITY_ID.fullmatch(activity_id):
        raise ValueError("شناسهٔ فعالیت معتبر نیست.")
    entries = latest_turns(thread_id)
    if not entries or activity_key(*entries[0][:2]) != activity_id:
        raise ValueError("این فعالیت در گزارش فعلی این گفت‌وگو در دسترس نیست. تازه‌سازی را بزن.")
    database, turn_id, started, turn = entries[0]
    evidence = active_evidence(turn)
    evidence_id = validate.digest(json.dumps(evidence))
    review_key = (activity_id, evidence_id)
    if recheck:
        if database:
            with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                root = Path(turn["root"])
                if "earlier_work" not in evidence:
                    earlier = validate.continuation_context(db, root, turn["session"], turn["request"], started)
                    if earlier:
                        evidence = {**evidence, "earlier_work": earlier}
                if "completed_evidence" in turn:
                    receipts = validate.recheck_tool_receipts(
                        db, turn_id, thread_id, root, evidence.get("tool_evidence", []),
                    )
                    evidence = {**evidence, "tool_evidence": receipts}
        evidence = history.with_confirmed_proposal(evidence, thread_id, turn_id)
        result = validate.evaluate(evidence)
        if "output" in result:
            # shortcut: retain manual results in this process, persist them if cross-restart history is needed.
            RECENT_REVIEWS[review_key] = (evidence, result)
            RECENT_REVIEWS.move_to_end(review_key)
            if len(RECENT_REVIEWS) > 16:
                RECENT_REVIEWS.popitem(last=False)
    elif review_key in RECENT_REVIEWS:
        evidence, result = RECENT_REVIEWS[review_key]
    elif database:
        with contextlib.closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT data FROM reports WHERE turn = ?", (turn_id,)).fetchone()
        stored = json.loads(row[0]) if row else {}
        result = stored.get("result", {}) if stored.get("evidence_id") == evidence_id else {}
    else:
        result = {}
    earlier = evidence.get("earlier_work", {})
    earlier_parts = earlier.get("parts", [])
    parts = [*earlier_parts, evidence]
    receipts = [{**tool, "beforeResume": part is not evidence}
                for part in parts for tool in part.get("tool_evidence", [])]
    checks = []
    output = result.get("output", {})
    for key in validate.CHECKS:
        if key not in output:
            continue
        entry = output[key]
        status = validate.display_status(entry)
        checks.append({"label": validate.CHECKS[key], "status": status,
                       "verdict": validate.STATUSES[status], "reason": entry["reason"]})
    catalog = review_items.evidence_catalog(evidence, validate.request_text(evidence.get("request", "")))
    requirements, references = review_items.panel_requirements(
        result.get("requirements", output.get("requirements")), catalog
    )
    return {
        "activity": {"id": activity_id, "chatKey": validate.digest(thread_id), "evidenceKey": evidence_id},
        "author": evidence.get("author_model"),
        "reviewer": result.get("reviewer_model"),
        "error": result.get("error"),
        "fromHistory": database is None,
        "phase": "آخرین نوبت کامل" if "completed_evidence" in turn else "آخرین عملیات ثبت‌شده",
        "limitedFileCoverage": evidence.get("git_workspace") is False,
        "sharedWorkspace": evidence.get("shared_workspace", False),
        "continued": bool(earlier_parts),
        "confirmation": validate.is_confirmation_request(evidence.get("request", "")),
        "confirmedProposal": evidence.get("confirmed_proposal"),
        "olderPartsOmitted": earlier.get("older_parts_omitted", False),
        "sources": list(dict.fromkeys(source["path"] for part in parts for source in part.get("sources", []))),
        "hasSourceRecord": any("sources" in part for part in parts),
        "changes": display_changes([evidence]),
        "earlierChanges": display_changes(earlier_parts),
        "tests": [{**tool, "followups": review_items.test_followups(tool)}
                  for tool in receipts if tool.get("test_command_family") and (
            tool.get("direct_test_command") or tool.get("test_summary") or type(tool.get("exit_code")) is int)],
        "installations": [tool["plugin_install"] for tool in receipts if tool.get("plugin_install")],
        "verifications": [{**check, "label": validate.VERIFICATION_SCOPES[check["scope"]]}
                          for tool in receipts for check in tool.get("verifications", [])],
        "checks": checks,
        "summaryChecks": review_items.panel_summary_checks(checks) if not requirements else [],
        "requirements": requirements,
        "evidenceReferences": references,
    }


def call_tool(name, args, *, thread_id=None):
    if name == "validation_panel":
        saved = validate.reviewer_models()
        data = {"threadBound": bool(thread_id), "activities": activities(thread_id),
                "models": saved, "modelOptions": model_options(saved)}
    elif name == "validation_report":
        data = {"report": report(args["activity_id"], args.get("recheck", False), thread_id=thread_id)}
    elif name == "validation_models":
        validate.save_models(args["models"])
        data = {"models": validate.reviewer_models()}
    else:
        raise ValueError("Unknown validation tool.")
    return {"content": [], "_meta": data}


def dispatch(method, params):
    if method == "initialize":
        version = params.get("protocolVersion")
        supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
        return {"protocolVersion": version if version in supported else supported[-1],
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "codex-validator", "version": "0.1.23"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "resources/list":
        return {"resources": [RESOURCE]}
    if method in ("resources/templates/list", "prompts/list"):
        return {"resourceTemplates" if method.startswith("resources/") else "prompts": []}
    if method == "resources/read" and params.get("uri") == PANEL_URI:
        html = Path(__file__).with_name("panel.html").read_text(encoding="utf-8")
        return {"contents": [{"uri": PANEL_URI, "mimeType": PANEL_MIME, "text": html}]}
    if method == "tools/call":
        meta = params.get("_meta")
        thread_id = meta.get("threadId") if isinstance(meta, dict) else None
        if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
            thread_id = None
        return call_tool(params["name"], params.get("arguments") or {}, thread_id=thread_id)
    raise LookupError(method)


def serve():
    for stream in (sys.stdin, sys.stdout):
        stream.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if not isinstance(request, dict) or "id" not in request:
            continue
        response = {"jsonrpc": "2.0", "id": request["id"]}
        try:
            response["result"] = dispatch(request.get("method"), request.get("params") or {})
        except (ValueError, KeyError, TypeError, OSError, sqlite3.Error) as error:
            response["error"] = {"code": -32602, "message": str(error)}
        except LookupError:
            response["error"] = {"code": -32601, "message": "Method not found"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    serve()
