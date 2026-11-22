import argparse
import contextlib
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import benchmark_review
import panel_server
import review_items
import validate


class ValidationTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="codex-review-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "repo"
        self.root.mkdir()
        self.folder = Path(temp.name) / "state"
        self.folder.mkdir()
        validate.git(self.root, "init", "-q")

    def put(self, path, content):
        (self.root / path).write_text(content, encoding="utf-8")

    def event(self, kind, *, panel_only=False, **args):
        event = {"hook_event_name": kind, "session_id": "test-chat", "turn_id": "test-turn",
                 "model": "author-model", **args}
        validate.handle_hook(event, self.root, self.folder, "reviewer-model", panel_only=panel_only)

    def test_reviews_only_this_activity_and_preserves_failed_test_evidence(self):
        self.put("app.py", "print('before')\n")
        self.put("other.py", "print('committed')\n")
        validate.git(self.root, "add", ".")
        validate.git(self.root, "-c", "user.name=Validation test", "-c", "user.email=test@example.invalid",
                     "commit", "--no-gpg-sign", "-qm", "baseline")
        self.put("other.py", "print('unrelated working change')\n")
        self.put(".env", "PRIVATE=excluded\n")

        report = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        with patch("validate.review", return_value=report) as reviewer:
            self.event("UserPromptSubmit", prompt="Edit app.py only and run pytest.")
            self.put("app.py", "print('after')\n")
            self.event("PostToolUse", tool_use_id="patch", tool_name="apply_patch")
            first = reviewer.call_args.args[0]
            self.assertEqual([change["path"] for change in first["activity_changes"]], ["app.py"])
            self.assertIn("-print('before')", first["activity_changes"][0]["patch"])
            self.assertIn("+print('after')", first["activity_changes"][0]["patch"])

            self.event("PostToolUse", tool_use_id="test", tool_name="Bash",
                       tool_input={"command": "pytest"}, tool_response={"exit_code": 1})
            self.event("Stop", last_assistant_message="All tests passed.")
            final = reviewer.call_args.args[0]
            self.assertEqual(final["final_claim"], "All tests passed.")
            self.assertEqual(final["tool_evidence"][-1]["exit_code"], 1)
            self.assertTrue(final["tool_evidence"][-1]["direct_test_command"])
            self.assertNotIn("unrelated working change", json.dumps(final))
            self.assertNotIn("PRIVATE=excluded", json.dumps(final))

            self.put("app.py", "print('manual edit made after turn')\n")
            terminal = io.StringIO()
            with contextlib.redirect_stdout(terminal):
                result = validate.manual(argparse.Namespace(show=False, session=None, model="reviewer-model"), self.folder)
            self.assertEqual(result, 0)
            self.assertEqual(reviewer.call_args.args[0], final)
            self.assertIn("manual-review.md", terminal.getvalue())

    def test_empty_checkout_and_unavailable_reviewer(self):
        base = validate.base_tree(self.root)
        before = validate.snapshot(self.root, base)
        self.put("new.txt", "")
        changes = validate.activity_changes(self.root, base, before, validate.snapshot(self.root, base))
        self.assertEqual(changes[0]["status"], "added")
        with patch("validate.review", side_effect=RuntimeError("Model unavailable")):
            report, success = validate.run_review(self.folder, "Fixture", {"author_model": "author-model"}, "reviewer-model")
        self.assertFalse(success)
        self.assertIn("Model unavailable", report.read_text(encoding="utf-8"))

    def test_failed_test_followup_keeps_the_matching_case_error_and_line(self):
        command = "python -m unittest -v tests.test_widget"
        output = (
            "test_slow (tests.test_widget.WidgetTest) ... FAIL\n"
            "test_price (tests.test_widget.WidgetTest) ... ERROR\n"
            "======================================================================\n"
            "FAIL: test_slow (tests.test_widget.WidgetTest.test_slow)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_widget.py", line 14, in test_slow\n'
            "AssertionError: expected the quick path\n"
            "======================================================================\n"
            "ERROR: test_price (tests.test_widget.WidgetTest)\n"
            "----------------------------------------------------------------------\n"
            "Traceback (most recent call last):\n"
            '  File "tests/test_widget.py", line 22, in test_price\n'
            "ValueError: invalid quote, token=example-sensitive-value\n"
            "----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.010s\n\nFAILED (failures=1, errors=1)\n"
        )
        receipt = validate.tool_evidence({"tool_name": "Bash", "cwd": str(self.root),
                                         "tool_input": {"command": command},
                                         "tool_response": {"output": output, "exit_code": 1}}, self.root)
        self.assertEqual(receipt["test_invocation"], command)
        first, second = review_items.test_followups(receipt)
        self.assertEqual(first["case"], "tests.test_widget.WidgetTest.test_slow")
        self.assertIn("tests/test_widget.py:14 · AssertionError: expected the quick path", first["message"])
        self.assertNotIn("invalid quote", first["message"])
        self.assertIn("tests/test_widget.py:22 · ValueError: invalid quote", second["message"])
        self.assertNotIn("example-sensitive-value", json.dumps(receipt))
        self.assertEqual(first["message"].splitlines()[-1], review_items.TEST_REQUEST)
        self.assertEqual(second["message"].splitlines()[-1], review_items.TEST_REQUEST)
        self.assertLess(len(first["message"]), 250)

    def test_pytest_followup_and_missing_witness_are_grounded_in_recorded_evidence(self):
        event = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"},
                 "tool_response": {"output": "tests/test_cache.py:7: AssertionError\n"
                                   "FAILED tests/test_cache.py::test_cache - AssertionError: cache still present\n",
                                   "exit_code": 1}}
        test, = review_items.test_followups(validate.tool_evidence(event, self.root))
        self.assertIn("tests/test_cache.py:7 · AssertionError: cache still present", test["message"])
        self.assertTrue(test["message"].endswith(review_items.TEST_REQUEST))

        finding = {"requirement": "بررسی نتیجه", "reason": "ثبت اجرای واقعی ناقص است.",
                   "work_status": "unverified", "claim_status": "unverified", "claim": "انجام شد.",
                   "refusal_quote": "", "work_refs": [], "claim_refs": ["final_claim"]}
        catalog = review_items.evidence_catalog({"final_claim": "انجام شد."}, "")
        rows, _ = review_items.panel_requirements([finding], catalog)
        self.assertIn("شاهد کافی ثبت نشده", rows[0]["followup"])
        self.assertIn("همین مورد را بررسی کن", rows[0]["followup"])
        self.assertIn("شاهد کافی ثبت نشده", rows[0]["batchFollowup"])
        self.assertNotIn("همین مورد را بررسی کن", rows[0]["batchFollowup"])

    def test_short_confirmation_recovers_only_the_preceding_proposal_after_reopening(self):
        home = self.root.parent / "codex"
        logs = home / "sessions"
        logs.mkdir(parents=True)
        proposal = "پیشنهاد من افزودن دکمهٔ ارسال موارد انتخاب‌شده است."

        def record_log(day, session, turn_id, reply=None, user_message=None):
            stamp = f"2026-08-{day:02}T10:00:00Z"
            records = [{"type": "session_meta", "payload": {"id": session, "timestamp": stamp}},
                       {"type": "event_msg", "timestamp": stamp,
                        "payload": {"type": "task_started", "turn_id": turn_id}}]
            if user_message is not None:
                records.append({"type": "event_msg", "timestamp": stamp,
                                "payload": {"type": "item_completed", "turn_id": turn_id,
                                            "item": {"type": "UserMessage", "id": f"user-{day}",
                                                     "content": [{"type": "text", "text": user_message}]}}})
            if reply is not None:
                records.append({"type": "event_msg", "timestamp": stamp,
                                "payload": {"type": "task_complete", "turn_id": turn_id,
                                            "last_agent_message": reply}})
            path = logs / f"rollout-2026-08-{day:02}T10-00-00-test-chat_reopened-{day}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")
            return path

        record_log(1, "test-chat", "proposal-turn", proposal)
        note = "فقط موارد انتخابی ارسال شوند. منظورت همین است؟"
        record_log(2, "test-chat", "question-without-reply", user_message=note)
        record_log(3, "other-chat", "other-proposal", "Unrelated proposal from another chat.")
        current = record_log(4, "test-chat", "test-turn")
        record_log(5, "test-chat", "later-turn", "An answer that came after the confirmation.")
        with contextlib.closing(sqlite3.connect(home / "state_1.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id, rollout_path, cwd)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("test-chat", str(current), str(self.root)))

        with patch("validate.codex_home", return_value=home), \
                patch("validate.evaluate", return_value={"error": "Fixture"}) as reviewer:
            self.event("UserPromptSubmit", prompt="خوب پس همین کار کن", panel_only=True)
            self.event("Stop", last_assistant_message="دکمه اضافه شد.", panel_only=True)
        evidence = reviewer.call_args.args[0]
        self.assertEqual(evidence["request"], "خوب پس همین کار کن")
        self.assertEqual(evidence["confirmed_proposal"], proposal)
        self.assertEqual(evidence["confirmation_followups"], note)
        self.assertEqual(review_items.evidence_catalog(evidence, evidence["request"])["confirmed_proposal"]["value"], proposal)
        self.assertNotIn("earlier_work", evidence)
        self.assertEqual(evidence["activity_changes"], [])

    def test_same_model_is_never_called(self):
        with patch("validate.review") as reviewer, patch("validate.reviewer_models", return_value=["author-model"]):
            result = validate.evaluate({"author_model": "author-model"})
            self.assertIn("error", result)
            reviewer.assert_not_called()

    def test_resume_includes_the_unfinished_work_in_the_same_chat(self):
        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        state_home = self.folder
        self.folder = state_home / ("4" * 20)
        self.folder.mkdir()
        self.put("AGENTS.md", "Run tests before reporting completion.")
        self.put("brief.md", "The report must include install and test evidence.")
        resume = "میتونی از جای که You've hit your usage limit متوقف شدی ادامه بدی ؟"
        self.assertTrue(validate.is_continuation_request("میتونی از جای You've hit your usage limit ادامه بدی ؟"))
        self.assertFalse(validate.is_continuation_request("## آخرین نوبت کامل\n" + resume))
        with patch("validate.state_home", return_value=state_home), \
                patch("validate.reviewer_models", return_value=["reviewer-model"]), \
                patch("validate.review", return_value=verdict) as reviewer:
            self.event("UserPromptSubmit", prompt="Update the report using the instructions in \x60brief.md\x60.")
            original_edit = {"tool_use_id": "original-edit", "tool_name": "apply_patch",
                             "tool_input": {"command": "*** Begin Patch\n*** Add File: panel.html\n+snippet\n*** End Patch"}}
            self.event("PreToolUse", **original_edit)
            self.put("panel.html", "<title>Show evidence</title>\n")
            self.event("PostToolUse", **original_edit)
            self.event("PostToolUse", tool_use_id="original-test", tool_name="Bash",
                       tool_input={"command": "pytest"}, tool_response={"exit_code": 1})
            self.event("UserPromptSubmit", prompt="Work on a different feature.",
                       session_id="other-chat", turn_id="unrelated")

            self.event("UserPromptSubmit", prompt=resume, turn_id="resumed")
            resumed_edit = {"turn_id": "resumed", "tool_use_id": "resumed-edit", "tool_name": "apply_patch",
                            "tool_input": {"command": "*** Begin Patch\n*** Add File: validate.py\n+pass\n*** End Patch"}}
            self.event("PreToolUse", **resumed_edit)
            self.put("validate.py", "pass\n")
            self.event("PostToolUse", **resumed_edit)
            self.event("Stop", turn_id="resumed", last_assistant_message="Finished the report.")
            evidence = reviewer.call_args.args[0]
            self.assertEqual(evidence["request"], resume)
            self.assertIn("Update the report", evidence["earlier_work"]["request"])
            prior = evidence["earlier_work"]["parts"][0]
            self.assertEqual([change["path"] for change in prior["activity_changes"]], ["panel.html"])
            self.assertEqual({s["path"] for s in prior["sources"]}, {"AGENTS.md", "brief.md"})
            self.assertTrue(any(r.get("exit_code") == 1 for r in prior["tool_evidence"]))
            self.assertEqual([change["path"] for change in evidence["activity_changes"]], ["validate.py"])
            self.assertNotIn("different feature", json.dumps(evidence))
            activity = panel_server.activities("test-chat")[0]
            self.assertTrue(activity["request"].startswith("Update the report"))
            report = panel_server.report(activity["id"], False, thread_id="test-chat")
            self.assertTrue(report["continued"])
            self.assertEqual([change["path"] for change in report["earlierChanges"]], ["panel.html"])
            self.assertTrue(report["tests"][0]["beforeResume"])

            turn_id = validate.turn_key({"session_id": "test-chat", "turn_id": "resumed"})
            with contextlib.closing(validate.connect(self.folder)) as db, db:
                row = db.execute("SELECT data FROM turns WHERE id = ?", (turn_id,)).fetchone()
                legacy = json.loads(row[0])
                del legacy["earlier_work"]
                del legacy["completed_evidence"]["earlier_work"]
                db.execute("UPDATE turns SET data = ? WHERE id = ?", (json.dumps(legacy), turn_id))
            self.assertTrue(panel_server.report(activity["id"], True, thread_id="test-chat")["continued"])
            self.assertIn("earlier_work", reviewer.call_args.args[0])
            self.assertTrue(panel_server.report(activity["id"], False, thread_id="test-chat")["continued"])

            self.event("UserPromptSubmit", prompt=resume, turn_id="after-finished")
            self.event("Stop", turn_id="after-finished", last_assistant_message="No edits requested.")
            self.assertNotIn("earlier_work", reviewer.call_args.args[0])

    def test_reviewer_differs_from_both_models_on_resumed_work(self):
        evidence = {"author_model": "new-author", "earlier_work": {"author_models": ["earlier-author"]}}
        with patch("validate.review", return_value={}) as reviewer, \
                patch("validate.reviewer_models", return_value=["earlier-author", "independent-model"]):
            self.assertEqual(validate.evaluate(evidence)["reviewer_model"], "independent-model")
            self.assertEqual(reviewer.call_args.args[1], "independent-model")

    def test_shell_evidence_preserves_observed_test_and_install_output(self):
        separator = "-" * 70
        command = "python -B -m unittest discover -s . -p 'test_*.py' -v"
        event = {"tool_name": "Bash", "tool_input": {"command": command},
                 "tool_response": f"test_example ... ok\r\n{separator}\r\nRan 7 tests in 2.803s\r\n\r\nOK\r\n"}
        receipt = validate.tool_evidence(event)
        self.assertTrue(receipt["direct_test_command"])
        self.assertEqual(receipt["test_summary"], "Ran 7 tests in 2.803s · OK")
        self.assertEqual(receipt["test_cases"], [{"name": "test_example", "result": "ok"}])
        self.assertNotIn("exit_code", receipt)

        event["tool_response"] = f"{separator}\nRan 3 tests in 0.152s\n\nFAILED (failures=1)\n"
        receipt = validate.tool_evidence(event)
        self.assertEqual(receipt["test_summary"], "Ran 3 tests in 0.152s · FAILED (failures=1)")
        self.assertNotIn("exit_code", receipt)
        event["tool_input"] = {"command": "echo unittest"}
        self.assertNotIn("test_command_family", validate.tool_evidence(event))

        install = validate.tool_evidence({
            "tool_name": "Bash", "tool_input": {"command": "codex.exe plugin add example@local --json"},
            "tool_response": '{"pluginId":"example@local","version":"0.1.7","irrelevant":"private-fixture"}\r\n',
        })
        self.assertEqual(install["plugin_install"], {"plugin_id": "example@local", "version": "0.1.7"})
        self.assertNotIn("private-fixture", json.dumps(install))
        pretty_install = validate.tool_evidence({
            "tool_name": "Bash", "tool_input": {"command": "codex.exe plugin add 'example@local' --json"},
            "tool_response": "Installer warning\n" + json.dumps(
                {"pluginId": "example@local", "version": "0.1.19", "installedPath": "private-fixture"}, indent=2,
            ),
        })
        self.assertEqual(pretty_install["plugin_install"], {"plugin_id": "example@local", "version": "0.1.19"})
        self.assertNotIn("private-fixture", json.dumps(pretty_install))

    def test_reads_runner_exit_code_from_recorded_process_envelope(self):
        output = "-" * 70 + "\nRan 23 tests in 8.164s\n\nOK\n"
        home = self.root.parent / "codex"
        directory = home / "plugins" / "cache" / "local" / "example" / "0.1.19"
        directory.mkdir(parents=True)
        (directory / "plugin.json").write_text('{"name":"example","version":"0.1.19"}', encoding="utf-8")
        event = {"tool_name": "Bash", "tool_input": {"command": "python -B -X utf8 -m unittest -v",
                                                     "workdir": str(directory)},
                 "tool_response": json.dumps({"chunk_id": "123abc", "wall_time_seconds": 8.5,
                                              "exit_code": 0, "original_token_count": 450, "output": output})}
        with patch("validate.codex_home", return_value=home):
            receipt = validate.tool_evidence(event)
        self.assertEqual(receipt["test_package"], {"plugin_id": "example@local", "version": "0.1.19"})
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["test_summary"], "Ran 23 tests in 8.164s · OK")

    def test_captures_supported_verification_facts_without_raw_output(self):
        check = {"scope": "saved_activity", "check": "resumed_report", "result": "passed",
                 "facts": {"earlier_files": 6, "resumed_files": 2, "rewrote_records": False}}
        unsafe = {**check, "facts": {"raw_log": "private-fixture"}}
        event = {"tool_name": "Bash", "tool_input": {"command": "python verify.py"},
                 "tool_response": "unrelated private output\n"
                                  f"CODEX_VERIFICATION {json.dumps(check)}\n"
                                  f"CODEX_VERIFICATION {json.dumps(unsafe)}\n"}
        receipt = validate.tool_evidence(event)
        self.assertEqual(receipt["verifications"], [check])
        self.assertNotIn("private", json.dumps(receipt))

    def test_records_test_summary_after_powershell_encoding_setup(self):
        test_command = "python -B -X utf8 -m unittest discover -s work/codex-validator -v"
        encoding = "[Console]::OutputEncoding = [Text.Encoding]::UTF8"
        response = ("Chunk ID: fixture\nWall time: 0.2 seconds\nProcess exited with code 1\nFinal output:\n"
                    "test_result ... FAIL\n" + "-" * 70 + "\nRan 1 test in 0.012s\n\nFAILED (failures=1)\n")
        event = {"tool_name": "Bash", "tool_input": {"command": encoding + "\r\n" + test_command},
                 "tool_response": response}
        receipt = validate.tool_evidence(event)
        self.assertTrue(receipt["direct_test_command"])
        self.assertEqual(receipt["test_summary"], "Ran 1 test in 0.012s · FAILED (failures=1)")
        self.assertEqual(receipt["test_cases"], [{"name": "test_result", "result": "FAIL"}])
        self.assertEqual(receipt["exit_code"], 1)

        event["tool_input"]["command"] += "\nWrite-Output 'more work'"
        compound = validate.tool_evidence(event)
        self.assertFalse(compound["direct_test_command"])
        self.assertNotIn("test_summary", compound)

    def test_reads_exit_code_for_the_exact_recorded_command(self):
        home = self.root.parent / "codex"
        home.mkdir()
        with contextlib.closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_items (thread_id, turn_id, item_id, item_type, item_json)")
            rows = [
                ("other-chat", "test-turn", "test-matched", 99),
                ("test-chat", "other-turn", "test-matched", 98),
                ("test-chat", "test-turn", "test-matched", 1),
                ("test-chat", "test-turn", "someone-elses-command", 0),
            ]
            db.executemany("INSERT INTO thread_items VALUES (?, ?, ?, 'commandExecution', ?)",
                           [(chat, turn, item, json.dumps({"exitCode": code})) for chat, turn, item, code in rows])
        with patch("validate.codex_home", return_value=home), \
                patch("validate.evaluate", return_value={"error": "Fixture"}) as reviewer:
            self.event("UserPromptSubmit", prompt="Run two tests.")
            for tool_id in ("test-matched", "test-unmatched"):
                self.event("PostToolUse", tool_name="Bash", tool_use_id=tool_id,
                           tool_input={"command": "python -m unittest"},
                           tool_response="-" * 70 + "\nRan 1 test in 0.012s\n\nFAILED (failures=1)\n")
            self.event("Stop", last_assistant_message="Finished testing.", panel_only=True)
            test, unmatched = reviewer.call_args.args[0]["tool_evidence"]
            self.assertEqual(test.get("exit_code"), 1)
            self.assertNotIn("exit_code", unmatched)

    def test_recovers_exit_codes_from_exact_completed_events_in_the_same_rollout_turn(self):
        home = self.root.parent / "codex home"
        logs = home / "sessions"
        logs.mkdir(parents=True)
        stamp = "2026-08-01T10:00:00Z"
        command = "python -m unittest -v"
        install_command = "codex.exe plugin add 'example@local' --json"
        install_output = json.dumps({"pluginId": "example@local", "version": "0.1.19"}, indent=2)
        output = "-" * 70 + "\nRan 1 test in 0.011s\n\nOK\n"
        package = home / "plugins" / "cache" / "local" / "example" / "0.1.19"
        package.mkdir(parents=True)
        def record(turn, item_id, status, shell=command, stdout=output):
            return {"type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": {
                "type": "CommandExecution", "id": item_id,
                "command": ["C:/Program Files/PowerShell/7/pwsh.exe", "-Command", shell],
                "cwd": package.as_uri(),
                "exit_code": status, "aggregated_output": stdout,
            }}}

        records = [{"type": "session_meta", "payload": {"id": "test-chat", "timestamp": stamp}},
                   record("other-turn", "matched", 99),
                   record("other-turn", "unmatched", 98),
                   record("test-turn", "install", 0, install_command, install_output),
                   record("test-turn", "matched", 0)]
        (logs / "rollout-2026-08-01T10-00-00-test-chat_reopened.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8",
        )
        key = validate.turn_key({"session_id": "test-chat", "turn_id": "test-turn"})
        with patch("validate.codex_home", return_value=home), \
                patch("validate.evaluate", return_value={"error": "Fixture"}) as reviewer:
            self.event("UserPromptSubmit", prompt="Run both tests.")
            self.event("PostToolUse", tool_name="Bash", tool_use_id="install",
                       tool_input={"command": install_command}, tool_response="")
            for item_id in ("matched", "unmatched"):
                self.event("PostToolUse", tool_name="Bash", tool_use_id=item_id,
                           tool_input={"command": command}, tool_response=output)
            self.event("Stop", last_assistant_message="Tests finished.", panel_only=True)
            with contextlib.closing(validate.connect(self.folder)) as db:
                saved = validate.turn_tools(db, key)
                recovered = validate.recheck_tool_receipts(db, key, "test-chat", self.root, saved)
                self.assertEqual(validate.turn_tools(db, key), saved)
                unrelated = [saved[0], {**saved[1], "test_summary": "A different run."}, saved[2]]
                self.assertEqual(validate.recheck_tool_receipts(db, key, "test-chat", self.root, unrelated), unrelated)
        latest = reviewer.call_args.args[0]["tool_evidence"]
        self.assertEqual(latest[1]["exit_code"], 0)
        self.assertNotIn("exit_code", latest[2])
        self.assertEqual(recovered[0]["plugin_install"], {"plugin_id": "example@local", "version": "0.1.19"})
        self.assertEqual(recovered[1]["exit_code"], 0)
        self.assertEqual(recovered[1]["test_package"], recovered[0]["plugin_install"])
        self.assertNotIn("exit_code", recovered[2])

    def test_large_patch_retains_evidence_for_later_paths(self):
        self.put("a-large.txt", "x" * 59_000)
        self.put("z-required.py", "answer = 42\n")
        changes = validate.activity_changes(
            self.root, None, {}, validate.snapshot_files(self.root, ["a-large.txt", "z-required.py"]),
        )
        self.assertTrue(changes[0]["incomplete"])
        self.assertFalse(changes[1]["incomplete"])
        self.assertIn("+answer = 42", changes[1]["patch"])

    def test_records_an_edit_in_a_folder_without_git(self):
        local = self.root.parent / "no-git"
        local.mkdir()
        main_file = local / "app.py"
        main_file.write_text("value = 1\n", encoding="utf-8")
        unrelated = local / "unrelated.py"
        unrelated.write_text("unrelated text\n", encoding="utf-8")
        patch_command = ("*** Begin Patch\n*** Update File: app.py\n@@\n-value = 1\n+value = 2\n"
                         "*** Add File: .env\n+PRIVATE=excluded\n*** Add File: ../outside.py\n+outside = True\n*** End Patch")

        def send(name, **props):
            payload = {"hook_event_name": name, "cwd": str(local), "session_id": "local-chat",
                       "turn_id": "local-turn", "model": "author-model", **props}
            with patch.object(sys, "argv", ["validate.py", "--hook", "--model", "reviewer-model"]), \
                    patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                self.assertEqual(validate.main(), 0)

        tool = {"tool_use_id": "patch", "tool_name": "apply_patch", "tool_input": {"command": patch_command}}
        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        with patch("validate.state_home", return_value=self.folder), patch("validate.review", return_value=verdict) as reviewer:
            send("UserPromptSubmit", prompt="Update the value in app.py.")
            send("PreToolUse", **tool)
            main_file.write_text("value = 2\n", encoding="utf-8")
            (local / ".env").write_text("PRIVATE=excluded\n", encoding="utf-8")
            (local.parent / "outside.py").write_text("outside = True\n", encoding="utf-8")
            send("PostToolUse", **tool)
            send("Stop", last_assistant_message="Updated app.py.")
            choices = panel_server.activities("local-chat")
            self.assertEqual(len(choices), 1)
            self.assertEqual(panel_server.report(choices[0]["id"], False, thread_id="local-chat")["changes"],
                             [{"path": "app.py", "status": "modified", "incomplete": False}])
            changes = reviewer.call_args.args[0]["activity_changes"]
            self.assertIn("-value = 1", changes[0]["patch"])
            self.assertIn("+value = 2", changes[0]["patch"])
            self.assertNotIn("unrelated", json.dumps(changes))

    def test_panel_returns_model_choices_even_without_activities(self):
        home = self.root.parent / "user"
        catalog = home / ".codex" / "models_cache.json"
        catalog.parent.mkdir(parents=True)
        catalog.write_text(json.dumps({"models": [
            {"slug": "reviewer-model", "display_name": "Reviewer model", "visibility": "list"},
            {"slug": "alternate-model", "display_name": "Alternate model", "visibility": "list"},
            {"slug": "internal-model", "display_name": "Internal", "visibility": "hide"},
        ]}), encoding="utf-8")
        initializing = self.folder / ("2" * 20)
        initializing.mkdir()
        (initializing / "evidence.db").touch()
        with patch("validate.state_home", return_value=self.folder), patch("validate.reviewer_models", return_value=["reviewer-model"]), \
                patch.object(Path, "home", return_value=home):
            data = panel_server.call_tool("validation_panel", {})["_meta"]
        self.assertEqual(data["activities"], [])
        self.assertEqual(data["modelOptions"], [{"id": "reviewer-model", "label": "Reviewer model"},
                                                {"id": "alternate-model", "label": "Alternate model"}])

    def test_panel_and_report_follow_the_host_thread_across_projects_and_agents(self):
        other = self.root.parent / "other-project"
        other.mkdir()
        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        with patch("validate.state_home", return_value=self.folder), \
                patch("validate.reviewer_models", return_value=["reviewer-model"]), \
                patch("validate.review", return_value=verdict) as reviewer:
            for chat, root in (("agent-a", self.root), ("agent-b", self.root), ("agent-c", other)):
                context = {"session_id": chat, "turn_id": f"turn-{chat}", "model": "author-model", "cwd": str(root)}
                folder = validate.state_dir(root)
                validate.handle_hook({**context, "hook_event_name": "UserPromptSubmit", "prompt": f"Request for {chat}"},
                                     root, folder, None, panel_only=True, use_git=False)
                validate.handle_hook({**context, "hook_event_name": "Stop", "last_assistant_message": f"Result for {chat}"},
                                     root, folder, None, panel_only=True, use_git=False)

            def call(name, chat=None, **args):
                meta = {"threadId": chat} if chat else {}
                return panel_server.dispatch("tools/call", {"name": name, "arguments": args, "_meta": meta})["_meta"]

            first = call("validation_panel", "agent-a")["activities"]
            second = call("validation_panel", "agent-b", thread_id="agent-a")["activities"]
            third = call("validation_panel", "agent-c")["activities"]
            self.assertEqual([item["request"] for item in first], ["Request for agent-a"])
            self.assertEqual([item["request"] for item in second], ["Request for agent-b"])
            self.assertEqual([item["project"] for item in third], ["other-project"])
            self.assertEqual(call("validation_panel", "agent-unrecorded")["activities"], [])
            self.assertEqual(call("validation_panel", thread_id="agent-a")["activities"], [])
            self.assertEqual(call("validation_report", "agent-b", activity_id=second[0]["id"])["report"]["author"],
                             "author-model")
            reviewer.reset_mock()
            for chat in ("agent-b", "agent-c", None):
                with self.assertRaises(ValueError):
                    call("validation_report", chat, activity_id=first[0]["id"], recheck=True)
            reviewer.assert_not_called()

    def test_shared_git_folder_does_not_import_another_agents_file_edits(self):
        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        with patch("validate.review", return_value=verdict) as reviewer:
            self.event("UserPromptSubmit", prompt="Agent A changes only a.py.")
            self.event("UserPromptSubmit", prompt="Agent B changes only b.py.",
                       session_id="other-agent", turn_id="other-turn")
            for name, chat, turn in (("a.py", "test-chat", "test-turn"), ("b.py", "other-agent", "other-turn")):
                edit = {"session_id": chat, "turn_id": turn, "tool_name": "apply_patch", "tool_use_id": name,
                        "tool_input": {"command": f"*** Begin Patch\n*** Add File: {name}\n+pass\n*** End Patch"}}
                self.event("PreToolUse", **edit)
                self.put(name, "pass\n")
                self.event("PostToolUse", **edit)
            self.event("Stop", session_id="other-agent", turn_id="other-turn", last_assistant_message="Edited b.py.")
            self.assertEqual([change["path"] for change in reviewer.call_args.args[0]["activity_changes"]], ["b.py"])
            self.event("Stop", last_assistant_message="Edited a.py.")
            evidence = reviewer.call_args.args[0]
            self.assertEqual([change["path"] for change in evidence["activity_changes"]], ["a.py"])
            self.assertTrue(evidence["shared_workspace"])

    def test_old_hook_report_remains_visible(self):
        state = self.folder
        self.folder = state / ("7" * 20)
        self.folder.mkdir()
        with patch("validate.state_home", return_value=state), \
                patch("validate.codex_home", return_value=self.root / "codex"), \
                patch("validate.evaluate", return_value={"error": "Fixture"}):
            with patch("validate.time.time", return_value=time.time() - 172_800):
                self.event("UserPromptSubmit", prompt="Keep the recorded report.")
                self.event("Stop", last_assistant_message="Done.", panel_only=True)
            choices = panel_server.activities("test-chat")
            self.assertEqual([item["request"] for item in choices], ["Keep the recorded report."])
            self.assertEqual(panel_server.report(choices[0]["id"], False, thread_id="test-chat")["author"],
                             "author-model")

    def test_old_chat_history_recovers_only_the_latest_completed_turn(self):
        home = self.root.parent / "codex"
        logs = home / "archived_sessions"
        logs.mkdir(parents=True)
        rollout = logs / "rollout-2026-08-01T00-00-00-old-chat.jsonl"
        prefix_path = "\\\\?\\" if len(rollout.drive) == 2 else ""
        cwd = prefix_path + str(self.root)
        earlier = {"type": "session_meta", "payload": {"id": "old-chat", "cwd": cwd}}
        context = {"type": "turn_context", "payload": {"turn_id": "last-turn", "cwd": cwd,
                                                      "model": "old-author"}}
        prefix = (json.dumps(earlier) + "\n").encode()
        rollout.write_bytes(prefix + (json.dumps(context) + "\n").encode())
        with contextlib.closing(sqlite3.connect(home / "state_5.sqlite")) as db, db:
            db.execute("CREATE TABLE threads (id, rollout_path, cwd)")
            db.execute("INSERT INTO threads VALUES (?, ?, ?)", ("old-chat", prefix_path + str(rollout), cwd))
        with contextlib.closing(sqlite3.connect(home / "thread_history_1.sqlite")) as db, db:
            db.execute("CREATE TABLE thread_turns (thread_id, turn_id, status, rollout_ordinal, started_at, "
                       "rollout_byte_offset, rollout_end_byte_offset)")
            db.execute("CREATE TABLE thread_items (thread_id, turn_id, item_type, rollout_ordinal, item_json)")
            db.executemany("INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?)", [
                ("old-chat", "earlier-turn", "completed", 1, 1, 0, 0),
                ("old-chat", "last-turn", "completed", 2, 2, len(prefix), rollout.stat().st_size),
                ("old-chat", "interrupted-turn", "interrupted", 3, 3, 0, 0),
                ("other-chat", "last-turn", "completed", 4, 4, 0, 0),
            ])
            items = [
                {"type": "userMessage", "content": [{"type": "text", "text": "Edit only old.py."}]},
                {"type": "agentMessage", "phase": "final_answer", "text": "Edited old.py and ran tests."},
                {"type": "fileChange", "status": "completed", "changes": [
                    {"path": "old.py", "kind": {"type": "add"}, "diff": "print(2)"},
                    {"path": ".env", "kind": {"type": "add"}, "diff": "private-fixture"},
                ]},
                {"type": "commandExecution", "exitCode": 1,
                 "command": "\"C:/Program Files/PowerShell/7/pwsh.exe\" -Command '[Console]::OutputEncoding="
                            "[Text.Encoding]::UTF8\npython -B -m unittest'",
                 "aggregatedOutput": "Private command output"},
            ]
            db.executemany("INSERT INTO thread_items VALUES (?, ?, ?, ?, ?)", [
                ("old-chat", "last-turn", item["type"], ordinal, json.dumps(item))
                for ordinal, item in enumerate(items)
            ] + [("other-chat", "last-turn", "userMessage", 4,
                  json.dumps({"content": [{"type": "text", "text": "Other chat fixture"}]}))])
        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        with patch("validate.codex_home", return_value=home), patch("validate.state_home", return_value=self.folder), \
                patch("validate.reviewer_models", return_value=["old-author", "reviewer-model"]), \
                patch("validate.review", return_value=verdict) as review:
            choices = panel_server.activities("old-chat")
            self.assertEqual([item["request"] for item in choices], ["Edit only old.py."])
            self.assertEqual(panel_server.activities("old-chat"), choices)
            activity_id = choices[0]["id"]
            preview = panel_server.report(activity_id, False, thread_id="old-chat")
            self.assertTrue(preview["fromHistory"])
            self.assertEqual(preview["author"], "old-author")
            self.assertEqual(preview["changes"], [{"path": "old.py", "status": "added", "incomplete": False}])
            self.assertTrue(preview["tests"][0]["direct_test_command"])
            self.assertEqual(preview["tests"][0]["exit_code"], 1)
            review.assert_not_called()
            with self.assertRaises(ValueError):
                panel_server.report(activity_id, True, thread_id="other-chat")
            review.assert_not_called()
            report = panel_server.report(activity_id, True, thread_id="old-chat")
            self.assertEqual(report["reviewer"], "reviewer-model")
            self.assertEqual(panel_server.report(activity_id, False, thread_id="old-chat")["checks"], report["checks"])
            evidence, _ = review.call_args.args
            self.assertEqual(evidence["final_claim"], "Edited old.py and ran tests.")
            for forbidden in ("private-fixture", "Other chat fixture", "Private command output"):
                self.assertNotIn(forbidden, json.dumps(evidence))

    def test_review_stays_with_the_original_folder_and_gathers_task_sources(self):
        local = self.root.parent / "local-project"
        section = local / "section"
        section.mkdir(parents=True)
        (local / "AGENTS.md").write_text("Keep work confined to the requested section.", encoding="utf-8")
        (section / "AGENTS.md").write_text("Superseded rules", encoding="utf-8")
        (section / "AGENTS.override.md").write_text("Keep labels in Persian.", encoding="utf-8")
        (section / "SKILL.md").write_text("Check required outputs against the brief.", encoding="utf-8")
        (local / "brief.md").write_text("The label must be قابل بررسی.", encoding="utf-8")
        (local / "unrelated.md").write_text("Unrelated project text", encoding="utf-8")
        (local / ".env").write_text("PRIVATE=excluded", encoding="utf-8")

        def send(name, cwd, **details):
            payload = {"hook_event_name": name, "cwd": str(cwd), "session_id": "local-chat",
                       "turn_id": "local-turn", "model": "author-model", **details}
            with patch.object(sys, "argv", ["validate.py", "--hook", "--panel-only", "--model", "reviewer-model"]), \
                    patch.object(sys, "stdin", io.StringIO(json.dumps(payload))):
                self.assertEqual(validate.main(), 0)

        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        with patch("validate.state_home", return_value=self.folder), patch("validate.review", return_value=verdict) as reviewer:
            send("UserPromptSubmit", local, prompt="Add a label following `brief.md` and ignore `.env`.")
            send("PostToolUse", section, tool_use_id="read-skill", tool_name="Bash",
                 tool_input={"command": "Get-Content -LiteralPath 'SKILL.md'"})
            edit = {"tool_name": "apply_patch", "tool_use_id": "patch", "tool_input": {
                "command": "*** Begin Patch\n*** Add File: label.txt\n+قابل بررسی\n*** End Patch"}}
            send("PreToolUse", section, **edit)
            (section / "label.txt").write_text("قابل بررسی\n", encoding="utf-8")
            send("PostToolUse", section, **edit)
            send("Stop", local, last_assistant_message="Added the label.")
            self.assertEqual(len(list(self.folder.glob("*/evidence.db"))), 1)
            result = reviewer.call_args.args[0]
            self.assertEqual([c["path"] for c in result["activity_changes"]], ["section/label.txt"])
            self.assertEqual({item["path"] for item in result["sources"]},
                             {"AGENTS.md", "brief.md", "section/AGENTS.override.md", "section/SKILL.md"})
            self.assertNotIn("PRIVATE=excluded", json.dumps(result))
            self.assertNotIn("Unrelated project text", json.dumps(result))

    def test_records_later_read_windows_in_the_report(self):
        state = self.folder
        self.folder = state / ("3" * 20)
        self.folder.mkdir()
        prefix = "یادداشت آغاز فایل\n" * 550
        self.put("large.py", prefix + "def required_rule():\n    return True\nunselected_line\nlast_rule\n")
        self.put(".env", prefix + "PRIVATE=excluded\n")
        (self.root.parent / "neighbor.txt").write_text(prefix + "Unrelated project line", encoding="utf-8")
        command = (
            "Get-Content -LiteralPath 'large.py' | Select-Object -Skip 550 -First 2\n"
            "Get-Content -LiteralPath 'large.py' | Select-Object -Skip 553 -First 1\n"
            "Get-Content -LiteralPath '.env' | Select-Object -Skip 550 -First 1\n"
            "Get-Content -LiteralPath '../neighbor.txt' | Select-Object -Skip 550 -First 1"
        )
        with patch("validate.state_home", return_value=state), \
                patch("validate.evaluate", return_value={"error": "Fixture"}) as reviewer:
            self.event("UserPromptSubmit", prompt="Read the relevant rules in \x60large.py\x60.")
            self.event("PostToolUse", tool_name="Bash", tool_use_id="source-read",
                       tool_input={"command": command}, tool_response={"exit_code": 0})
            self.event("Stop", last_assistant_message="Read the selected rules.", panel_only=True)
            captured = reviewer.call_args.args[0]
            excerpts = {source["path"]: source["excerpt"].replace("\r\n", "\n") for source in captured["sources"]}
            self.assertEqual(excerpts.get("large.py:551-552"), "def required_rule():\n    return True\n")
            self.assertEqual(excerpts.get("large.py:554-554"), "last_rule\n")
            self.assertTrue(excerpts["large.py"].endswith("[Evidence truncated]"))
            self.assertNotIn("unselected_line", json.dumps(captured))
            self.assertNotIn("PRIVATE=excluded", json.dumps(captured))
            self.assertNotIn("Unrelated project line", json.dumps(captured))
            selected = panel_server.activities("test-chat")[0]
            report = panel_server.report(selected["id"], False, thread_id="test-chat")
            self.assertIn("large.py:551-552", report["sources"])

    def test_plugin_supplies_a_structured_panel_without_markdown(self):
        state_home = self.folder
        self.folder = state_home / ("1" * 20)
        self.folder.mkdir()
        verdict = {key: {"status": "unverified", "reason": "Fixture"} for key in validate.CHECKS}
        verdict["claims"]["partial"] = True
        with patch("validate.state_home", return_value=state_home), patch("validate.review", return_value=verdict):
            self.event("UserPromptSubmit", prompt="\n# Files mentioned by the user:\n\n"
                       "## example.png: C:/Temp/example.png\nImage attachment: true\n\n"
                       "## My request:\nAdd a helper.")
            self.put("helper.py", "pass\n")
            verification = {"scope": "app_server", "check": "recorded_panel", "result": "passed",
                            "facts": {"panel_ready": True}}
            self.event("PostToolUse", tool_use_id="verified-panel", tool_name="Bash", panel_only=True,
                       tool_input={"command": "python verify.py"},
                       tool_response="CODEX_VERIFICATION " + json.dumps(verification))
            self.event("Stop", last_assistant_message="Helper added.", panel_only=True)
            choices = panel_server.activities("test-chat")
            self.assertEqual(len(choices), 1)
            self.assertEqual(choices[0]["request"], "Add a helper.")
            output = panel_server.call_tool("validation_report", {"activity_id": choices[0]["id"]}, thread_id="test-chat")
            self.assertEqual(output["content"], [])
            report = output["_meta"]["report"]
            self.assertEqual(report["author"], "author-model")
            self.assertEqual(report["reviewer"], "reviewer-model")
            self.assertEqual(report["changes"], [{"path": "helper.py", "status": "added", "incomplete": False}])
            self.assertEqual(len(report["checks"]), 3)
            self.assertIsNone(report["requirements"])
            self.assertEqual(report["evidenceReferences"], {})
            self.assertEqual(report["checks"][2]["status"], "partial")
            self.assertEqual(report["verifications"][0]["facts"], {"panel_ready": True})
            self.assertFalse(list(self.folder.glob("*.md")))

            self.event("UserPromptSubmit", prompt="Another task", turn_id="new-turn")
            self.assertEqual(panel_server.activities("test-chat")[0]["id"], choices[0]["id"])
            self.event("Stop", last_assistant_message="Finished the new task.", turn_id="new-turn", panel_only=True)
            self.assertNotEqual(panel_server.activities("test-chat")[0]["id"], choices[0]["id"])
            with self.assertRaises(ValueError):
                panel_server.report(choices[0]["id"], True, thread_id="test-chat")

    def test_report_preserves_summary_actions_for_saved_general_checks(self):
        database_folder = self.folder / ("7" * 20)
        database_folder.mkdir()
        database = database_folder / "evidence.db"
        evidence = {"request": "Review the package.", "author_model": "author-model", "tool_evidence": []}
        key = validate.turn_key({"session_id": "test-chat", "turn_id": "legacy-overview"})
        turn = {"root": str(self.root), "session": "test-chat", "request": evidence["request"],
                "completed_evidence": evidence}
        reason = "اجرای رابط اصلی تأیید نشده است. token=private-fixture " + ("شرح تفصیلی " * 20)
        result = {"requirements": [], "output": {
            "instructions": {"status": "supported", "partial": False, "reason": "محدوده رعایت شد."},
            "claims": {"status": "unverified", "partial": True, "reason": reason},
        }}
        with contextlib.closing(validate.connect(database_folder)) as db, db:
            db.execute("INSERT INTO reports VALUES (?, ?)", (key, json.dumps({
                "evidence_id": validate.digest(json.dumps(evidence)), "result": result,
            })))
        with patch("panel_server.latest_turns", return_value=[(database, key, time.time(), turn)]):
            report = panel_server.report(panel_server.activity_key(database, key), False, thread_id="test-chat")
        self.assertEqual(report["requirements"], [])
        supported, unresolved = report["summaryChecks"]
        self.assertNotIn("followup", supported)
        self.assertEqual(unresolved["status"], "partial")
        self.assertLessEqual(len(unresolved["reason"]), 150)
        self.assertIn(review_items.NO_WITNESS, unresolved["followup"])
        self.assertNotIn("private-fixture", unresolved["batchFollowup"])
        self.assertEqual(report["checks"][1]["reason"], reason)

    def test_itemized_report_validates_and_preserves_exact_witnesses(self):
        _, scenario, expected = benchmark_review.SCENARIOS[0]
        evidence = {**scenario, "author_model": "author-model"}
        item = {
            "requirement": "اجرای تست‌های واحد", "work_status": "done", "claim_status": "contradicted",
            "claim": scenario["final_claim"], "reason": "تست‌ها اجرا شدند و یک تست شکست خورد.",
            "refusal_quote": "", "work_refs": ["tool_evidence[0]", "tool_evidence[99]"],
            "claim_refs": ["tool_evidence[0]"],
        }
        output = {key: {"status": "unverified", "reason": "Fixture", "partial": False} for key in validate.CHECKS}
        output["requirements"] = [item]
        state = self.folder
        self.folder = state / ("5" * 20)
        self.folder.mkdir()
        turn = {**evidence, "root": str(self.root), "session": "test-chat", "completed_evidence": evidence}
        key = validate.turn_key({"session_id": "test-chat", "turn_id": "item-test"})
        with contextlib.closing(validate.connect(self.folder)) as db, db:
            db.execute("INSERT INTO turns VALUES (?, ?, ?)", (key, time.time(), json.dumps(turn)))

        def reviewer(args, **kwargs):
            payload = json.loads(kwargs["input"].split("\nEVIDENCE\n", 1)[1])
            self.assertIn("tool_evidence[0]", payload["reference_index"])
            schema = json.loads(Path(args[args.index("--output-schema") + 1]).read_text(encoding="utf-8"))
            self.assertIn("requirements", schema["required"])
            destination = Path(args[args.index("--output-last-message") + 1])
            destination.write_text(json.dumps(output), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)

        with patch("validate.state_home", return_value=state), \
                patch("validate.codex_home", return_value=self.root), \
                patch("validate.reviewer_models", return_value=["reviewer-model"]), \
                patch("validate.shutil.which", return_value="fixture-codex"), \
                patch("validate.subprocess.run", side_effect=reviewer) as review_call:
            activity = panel_server.activities("test-chat")[0]
            report = panel_server.report(activity["id"], True, thread_id="test-chat")
            self.assertEqual(panel_server.report(activity["id"], False, thread_id="test-chat"), report)
            review_call.assert_called_once()
        row = report["requirements"][0]
        self.assertEqual(row["workLabel"], review_items.WORK_LABELS["done"])
        self.assertEqual(row["claimLabel"], review_items.CLAIM_LABELS["contradicted"])
        self.assertTrue(row["needsAttention"])
        self.assertEqual(row["references"], ["tool_evidence[0]", "final_claim"])
        self.assertEqual(row["workReferences"], ["tool_evidence[0]"])
        self.assertEqual(row["claimReferences"], ["tool_evidence[0]", "final_claim"])
        self.assertEqual(report["evidenceReferences"]["tool_evidence[0]"]["value"]["exit_code"], 1)
        self.assertEqual(len(report["checks"]), 3)
        parsed = review_items.normalize_requirements([item], review_items.evidence_catalog(evidence, evidence["request"]))
        self.assertTrue(benchmark_review.matches(parsed, expected))
        self.assertFalse(benchmark_review.matches([{**parsed[0], "work_status": "not_done"}], expected))

    def test_unproven_work_and_refusal_are_downgraded(self):
        reply = "همهٔ تست‌ها پاس شدند."
        catalog = review_items.evidence_catalog({"final_claim": reply}, "تست‌ها را اجرا کن.")
        row = {"requirement": "اجرای آزمون", "work_status": "not_done", "claim_status": "contradicted",
               "claim": reply, "refusal_quote": "", "reason": "نتیجه مستند است.",
               "work_refs": ["request", "missing"], "claim_refs": ["final_claim"]}
        missing, refused = review_items.normalize_requirements(
            [row, {**row, "work_status": "refused", "refusal_quote": "امتناع می‌کنم."}], catalog
        )
        self.assertEqual((missing["work_status"], missing["claim_status"]), ("unverified", "unverified"))
        self.assertEqual(missing["work_refs"], ["request"])
        self.assertEqual(refused["work_status"], "unverified")
        self.assertNotIn("نتیجه مستند است", refused["reason"])

        exact_reply = "از انجام این کار امتناع می‌کنم."
        explicit = review_items.evidence_catalog({"final_claim": exact_reply}, "این کار را انجام بده.")
        refusal = {**row, "work_status": "refused", "refusal_quote": exact_reply, "claim_status": "no_claim"}
        recorded = review_items.normalize_requirements([refusal], explicit)[0]
        self.assertEqual(recorded["work_status"], "refused")
        self.assertIn("final_claim", recorded["work_refs"])
        findings, _ = review_items.panel_requirements([recorded], explicit)
        self.assertEqual(findings[0]["refusalQuote"], exact_reply)


if __name__ == "__main__":
    unittest.main()
