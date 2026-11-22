from contextlib import closing, nullcontext, suppress
from datetime import datetime
import json
from pathlib import Path
import re
import shlex
import sqlite3
from urllib.parse import urlsplit
from urllib.request import url2pathname

import validate
import review_items


def open_projection(home, name):
    versions = []
    for path in home.glob(f"{name}_*.sqlite"):
        version = path.stem.rsplit("_", 1)[-1]
        if version.isdecimal() and not path.is_symlink():
            versions.append((int(version), path))
    if not versions:
        return nullcontext(None)
    path = max(versions)[1]
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1))


def command_exit_codes(thread_id, turn_id, item_ids):
    missing = set(item_ids)
    with suppress(OSError, sqlite3.Error), open_projection(validate.codex_home(), "thread_history") as history:
        for item_id in item_ids:
            if history is None:
                break
            row = history.execute(
                """SELECT json_extract(item_json, '$.exitCode') FROM thread_items
                   WHERE thread_id = ? AND turn_id = ? AND item_id = ? AND item_type = 'commandExecution'
                     AND json_type(item_json, '$.exitCode') = 'integer'""",
                (thread_id, turn_id, item_id),
            ).fetchone()
            if row:
                missing.remove(item_id)
                yield item_id, row[0]
    if missing:
        key = validate.turn_key({"session_id": thread_id, "turn_id": turn_id})
        for item_id, item in rollout_commands(thread_id, key, missing):
            if type(item.get("exit_code")) is int:
                yield item_id, item["exit_code"]


def local_path(value):
    if isinstance(value, str) and value.startswith("file:"):
        uri = urlsplit(value)
        if uri.netloc in ("", "localhost") and not uri.query and not uri.fragment:
            value = url2pathname(uri.path)
    path = Path(value)
    if path.drive.startswith("\\\\?\\"):
        drive = path.drive[4:]
        if drive.casefold().startswith("unc\\"):
            return Path("\\\\" + str(path)[8:])
        if len(drive) == 2 and drive.endswith(":"):
            return Path(str(path)[4:])
    return path


def log_timestamp(value):
    if isinstance(value, str):
        with suppress(ValueError):
            stamp = datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
            if stamp.tzinfo is not None:
                return stamp
    return None


def conversation_rollouts(home, thread_id):
    if not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", thread_id):
        return []
    matches = []
    for directory in (home / "sessions", home / "archived_sessions"):
        for file in directory.rglob(f"rollout-*-{thread_id}*.jsonl"):
            with suppress(OSError, ValueError, RuntimeError, TypeError, KeyError):
                if file.is_symlink() or not file.resolve().is_relative_to(directory):
                    continue
                with file.open("rb") as stream:
                    first = json.loads(stream.readline(4_000_000))
                if first["type"] == "session_meta" and first["payload"]["id"] == thread_id:
                    stamp = log_timestamp(first["payload"].get("timestamp"))
                    if stamp is not None:
                        matches.append((stamp, file))
    return sorted(matches, reverse=True)


def rollout_commands(thread_id, target_key, item_ids):
    missing = set(item_ids)
    if not missing:
        return
    for _, file in conversation_rollouts(validate.codex_home(), thread_id):
        with suppress(OSError, ValueError, TypeError, KeyError, RuntimeError), file.open("rb") as stream:
            for line in stream:
                if b'"CommandExecution"' not in line or b'"item_completed"' not in line:
                    continue
                event = json.loads(line)
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                item = payload.get("item", {})
                if (event["type"] != "event_msg" or payload.get("type") != "item_completed"
                        or not isinstance(item, dict) or item.get("type") != "CommandExecution"
                        or item.get("id") not in missing or not isinstance(payload.get("turn_id"), str)):
                    continue
                if validate.turn_key({"session_id": thread_id, "turn_id": payload["turn_id"]}) != target_key:
                    continue
                missing.remove(item["id"])
                yield item["id"], item
                if not missing:
                    return


def command_evidence(item, root):
    command = item.get("command")
    if isinstance(command, list) and command and all(isinstance(arg, str) for arg in command):
        launcher = Path(command[0]).stem.casefold()
        if len(command) == 3 and launcher in ("pwsh", "powershell") and command[1].casefold() == "-command":
            command = command[2]
        else:
            command = shlex.join(command)
    return validate.tool_evidence({
        "tool_name": "Bash", "tool_input": {"command": command},
        "cwd": local_path(item["cwd"]) if item.get("cwd") else root,
        "tool_response": {"exit_code": item.get("exit_code", item.get("exitCode")),
                          "output": item.get("aggregated_output", item.get("aggregatedOutput"))},
    }, root)


