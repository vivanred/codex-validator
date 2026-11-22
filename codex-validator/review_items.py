import re


NO_WITNESS = "شاهد کافی ثبت نشده"
TEST_REQUEST = "علت شکست همین تست را بررسی کن، همان تست را دوباره اجرا کن و نتیجه و کد خروج را بگو."
SECRET_VALUE = re.compile(
    r"\b(?:sk-|gh[pousr]_)[a-z0-9_-]{12,}|\bbearer\s+\S+|"
    r"\b(?:password|secret|token|api[-_ ]?key)\s*[:=]\s*\S+|"
    r"--(?:password|secret|token|api[-_]?key)\s+\S+",
    re.IGNORECASE,
)
WORK_LABELS = {
    "done": "انجام‌شده",
    "partial": "ناقص",
    "not_done": "انجام‌نشده",
    "blocked": "متوقف",
    "refused": "امتناع صریح",
    "unverified": "نامشخص",
}
CLAIM_LABELS = {
    "supported": "تأییدشده",
    "contradicted": "متناقض با شاهد",
    "unverified": "شاهد کافی نیست",
    "no_claim": "ادعایی ثبت نشده",
}
TEXT_FIELDS = ("requirement", "claim", "reason", "refusal_quote")
REFERENCE_FIELDS = ("work_refs", "claim_refs")
ITEM_FIELDS = {
    **dict.fromkeys(TEXT_FIELDS, {"type": "string"}),
    "work_status": {"type": "string", "enum": list(WORK_LABELS)},
    "claim_status": {"type": "string", "enum": list(CLAIM_LABELS)},
    **dict.fromkeys(REFERENCE_FIELDS, {"type": "array", "items": {"type": "string"}}),
}
REQUIREMENTS_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "additionalProperties": False,
              "properties": ITEM_FIELDS, "required": list(ITEM_FIELDS)},
}


def evidence_catalog(evidence, request):
    catalog = {}
    earlier = evidence.get("earlier_work", {})
    if earlier.get("request"):
        catalog["earlier_work.request"] = {
            "kind": "request", "label": "درخواست اولیه", "value": earlier["request"],
        }
    if request:
        catalog["request"] = {
            "kind": "request", "label": "درخواست ادامه" if earlier else "درخواست کاربر", "value": request,
        }
    if evidence.get("confirmed_proposal"):
        catalog["confirmed_proposal"] = {
            "kind": "request", "label": "پیشنهاد تأییدشده در همین گفت‌وگو",
            "value": evidence["confirmed_proposal"],
        }
    if evidence.get("confirmation_followups"):
        catalog["confirmation_followups"] = {
            "kind": "request", "label": "پیام‌های کاربر پس از پیشنهاد",
            "value": evidence["confirmation_followups"],
        }
    if evidence.get("final_claim"):
        catalog["final_claim"] = {
            "kind": "reply", "label": "پاسخ ثبت‌شدهٔ نویسنده", "value": evidence["final_claim"],
        }
    parts = [(f"earlier_work.parts[{i}].", part) for i, part in enumerate(earlier.get("parts", []))]
    for prefix, part in [*parts, ("", evidence)]:
        for field, kind in (("sources", "source"), ("activity_changes", "change"), ("tool_evidence", "tool")):
            for i, item in enumerate(part.get(field, [])):
                label = (item.get("path") or item.get("test_runner") or item.get("test_command_family")
                         or item.get("plugin_install", {}).get("plugin_id") or item.get("tool") or "رخداد ثبت‌شده")
                catalog[f"{prefix}{field}[{i}]"] = {
                    "kind": kind, "label": label, "value": item, "beforeResume": bool(prefix),
                }
    return catalog


