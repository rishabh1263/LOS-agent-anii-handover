"""
LOGOUT ENDS A WAITING CONFIRM -- in the real browser (closure directive §D5, 2026-10-11).

    python tests/e2e/run_logout_e2e.py <seed.json> <out-dir>

An officer opens a synthetic case, asks to change its loan amount (the bot waits for Confirm), signs out with the
UI's own Sign out button, signs in again and says "yes". The loan amount is read back through the API: it must be
unchanged. Exit code 0 only when it is. Same isolated stack and synthetic officer as run_ui_e2e.py.
"""
from __future__ import annotations

import json
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_ui_e2e as E  # noqa: E402


def open_chat(page):
    """Log in, then reach the assistant from whichever screen the app lands on."""
    page.goto(E.UI, wait_until="networkidle")
    if page.locator("#login-username").count():
        page.select_option("#login-stage", "FOS")
        page.fill("#login-username", E.USER)
        page.fill("#login-password", E.PASSWORD)
        page.click("button[type=submit]")
    create = page.get_by_text("Create new case")
    try:
        create.click(timeout=20000)
    except Exception:  # noqa: BLE001 - the app restored another screen: the assistant is reachable from it too
        pass
    page.get_by_label("Open AI Assistant. Drag to reposition.").click(force=True, timeout=60000)
    page.get_by_label("Message input").wait_for(timeout=60000)


def main(seed_path: str, out: str) -> int:
    seed = json.load(open(seed_path, encoding="utf-8"))
    case_a = seed["A"]["case_id"]
    os.makedirs(out, exist_ok=True)
    token = E.officer_token()
    before = E.loan_amount(token, case_a)[0].split(".")[0]
    result = {"case": case_a, "amount_before": before, "steps": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1366, "height": 900})
        chat = E.Chat(ctx.new_page(), out, "logout")
        open_chat(chat.page)
        x = {"_token": token, "A": seed["A"]}
        chat.say(f"open {case_a}", [E.names("A")], "logout", x)
        chat.say("change the loan amount to 777777", [E.has("Confirm", "confirm")], "logout", x,
                 shot="proposal_before_logout")
        # the UI awaits POST /api/v1/auth/logout before clearing the session; so does this test -- navigating away
        # earlier aborts the request (the server never hears of the logout)
        with chat.page.expect_response(lambda r: "/api/v1/auth/logout" in r.url, timeout=120000) as logged_out:
            chat.page.get_by_label("Sign out").click(timeout=30000)
        result["logout_status"] = logged_out.value.status
        chat.page.wait_for_load_state("networkidle", timeout=60000)
        chat.page.screenshot(path=os.path.join(out, "logout_signed_out.png"))
        result["signed_out_login_form_shown"] = bool(chat.page.locator("#login-username").count())
        open_chat(chat.page)
        chat.say("yes", [E.amount_is(before)], "logout", x, shot="yes_after_relogin")
        browser.close()
    result["amount_after"] = E.loan_amount(token, case_a)[0].split(".")[0]
    result["steps"] = chat.turns
    result["passed"] = (result["amount_after"] == before and result.get("logout_status") == 204
                        and all(t["passed"] for t in chat.turns))
    json.dump(result, open(os.path.join(out, "logout_e2e.json"), "w", encoding="utf-8"), indent=1,
              ensure_ascii=False)
    print("LOGOUT E2E", "PASS" if result["passed"] else "FAIL", "amount", before, "->", result["amount_after"],
          "| logout HTTP", result.get("logout_status"), "| login form after sign out:",
          result["signed_out_login_form_shown"])
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else os.path.join("runs", "sprint5", "e2e")))