def scan_rollout(file, thread_id, target_key, *, before=None):
    active_turn = None
    reply = None
    messages = {}
    with file.open("rb") as stream:
        for line in stream:
            if b'"event_msg"' not in line or not any(marker in line for marker in (
                b'"task_started"', b'"task_complete"', b'"UserMessage"',
            )):
                continue
            event = json.loads(line)
            if event.get("type") != "event_msg":
                continue
            stamp = log_timestamp(event.get("timestamp"))
            if before is not None and (stamp is None or stamp >= before):
                continue
            payload = event["payload"]
            turn_id = payload.get("turn_id")
            if not isinstance(turn_id, str) or not turn_id:
                continue
            if payload["type"] == "task_started":
                if target_key is not None and validate.turn_key({"session_id": thread_id, "turn_id": turn_id}) == target_key:
                    return True, stamp, reply, messages
                active_turn = turn_id
            elif turn_id == active_turn and payload["type"] == "task_complete":
                answer = payload.get("last_agent_message")
                if isinstance(answer, str) and answer.strip():
                    reply = (stamp, answer)
                    messages.clear()
            elif turn_id == active_turn and payload["type"] == "item_completed":
                item = payload.get("item", {})
                if isinstance(item, dict) and item.get("type") == "UserMessage" and isinstance(item.get("id"), str):
                    text = "\n".join(part["text"] for part in item.get("content", [])
                                     if isinstance(part, dict) and part.get("type") == "text"
                                     and isinstance(part.get("text"), str))
                    if text.strip():
                        messages[item["id"]] = (stamp, text)
    return False, None, reply, messages


def reply_before_confirmation(thread_id, target_key):
    rollouts = conversation_rollouts(validate.codex_home(), thread_id)
    for index, (_, file) in enumerate(rollouts):
        found, stamp, reply, messages = scan_rollout(file, thread_id, target_key)
        if not found:
            continue
        if reply is not None:
            return reply[1], [text for _, text in messages.values()]
        if stamp is None:
            return None
        # Reopening a conversation can start a new rollout file with the confirmation as its first turn.
        candidates = []
        for _, earlier in rollouts[index + 1:]:
            _, _, earlier_reply, earlier_messages = scan_rollout(earlier, thread_id, None, before=stamp)
            if earlier_reply is not None:
                candidates.append(earlier_reply)
            messages.update(earlier_messages)
        if candidates:
            reply_time, answer = max(candidates, key=lambda item: item[0])
            following = sorted((sent, text) for sent, text in messages.values() if sent is not None and sent > reply_time)
            return answer, [text for _, text in following]
    return None


def projected_previous_reply(db, thread_id, turn_id):
    prior = db.execute(
        """SELECT earlier.turn_id, earlier.status FROM thread_turns current
           JOIN thread_turns earlier ON earlier.thread_id = current.thread_id
                                     AND earlier.rollout_ordinal < current.rollout_ordinal
           WHERE current.thread_id = ? AND current.turn_id = ?
           ORDER BY earlier.rollout_ordinal DESC LIMIT 1""",
        (thread_id, turn_id),
    ).fetchone()
    if not prior or prior[1] != "completed":
        return None
    row = db.execute(
        """SELECT json_extract(item_json, '$.text') FROM thread_items
           WHERE thread_id = ? AND turn_id = ? AND item_type = 'agentMessage'
             AND (json_extract(item_json, '$.phase') = 'final_answer'
                  OR json_extract(item_json, '$.phase') IS NULL)
           ORDER BY rollout_ordinal DESC LIMIT 1""",
        (thread_id, prior[0]),
    ).fetchone()
    return row[0] if row and isinstance(row[0], str) else None


def proposal_excerpt(reply):
    if reply and reply.strip():
        return validate.clip(review_items.SECRET_VALUE.sub("[مقدار حساس حذف شد]", reply).strip(), 4_000)
    return None


def confirmation_context(thread_id, target_key, request, *, native_turn_id=None):
    if not validate.is_confirmation_request(request):
        return {}
    history_context = None
    with suppress(OSError, ValueError, TypeError, KeyError, RuntimeError):
        history_context = reply_before_confirmation(thread_id, target_key)
    if history_context is None and native_turn_id is not None:
        with suppress(OSError, sqlite3.Error), open_projection(validate.codex_home(), "thread_history") as db:
            if db is not None:
                history_context = (projected_previous_reply(db, thread_id, native_turn_id), [])
    if not history_context or not (proposal := proposal_excerpt(history_context[0])):
        return {}
    notes = review_items.SECRET_VALUE.sub("[مقدار حساس حذف شد]", "\n\n".join(history_context[1])).strip()
    context = {"confirmed_proposal": proposal}
    if notes:
        context["confirmation_followups"] = notes if len(notes) <= 4_000 else "[Evidence truncated]\n" + notes[-4_000:]
    return context


