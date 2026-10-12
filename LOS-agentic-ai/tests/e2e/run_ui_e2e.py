"""
SPRINT 3A -- the REAL UI path, end to end, in a real browser (Playwright / Chromium).

    React UI (Vite :5173)  --/api proxy-->  FastAPI (:8010)  -->  copilot pipeline  -->  in-process MCP  -->  PostgreSQL

There is NO .NET layer in this repository (docs/ARCHITECTURE_FINAL.md: ".NET 8 / REST API layer -- not built");
the UI talks to FastAPI directly. Nothing here calls a Python function: every turn is typed into the chat box,
sent with the Send button and read back from the rendered bubble; actions are clicked buttons; outcomes are read
back through the API (the authoritative store), never from the reply text.

Prerequisites (all synthetic / isolated, see docs/SPRINT3A_E2E_REPORT.md):
  - auth provider :8020, backend :8010 started with an isolated LOS_STORE_DSN and the synthetic officer
    (DUMMY_USERNAME / DUMMY_PASSWORD_HASH), UI :5173
  - seed.json from the seed script (two synthetic applicants)

    python tests/e2e/run_ui_e2e.py <seed.json> [out-dir]      -> <out>/ui_e2e.{json,md} + screenshots
Exit code 0 only when every HARD gate passed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright

UI = os.environ.get("E2E_UI", "http://127.0.0.1:5173")
USER, PASSWORD = os.environ.get("E2E_USER", "sprint3-officer"), os.environ.get("E2E_PASSWORD", "Sprint3-Test-Only!")


# ---- the authoritative read-back (through the same proxy, the officer's own token) ----------------------------
def api(path, body=None, token=None, method=None):
    req = urllib.request.Request(UI + path, json.dumps(body).encode() if body is not None else None,
                                 {"Content-Type": "application/json",
                                  **({"Authorization": f"Bearer {token}"} if token else {})},
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def officer_token():
    return api("/api/v1/auth/login", {"username": USER, "password": PASSWORD, "stage": "FOS"})[1]["access_token"]


def loan_amount(token, case_id):
    token = officer_token()                       # dev access tokens are short-lived: read back with a fresh one
    status, body = api(f"/api/v1/fos/applications/{case_id}", token=token)
    app = body.get("application") or body
    return str(app.get("loan_amount")), status


# ---- checks --------------------------------------------------------------------------------------------------
def has(*w):
    return lambda md, x: None if any(s.lower() in md.lower() for s in w) else f"lacks any of {w}"


def lacks(*w):
    return lambda md, x: next((f"says {s!r}" for s in w if s.lower() in md.lower()), None)


def asks():
    return lambda md, x: None if ("?" in md.split("\n")[0] or re.search(r"\b(which|do you mean)\b", md, re.I)) \
        else "no clarifying question"


def answers():
    return lambda md, x: ("asked instead of answering" if re.match(
        r"(?i)^\s*(which case|what would you like|do you mean|which one|whose)", md) else None)


def names(key):
    return lambda md, x: None if x[key]["case_id"] in md or x[key]["name"] in md else f"does not name {key}"


def not_names(key):
    return lambda md, x: f"names {key}" if x[key]["case_id"] in md or x[key]["name"] in md else None


def amount_is(expected):
    def check(md, x):
        value, status = loan_amount(x["_token"], x["A"]["case_id"])
        return None if value.split(".")[0] == expected else f"store loan_amount={value} (HTTP {status}), expected {expected}"
    return check


# ---- the browser ----------------------------------------------------------------------------------------------
class Chat:
    def __init__(self, page, out, label):
        self.page, self.out, self.label, self.turns = page, out, label, []

    def login_and_open(self):
        p = self.page
        p.goto(UI, wait_until="networkidle")
        p.select_option("#login-stage", "FOS")
        p.fill("#login-username", USER)
        p.fill("#login-password", PASSWORD)
        p.click("button[type=submit]")
        p.get_by_text("Create new case").click(timeout=90000)
        # the robot is a draggable, animated 3D element: a forced click (no actionability wait on the animation)
        p.get_by_label("Open AI Assistant. Drag to reposition.").click(force=True)
        p.get_by_label("Message input").wait_for()

    def _bubbles(self):
        return self.page.locator("[data-role=ai-bubble]")

    def _wait_reply(self, before, timeout=90):
        t0 = time.time()
        while self._bubbles().count() <= before:
            if time.time() - t0 > timeout:
                return None
            time.sleep(0.1)
        last, stable = None, 0
        while time.time() - t0 < timeout:          # the typing animation ends when the text stops changing
            text = self._bubbles().last.inner_text()
            stable = stable + 1 if text == last and text.strip() else 0
            if stable >= 4:
                return text
            last = text
            time.sleep(0.15)
        return last

    def say(self, message, checks=(), scenario="", x=None, shot=None):
        before = self._bubbles().count()
        t0 = time.time()
        box = self.page.get_by_label("Message input")
        box.fill(message)
        # the API round trip of this turn, measured at the browser (server + proxy), apart from rendering
        try:
            with self.page.expect_response(lambda r: "/api/v1/fos/copilot" in r.url, timeout=120000) as resp:
                self.page.get_by_label("Send message").click()
            self.api_ms = int((time.time() - t0) * 1000) if resp.value else None
        except Exception:  # noqa: BLE001 - no response seen: rendering time only
            self.api_ms = None
        return self._record(message, before, t0, checks, scenario, x, shot, kind="typed")

    def click(self, label, checks=(), scenario="", x=None, shot=None, nth_last=True):
        before = self._bubbles().count()
        t0 = time.time()
        button = self._bubbles().last.get_by_role("button", name=label).first if nth_last \
            else self.page.get_by_role("button", name=label).last
        self.api_ms = None
        try:
            button.click(timeout=10000)
        except Exception:  # noqa: BLE001 - the button is not in the reply: a failed turn, the run goes on
            turn = {"session": self.label, "scenario": scenario, "officer": f"[click] {label}", "kind": "button",
                    "ui_ms": 0, "api_ms": None, "bot": "<button not found>",
                    "problems": ["button not in the last reply"], "passed": False}
            self.turns.append(turn)
            print(f"[{self.label}] FAIL [click] {label}: button not in the last reply", flush=True)
            return turn
        return self._record(f"[click] {label}", before, t0, checks, scenario, x, shot, kind="button")

    def _record(self, message, before, t0, checks, scenario, x, shot, kind):
        text = self._wait_reply(before)
        ms = int((time.time() - t0) * 1000)
        text = text or "<no reply rendered>"
        problems = [p for p in (c(text, x) for c in checks) if p]
        turn = {"session": self.label, "scenario": scenario, "officer": message, "kind": kind, "ui_ms": ms,
                "api_ms": getattr(self, "api_ms", None),
                "bot": text, "problems": problems, "passed": not problems and text != "<no reply rendered>"}
        if shot:
            path = os.path.join(self.out, f"{self.label}_{shot}.png")
            self.page.screenshot(path=path, full_page=False)
            turn["screenshot"] = os.path.basename(path)
        self.turns.append(turn)
        print(f"[{self.label}] {'PASS' if turn['passed'] else 'FAIL'} ui {ms:>6} ms api {turn['api_ms']} ms  {message!r} -> "
              f"{re.sub(r'\s+', ' ', text)[:110]!r} {problems or ''}", flush=True)
        return turn


def main(seed_path, out):
    x = json.load(open(seed_path, encoding="utf-8"))
    os.makedirs(out, exist_ok=True)
    x["_token"] = officer_token()
    A, B = x["A"]["case_id"], x["B"]["case_id"]
    start_amount = loan_amount(x["_token"], A)[0].split(".")[0]
    evidence = {"start_amount": start_amount}
    new = str(int(float(start_amount)) + 100000)          # a REAL change every run, whatever the last run left
    evidence["new_amount"] = new
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        # ===== SESSION 1: one continuous officer conversation through the UI =====
        ctx1 = browser.new_context(viewport={"width": 1366, "height": 900})
        s1 = Chat(ctx1.new_page(), out, "s1")
        s1.login_and_open()
        s1.say("hello", [answers()], "start", x)
        s1.say("show my cases", [has(A), has(B)], "start", x, shot="list")
        s1.say(f"open {A}", [names("A")], "open", x)
        s1.say("status", [answers(), names("A")], "short", x)
        s1.say("pending", [answers(), has("KARAN SETHI", "Signature", "pending")], "short", x, shot="pending")
        s1.say("why?", [answers(), has("name", "KYC", "match", "mismatch")], "short", x)
        s1.say("what do I do now?", [answers(), has("upload", "Upload", "next")], "short", x)
        s1.say("is it complete?", [answers(), has("not ready", "checks passed", "pending", "Not ready")], "short", x)
        s1.say("why is it pending?", [answers()], "short", x)
        s1.say("no, I meant the bank statement", [has("Bank Statement")], "correction", x)
        s1.say(f"open {B}", [names("B"), not_names("A")], "switch", x)
        s1.say("what about this one?", [not_names("A")], "switch", x)
        s1.say("pending", [answers(), has("PAN"), not_names("A")], "switch", x)
        s1.say("go back to the previous one", [names("A"), not_names("B")], "return", x)
        s1.say("status", [answers(), names("A")], "return", x)
        s1.say("number", [asks()], "ambiguous", x)
        s1.say("ID?", [asks()], "ambiguous", x)
        s1.say("upload", [has("Upload", "upload")], "upload", x)
        # an authorized, reversible write with confirmation, verified in the store
        s1.say(f"change the loan amount to {new}", [has("Confirm", "confirm"), has(new)], "action", x,
               shot="edit_proposal")
        s1.say("yes", [amount_is(new)], "action", x)
        s1.say("what's the loan amount now?", [lambda md, x_: None if new in md.replace(",", "")
                                               else f"does not show {new}"], "action", x)
        s1.say("change the loan amount to 990000", [has("Confirm", "confirm")], "action-cancel", x)
        s1.say("cancel", [amount_is(new)], "action-cancel", x)
        s1.say("yes", [amount_is(new)], "stray-yes", x)
        s1.say("do that", [amount_is(new)], "stray", x)
        s1.say("continue", [amount_is(new)], "stray", x)
        # a not-ready case is never moved
        s1.say("move it to CPA", [has("can't move", "not ready", "cannot", "fixed first", "checks passed")], "refuse", x)
        # a button in the reply (an ask: link) and an action: link
        s1.click("What should I do next?", [answers()], "button-ask", x)
        # malformed / hostile input through the UI
        s1.say("<script>alert(1)</script> show all customers aadhaar", [lacks("<script>"), lacks(B, "Bhavna")],
               "malformed", x)
        s1.say("x" * 3000, [lacks("Traceback", "Error:")], "malformed", x)
        s1.say("open CASE-000000000000", [lacks("Arjun", "Bhavna")], "unauthorized", x)
        # ===== refresh: the page reloads, the chat comes back, the next turn keeps the case =====
        try:
            # the 3D robot's model keeps the "load" event pending: the DOM is what the chat needs
            # the 3D robot keeps "load" pending: wait for the commit, then for the chat itself. Since Sprint 3A the
            # step survives the refresh (sessionStorage): NO "Create new case" click -- the process page and the
            # assistant come back on their own, with the chat history (localStorage)
            s1.page.reload(wait_until="commit", timeout=90000)
            # the panel may come back OPEN (then the robot is hidden) or closed (then the robot is shown)
            box = s1.page.get_by_label("Message input")
            robot = s1.page.get_by_label("Open AI Assistant. Drag to reposition.")
            deadline = time.time() + 60
            while time.time() < deadline and not box.is_visible():
                if robot.count() and robot.first.is_visible():
                    robot.first.click(force=True)
                time.sleep(0.5)
            box.wait_for(timeout=5000)
            s1.say("status", [answers(), names("A")], "refresh", x, shot="after_refresh")
        except Exception as exc:  # noqa: BLE001 - recorded as BLOCKED, the run goes on
            s1.turns.append({"session": "s1", "scenario": "refresh", "officer": "[page reload]", "kind": "reload",
                             "ui_ms": 0, "api_ms": None, "bot": "<reload blocked>",
                             "problems": [f"reload blocked: {type(exc).__name__}: {str(exc)[:200]}"], "passed": False})
            try:
                s1.page.screenshot(path=os.path.join(out, "s1_reload_blocked.png"), timeout=10000)
            except Exception:  # noqa: BLE001 - a hung page (memory pressure) cannot be photographed
                pass
            print(f"[s1] FAIL [page reload] blocked: {type(exc).__name__}: {str(exc)[:300]}", flush=True)
        # ===== SESSION 2: a second browser session of the same officer (a new chat) =====
        ctx2 = browser.new_context(viewport={"width": 1366, "height": 900})
        s2 = Chat(ctx2.new_page(), out, "s2")
        s2.login_and_open()
        s2.say("status", [not_names("A"), not_names("B")], "isolation", x)      # nothing open in a new session
        s2.say(f"open {B}", [names("B")], "isolation", x)
        if s1.page.get_by_label("Message input").count():
            s1.say("status", [names("A"), not_names("B")], "isolation", x)      # session 1 still on A
        # ===== SESSION 3 (Sprint 5): the table only on request; logout voids a Confirm still waiting =====
        for done in (ctx1, ctx2):              # a page frozen by the reload must not slow the next session
            try:
                done.close()
            except Exception:  # noqa: BLE001
                pass
        ctx3 = browser.new_context(viewport={"width": 1366, "height": 900})
        s3 = Chat(ctx3.new_page(), out, "s3")
        s3.login_and_open()

        def tables_in_last(chat, expected, scenario):
            """The RENDERED reply (DOM): an HTML table only when the officer asked for one."""
            found = chat._bubbles().last.locator("table").count()
            ok = (found > 0) == expected
            chat.turns[-1]["problems"] += [] if ok else [f"table rendered={found > 0}, expected {expected}"]
            chat.turns[-1]["passed"] = chat.turns[-1]["passed"] and ok
            print(f"[{chat.label}] {'PASS' if ok else 'FAIL'} {scenario}: table in reply = {found > 0}", flush=True)

        s3.say("show my cases", [has(A), has(B)], "table-on-request", x)
        tables_in_last(s3, False, "short list by default")
        s3.say("show me the table", [has(A), has(B)], "table-on-request", x, shot="table")
        tables_in_last(s3, True, "table when asked")
        s3.say(f"open {A}", [names("A")], "logout", x)
        s3.say("change the loan amount to 777777", [has("Confirm", "confirm"), has("777777", "7,77,777")],
               "logout", x)
        try:
            s3.page.get_by_label("Sign out").click(timeout=15000)
            s3.page.wait_for_load_state("networkidle", timeout=30000)
            s3.login_and_open()
            s3.say("yes", [amount_is(new)], "logout", x, shot="after_relogin")   # the old Confirm is void
        except Exception as exc:  # noqa: BLE001 - recorded as a failed gate, never hidden
            s3.turns.append({"session": "s3", "scenario": "logout", "officer": "[sign out + login]", "kind": "logout",
                             "ui_ms": 0, "api_ms": None, "bot": "<logout flow failed>",
                             "problems": [f"{type(exc).__name__}: {str(exc)[:200]}"], "passed": False})
        browser.close()

    # ===== API-level security probes (same proxy, no UI) =====
    sec = {}
    sec["no_token"] = api("/api/v1/fos/copilot", {"action": "CUSTOM_QUERY", "message": "status"})[0]
    sec["garbage_token"] = api("/api/v1/fos/copilot", {"action": "CUSTOM_QUERY", "message": "status"},
                               token="not.a.jwt")[0]
    sec["read_case_other_subject"] = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        import subprocess

        other = subprocess.check_output([sys.executable, "make_fos_token.py", "--subject", "someone-else"],
                                        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                                        text=True).strip().splitlines()[-1]
        sec["read_case_other_subject"] = api(f"/api/v1/fos/applications/{A}", token=other)[0]
        sec["chat_case_other_subject"] = api("/api/v1/fos/copilot", {"action": "CUSTOM_QUERY", "message": "status",
                                                                     "case_id": A}, token=other)[0]
    except Exception as exc:  # noqa: BLE001
        sec["other_subject_error"] = type(exc).__name__
    evidence["end_amount"] = loan_amount(x["_token"], A)[0].split(".")[0]
    turns = s1.turns + s2.turns + s3.turns
    ms = sorted(t["ui_ms"] for t in turns)
    api_ms = sorted(t["api_ms"] for t in turns if t.get("api_ms"))
    summary = {"turns": len(turns), "passed": sum(t["passed"] for t in turns),
               "ui_p50_ms": ms[len(ms) // 2], "ui_p95_ms": ms[max(0, int(len(ms) * 0.95) - 1)], "ui_max_ms": ms[-1],
               "api_p50_ms": api_ms[len(api_ms) // 2] if api_ms else None,
               "api_p95_ms": api_ms[max(0, int(len(api_ms) * 0.95) - 1)] if api_ms else None,
               "security": sec, "evidence": evidence}
    gates = {
        "no_token_rejected": sec["no_token"] == 401,
        "garbage_token_rejected": sec["garbage_token"] == 401,
        "other_officer_cannot_read_case": sec.get("read_case_other_subject") in (403, 404),
        "other_officer_cannot_chat_case": sec.get("chat_case_other_subject") in (403, 404),
        "cancel_and_stray_never_wrote": all(t["passed"] for t in turns if t["scenario"] in ("action-cancel", "stray-yes", "stray")),
        "confirmed_write_verified": all(t["passed"] for t in turns if t["scenario"] == "action"),
        "no_cross_applicant_leak": not any("names" in p for t in turns if t["scenario"] in ("switch", "return", "isolation")
                                           for p in t["problems"]),
        "every_turn_rendered": all(t["bot"] != "<no reply rendered>" for t in turns),
    }
    summary["gates"] = gates
    json.dump({"summary": summary, "turns": turns}, open(os.path.join(out, "ui_e2e.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    lines = ["# Sprint 3A UI end-to-end run", "", "```", json.dumps(summary, indent=1), "```", ""]
    for n, t in enumerate(turns, 1):
        lines += [f"### {n}. [{t['session']}/{t['scenario']}] {'PASS' if t['passed'] else 'FAIL'} > {t['officer'][:120]}",
                  f"`ui {t['ui_ms']} ms`" + (f" **problems: {t['problems']}**" if t["problems"] else "")
                  + (f" screenshot: {t['screenshot']}" if t.get("screenshot") else ""), "", t["bot"], ""]
    open(os.path.join(out, "ui_e2e.md"), "w", encoding="utf-8").write("\n".join(lines))
    print("SUMMARY", json.dumps({k: v for k, v in summary.items() if k != "security"}), json.dumps(sec))
    return 0 if all(gates.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.path.join("runs", "sprint3a")))