def normalize_requirements(items, catalog):
    if not isinstance(items, list):
        raise RuntimeError("The reviewer returned invalid requirement findings.")
    reply = catalog.get("final_claim", {}).get("value", "")
    normalized = []
    for row in items:
        if (not isinstance(row, dict) or set(row) != set(ITEM_FIELDS)
                or any(not isinstance(row[key], str) for key in (*TEXT_FIELDS, "work_status", "claim_status"))
                or row["work_status"] not in WORK_LABELS or row["claim_status"] not in CLAIM_LABELS
                or any(not isinstance(row[key], list) or any(not isinstance(ref, str) for ref in row[key])
                       for key in REFERENCE_FIELDS)
                or not row["requirement"].strip()):
            raise RuntimeError("The reviewer returned an invalid requirement finding.")
        item = {**row, **{key: row[key].strip() for key in TEXT_FIELDS}}
        for key in REFERENCE_FIELDS:
            item[key] = list(dict.fromkeys(ref for ref in row[key] if ref in catalog))
        missing = []
        if item["work_status"] == "refused":
            quote = item["refusal_quote"]
            if quote and quote in reply:
                item["work_refs"] = list(dict.fromkeys(["final_claim", *item["work_refs"]]))
            else:
                item["work_status"] = "unverified"
                missing.append("عبارت صریح امتناع در پاسخ ثبت‌شده پیدا نشد.")
        if item["work_status"] != "unverified" and not any(
            catalog[ref]["kind"] in ("change", "tool", "reply") for ref in item["work_refs"]
        ):
            item["work_status"] = "unverified"
            missing.append("وضعیت کار شاهد مستقیمی ندارد.")
        if item["work_status"] != "refused":
            item["refusal_quote"] = ""
        if item["claim_status"] == "no_claim":
            item["claim"], item["claim_refs"] = "", []
        elif not item["claim"] or item["claim"] not in reply:
            item["claim_status"], item["claim"] = "unverified", ""
            missing.append("متن ادعا در پاسخ ثبت‌شده پیدا نشد.")
        else:
            item["claim_refs"] = list(dict.fromkeys([*item["claim_refs"], "final_claim"]))
            if item["claim_status"] in ("supported", "contradicted") and not any(
                catalog[ref]["kind"] in ("source", "change", "tool") for ref in item["claim_refs"]
            ):
                item["claim_status"] = "unverified"
                missing.append("برای داوری دربارهٔ ادعا شاهد کافی ثبت نشده است.")
        if missing:
            item["reason"] = " ".join(missing)
        normalized.append(item)
    return normalized


def short_evidence(value, limit):
    text = " ".join(value.split())
    text = SECRET_VALUE.sub("[مقدار حساس حذف شد]", text)
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def test_followups(receipt):
    failures = {entry["name"]: entry for entry in receipt.get("test_failures", [])}
    for case in receipt.get("test_cases", []):
        if case["result"] in ("FAIL", "ERROR", "unexpected success"):
            failures.setdefault(case["name"], {"name": case["name"]})
    summary = receipt.get("test_summary") or ""
    code = receipt.get("exit_code")
    if not failures and (" · FAILED" in summary or (
        receipt.get("direct_test_command") and " · OK" not in summary and type(code) is int and code > 0
    )):
        failures[None] = {}
    retries = []
    for name, record in failures.items():
        target = name or receipt.get("test_invocation") or receipt.get("test_runner") or receipt["test_command_family"]
        issue = ("پیگیری تست «" if name else "پیگیری اجرای ناموفق «") + short_evidence(target, 220) + "»."
        location = short_evidence(record.get("path", ""), 120)
        if location and record.get("line"):
            location += f":{record['line']}"
        error = short_evidence(record.get("error", ""), 125)
        outcome = short_evidence(summary, 85) if summary else f"کد خروج {code}" if type(code) is int else ""
        if not outcome and name:
            outcome = "تست‌گیرنده برای این مورد نتیجهٔ ناموفق ثبت کرده است"
        witness = " · ".join(dict.fromkeys(filter(None, (location, error or outcome)))) or NO_WITNESS
        if not error and witness != NO_WITNESS:
            witness += " · خط اصلی خطا ثبت نشده"
        retries.append({"case": name, "label": target, "witness": witness,
                        "message": f"{issue}\nشاهد: {witness}\n{TEST_REQUEST}"})
    return retries