def with_confirmed_proposal(evidence, thread_id, target_key):
    if evidence.get("confirmed_proposal"):
        return evidence
    return {**evidence, **confirmation_context(thread_id, target_key, evidence.get("request", ""))}


def turn_context(home, path, thread_id, turn_id, start, end):
    with suppress(OSError, ValueError, TypeError, KeyError, RuntimeError):
        path = local_path(path).resolve()
        if not any(path.is_relative_to(home / name) for name in ("sessions", "archived_sessions")):
            return {}
        if not (type(start) is int and type(end) is int and 0 <= start < end):
            return {}
        with path.open("rb") as stream:
            meta = json.loads(stream.readline(4_000_000))
            if meta["type"] != "session_meta" or meta["payload"]["id"] != thread_id:
                return {}
            stream.seek(start)
            # shortcut: inspect the first 4 MB for turn settings, use projected turn settings if available later.
            end = min(end, start + 4_000_000)
            while stream.tell() < end:
                line = stream.readline(end - stream.tell())
                if not line.endswith(b"\n"):
                    break
                row = json.loads(line)
                if row.get("type") == "turn_context" and row["payload"].get("turn_id") == turn_id:
                    return row["payload"]
    return {}


def latest(thread_id, captured):
    home = validate.codex_home()
    with open_projection(home, "thread_history") as history:
        if history is None:
            return
        row = history.execute(
            """SELECT turn_id, started_at, rollout_byte_offset, rollout_end_byte_offset
               FROM thread_turns WHERE thread_id = ? AND status = 'completed'
               ORDER BY rollout_ordinal DESC LIMIT 1""", (thread_id,),
        ).fetchone()
        if not row:
            return
        turn_id, started, start, end = row
        key = validate.turn_key({"session_id": thread_id, "turn_id": turn_id})
        if captured:
            same_turn = captured[1] == key
            if same_turn and captured[3].get("completed_evidence"):
                return
            if not same_turn and (started or 0) <= captured[2]:
                return
        with open_projection(home, "state") as state:
            if state is None:
                return
            indexed = state.execute("SELECT rollout_path, cwd FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if not indexed:
            return
        path, cwd = indexed
        context = turn_context(home, path, thread_id, turn_id, start, end)
        root = local_path(context.get("cwd") or cwd)
        if not root.is_absolute():
            return
        root = root.resolve()
        rows = history.execute(
            """SELECT item_json FROM thread_items WHERE thread_id = ? AND turn_id = ?
               AND item_type IN ('userMessage', 'agentMessage', 'fileChange', 'commandExecution')
               ORDER BY rollout_ordinal""", (thread_id, turn_id),
        )
        evidence = turn_evidence(root, context.get("model"), rows)
    evidence.update(confirmation_context(thread_id, key, evidence["request"], native_turn_id=turn_id))
    if not any(evidence[key] for key in ("request", "final_claim", "activity_changes", "tool_evidence")):
        return
    return (None, key, started, {"session": thread_id, "root": str(root), "request": evidence["request"],
                                "completed_evidence": evidence})


def turn_evidence(root, model, rows):
    requests, changes, receipts = [], [], []
    final = ""
    remaining = 64_000
    for raw, in rows:
        item = json.loads(raw)
        kind = item.get("type")
        if kind == "userMessage":
            requests.extend(content["text"] for content in item.get("content", []) if content.get("type") == "text")
        elif kind == "agentMessage" and item.get("phase") in (None, "final_answer"):
            final = item.get("text") or final
        elif kind == "commandExecution":
            receipts.append(command_evidence(item, root))
        elif kind == "fileChange" and item.get("status") == "completed":
            for change in item.get("changes", []):
                path = validate.project_path(root, root / local_path(change["path"]))
                status = {"add": "added", "update": "modified", "delete": "deleted"}.get(change["kind"]["type"])
                if path is None or not path.parts or status is None:
                    continue
                patch = change.get("diff")
                record = {"path": path.as_posix(), "status": status,
                          "incomplete": not isinstance(patch, str) or len(patch) > remaining}
                if isinstance(patch, str) and remaining:
                    record["patch"] = validate.clip(patch, remaining)
                    remaining -= min(remaining, len(patch))
                changes.append(record)
    return {
        "request": validate.clip("\n\n".join(requests), 9_000), "final_claim": validate.clip(final, 9_000),
        "author_model": model, "activity_changes": changes, "tool_evidence": receipts[-20:],
        "phase": "Codex history",
        "scope_note": "Evidence recovered from Codex's saved items in this turn. File changes are logged edits, "
                      "not full snapshots. Project instructions, original user requests or execution details "
                      "may be missing. Do not infer them from current files or other turns.",
    }