def witness_line(entry, item):
    value, label = entry["value"], entry["label"]
    if entry["kind"] == "source":
        return f"گزیدهٔ ثبت‌شدهٔ {label}"
    if entry["kind"] == "change":
        change = {"added": "اضافه شده", "modified": "ویرایش شده", "deleted": "حذف شده"}
        return f"{label} · {change.get(value.get('status'), 'تغییر ثبت‌شده')}"
    if entry["kind"] == "reply" and item["refusal_quote"]:
        return f"عبارت ثبت‌شدهٔ نویسنده «{item['refusal_quote']}»"
    if entry["kind"] == "tool":
        if value.get("test_summary"):
            return f"{label} · {value['test_summary']}"
        if value.get("direct_test_command") and type(value.get("exit_code")) is int:
            return f"{label} · کد خروج {value['exit_code']}"
        if value.get("plugin_install"):
            return f"نصب ثبت‌شدهٔ {label} · نسخهٔ {value['plugin_install']['version']}"
        if value.get("verifications"):
            check = value["verifications"][0]
            return f"بررسی ثبت‌شدهٔ {check['check']} · {check['result']}"
    return ""


def finding_followup(item, entries):
    issue = "«" + short_evidence(item["requirement"], 110) + "»."
    if item["reason"]:
        issue += " " + short_evidence(item["reason"], 125)
    combined = " ".join(item[key] for key in ("requirement", "claim", "reason"))
    if re.search(r"\b(?:tests?|pytest|unittest)\b|تست|آزمون", combined, re.IGNORECASE):
        tests = [retry for entry in entries if entry["kind"] == "tool"
                 and entry["value"].get("test_command_family") for retry in test_followups(entry["value"])]
        named = [test for test in tests if test["label"] in combined]
        linked = named or tests
        if len(linked) == 1:
            test_message = linked[0]["message"]
            return {
                "followup": f"{short_evidence('پیگیری ' + issue, 130)}\n{test_message}",
                "batchFollowup": f"{short_evidence(issue, 130)}\n{test_message}",
            }
    witness = next((line for line in (witness_line(entry, item) for entry in entries) if line), NO_WITNESS)
    body = f"{issue}\nشاهد: {short_evidence(witness, 160)}"
    return {
        "followup": f"پیگیری {body}\nهمین مورد را بررسی کن و نتیجه را با شاهد بگو.",
        "batchFollowup": body,
    }


def panel_summary_checks(checks):
    summary = []
    for check in checks:
        item = {**check, "reason": short_evidence(check["reason"], 150)}
        if item["status"] != "supported":
            item.update(finding_followup({
                "requirement": item["label"], "reason": item["reason"] or item["verdict"],
                "claim": "", "refusal_quote": "",
            }, ()))
        summary.append(item)
    return summary


def panel_requirements(items, catalog):
    if items is None:
        return None, {}
    findings, cited = [], {}
    for item in items:
        work_refs = [ref for ref in item["work_refs"] if ref in catalog]
        claim_refs = [ref for ref in item["claim_refs"] if ref in catalog]
        refs = list(dict.fromkeys([*work_refs, *claim_refs]))
        cited.update((ref, catalog[ref]) for ref in refs)
        work, claim = item["work_status"], item["claim_status"]
        needs_attention = work != "done" or claim in ("contradicted", "unverified")
        finding = {
            "requirement": item["requirement"], "workLabel": WORK_LABELS[work],
            "claimLabel": CLAIM_LABELS[claim], "claim": item["claim"], "reason": item["reason"],
            "references": refs, "workReferences": work_refs, "claimReferences": claim_refs,
            "refusalQuote": item["refusal_quote"],
            "needsAttention": needs_attention,
        }
        if needs_attention:
            evidence_refs = list(dict.fromkeys([*claim_refs, *refs])) if claim == "contradicted" else refs
            finding.update(finding_followup(item, [catalog[ref] for ref in evidence_refs]))
        findings.append(finding)
    return findings, cited
