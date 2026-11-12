import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ResultsTable } from "../components/ResultsTable";
import { SummaryCard } from "../components/SummaryCard";
import { SearchBox } from "../pages/SearchPage";
import type { PlainSummary, RuleResult, Run } from "../types/api";
import { rupees, tone } from "../utils/format";

function respond(map: Record<string, unknown>, status = 200) {
  return vi.fn(async (url: string) => {
    const key = Object.keys(map).find((k) => url.includes(k));
    const body = key ? map[key] : { error: { code: "CASE_NOT_FOUND", message: "Not found", details: null } };
    return new Response(JSON.stringify(body), { status: key ? status : 404, headers: { "Content-Type": "application/json" } });
  });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const rule = (id: string, status: RuleResult["status"], extra: Partial<RuleResult> = {}): RuleResult => ({
  rule_id: id, rule_name: `Check ${id}`, status, reason: `${id} reason`, comparisons: [], evidence: [],
  missing_evidence: [], risk_flags: [], recommended_action: null, details: {}, ...extra,
});

const summary = (over: Partial<PlainSummary> = {}): PlainSummary => ({
  run_id: "RUN-1", text: "Rules-written summary.", source: "rules", pending: false, note: "", label: "Summary",
  checklist: [{ rule_id: "R1", name: "Bank details", status: "PASS", status_label: "OK", text: "Bank details match." },
              { rule_id: "R4", name: "Account belongs to customer", status: "REVIEW_REQUIRED",
                status_label: "Needs a person", text: "Waiting for the branch." }],
  ...over,
});

const run = { run_id: "RUN-1", checks: { checks_passed: 6, checks_applicable: 7 } } as unknown as Run;

describe("format", () => {
  it("groups rupees the Indian way without float arithmetic", () => {
    expect(rupees("125979.25")).toBe("₹1,25,979.25");
    expect(rupees("1000")).toBe("₹1,000.00");
    expect(rupees("12345678.9")).toBe("₹1,23,45,678.90");
    expect(rupees(null)).toBe("—");
  });
  it("maps statuses to tones", () => {
    expect(tone("PASS")).toBe("good");
    expect(tone("FAIL")).toBe("bad");
    expect(tone("REVIEW_REQUIRED")).toBe("warn");
    expect(tone("NOT_APPLICABLE")).toBe("neutral");
  });
});

describe("SearchBox", () => {
  it("never lists cases: only a search box", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<SearchBox onFound={() => {}} current={null} />);
    expect(screen.getByLabelText(/applicant ID/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();                     // nothing is loaded before a search
  });

  it("opens the one case that matches exactly", async () => {
    vi.stubGlobal("fetch", respond({ "/lookup": { case_id: "BV-2026-001" } }));
    const onFound = vi.fn();
    render(<SearchBox onFound={onFound} current={null} />);
    fireEvent.change(screen.getByLabelText(/applicant ID/), { target: { value: "APL-2026-10401" } });
    fireEvent.click(screen.getByText("Search"));
    await waitFor(() => expect(onFound).toHaveBeenCalledWith("BV-2026-001"));
  });

  it("says plainly when nothing matches, without any code", async () => {
    vi.stubGlobal("fetch", respond({}));
    render(<SearchBox onFound={() => {}} current={null} />);
    fireEvent.change(screen.getByLabelText(/applicant ID/), { target: { value: "APL-0000" } });
    fireEvent.click(screen.getByText("Search"));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("No case found");
    expect(alert.textContent).not.toMatch(/CASE_NOT_FOUND/);
  });
});

describe("SummaryCard", () => {
  it("shows the summary in plain words", async () => {
    vi.stubGlobal("fetch", respond({ "/summary": summary() }));
    render(<SummaryCard caseId="BV-2026-001" run={run} />);
    expect(await screen.findByTestId("summary-text")).toHaveTextContent("Rules-written summary.");
  });

  it("shows one point per sentence", async () => {
    vi.stubGlobal("fetch", respond({ "/summary": summary({ text: "4 of 6 checks passed. The account type differs. Send it back." }) }));
    render(<SummaryCard caseId="BV-2026-001" run={run} />);
    const list = await screen.findByTestId("summary-text");
    expect(list.querySelectorAll("li")).toHaveLength(3);
  });

  it("refreshes while a better summary is being prepared, then shows it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const calls = [summary({ pending: true }), summary({ source: "ai", text: "The written explanation." })];
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(calls.shift() ?? calls[0]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    render(<SummaryCard caseId="BV-2026-001" run={run} />);
    expect(await screen.findByText(/Updating/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    await waitFor(() => expect(screen.getByTestId("summary-text")).toHaveTextContent("The written explanation."));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows an error state", async () => {
    vi.stubGlobal("fetch", respond({}, 500));
    render(<SummaryCard caseId="BV-X" run={run} />);
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });
});

describe("ResultsTable", () => {
  it("lists the checks by name (no codes) and opens one to its comparison", () => {
    const rules = [rule("R1", "FAIL", { comparisons: [{ field: "account_type", left_label: "Account type", left: "Current",
                                                        right_label: "Statement", right: "Savings", match: false,
                                                        note: null }] }),
                   rule("R6", "NOT_APPLICABLE"), rule("R8", "FAIL")];
    render(<ResultsTable rules={rules} actions={{}} />);
    expect(screen.getByText("Not applicable")).toBeInTheDocument();
    expect(screen.queryByText("Check R8")).toBeNull();               // the overall result is the summary, not a row
    expect(screen.queryByText(/^R1$/)).toBeNull();
    fireEvent.click(screen.getByText("R1 reason"));
    expect(screen.getByText("Mismatch")).toBeInTheDocument();
    expect(screen.getByText("Savings")).toBeInTheDocument();
  });
});

describe("Suggest", () => {
  const banks = [{ value: "State Bank of India", keys: ["SBIN"] }, { value: "Canara Bank", keys: ["CNRB"] },
                 { value: "Central Bank of India", keys: ["CBIN"] }];
  it("offers the bank for a part of its name or its IFSC code", async () => {
    const { matches } = await import("../components/Suggest");
    expect(matches(banks, "canara", 8).map((o) => o.value)).toEqual(["Canara Bank"]);
    expect(matches(banks, "cnrb", 8).map((o) => o.value)).toEqual(["Canara Bank"]);
    expect(matches(banks, "ce", 8)[0].value).toBe("Central Bank of India");
    expect(matches(banks, "india", 8)).toHaveLength(2);
  });
});

describe("withRead", () => {
  it("fills only what the officer left blank, tagged From statement", async () => {
    const { withRead } = await import("../components/BankAccountPanel");
    const form = {
      bank_name: "Axis Bank", account_number: "XXXX7881", account_type: null,
      transactions: [{ date: "2026-09-21", amount: "1.00", direction: "DEBIT" as const },
                     { date: null, amount: null, direction: null }],
      provenance: { bank_name: "APPLICATION" as const, transaction_1: "MANUAL" as const },
    };
    const read = {
      bank_name: null, account_number: null, account_type: "Savings",
      transactions: [{ date: "2026-09-21", amount: "1240.50", direction: "DEBIT" as const },
                     { date: "2026-09-05", amount: "12500.00", direction: "DEBIT" as const }],
      provenance: { account_type: "EXTRACTED" as const, transaction_1: "EXTRACTED" as const, transaction_2: "EXTRACTED" as const },
    };
    const out = withRead(form, read);
    expect(out.account_type).toBe("Savings");
    expect(out.transactions[0].amount).toBe("1.00");                   // typed by the officer: kept
    expect(out.provenance.transaction_1).toBe("MANUAL");
    expect(out.transactions[1].amount).toBe("12500.00");
    expect(out.provenance.transaction_2).toBe("EXTRACTED");
    expect(out.provenance.bank_name).toBe("APPLICATION");
  });
});

describe("FailPopup", () => {
  it("lists only what differs, Show opens the check, Escape closes", async () => {
    const { FailPopup } = await import("../components/FailPopup");
    const onShow = vi.fn();
    const onClose = vi.fn();
    const failed = [rule("R2", "FAIL", { comparisons: [
      { field: "transaction_2", left_label: "Transaction 2", left: "21-09-2026 / ₹5,500.00 / Debit", right_label: "Statement",
        right: "21-09-2026 / ₹5,000.00 / Debit", match: false, note: "the amount differs" },
      { field: "transaction_1", left_label: "Transaction 1", left: "same", right_label: "Statement", right: "same",
        match: true, note: null }] })];
    render(<FailPopup rules={failed} onShow={onShow} onClose={onClose} />);
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText("1 check found a problem")).toBeInTheDocument();
    expect(screen.getByText("the amount differs")).toBeInTheDocument();
    expect(screen.queryByText("Transaction 1")).toBeNull();          // a matching row is not repeated
    fireEvent.click(screen.getByText("Show"));
    expect(onShow).toHaveBeenCalledWith("R2");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("ResultsTable", () => {
  it("names the one source in the column title, with no tag on every row", async () => {
    const { ResultsTable } = await import("../components/ResultsTable");
    const comparisons = [
      { field: "bank_name", left_label: "Bank", left: "ICICI Bank", right_label: "Statement", right: "ICICI Bank", match: true, note: null },
      { field: "account_type", left_label: "Account type", left: "Current", right_label: "Statement", right: "Savings", match: false, note: null }];
    const { container } = render(<ResultsTable rules={[rule("R1", "FAIL", { comparisons })]} actions={{}} />);
    fireEvent.click(container.querySelector(".check-row")!);
    expect(screen.getByRole("columnheader", { name: "Compared with statement" })).toBeInTheDocument();
    expect(container.querySelector(".compare tbody")!.textContent).not.toContain("Statement");
  });
});

describe("ReportCheckView", () => {
  it("shows the report against the screen, field by field", async () => {
    const { ReportCheckView } = await import("../components/ReportCheckView");
    render(<ReportCheckView check={{ status: "FAIL", reason: "The report does not agree with the case: customer name.",
      checks: [{ field: "case_ref", left_label: "Case / application number", left: "BV-2026-001", right_label: "On screen",
                 right: "BV-2026-001", match: true, note: null }],
      comparisons: [{ field: "applicant_name", left_label: "Customer name", left: "Rohan Mehta", right_label: "On screen",
                      right: "Ishaan Verma", match: false, note: null }] }} />);
    expect(screen.getByText("Rohan Mehta")).toBeInTheDocument();
    expect(screen.getByText("Ishaan Verma")).toBeInTheDocument();
    expect(screen.getByText("Mismatch")).toBeInTheDocument();
  });
});


describe("BankAccountPanel actions by AA", () => {
  const detailOf = (mode: "AA_SUCCESS" | "AA_FAILURE") => ({
    case: { case_id: "BV-2026-001", application_ref: "APP-1", applicant_name: "Asha Rao", loan_amount: "100000",
            product: "Home Loan", stage: "CREDIT", mode, status: "PENDING_VERIFICATION", risk_level: null,
            assigned_agency: null, scenario: null, updated_at: "2026-10-30" },
    latest_run: null, aa_response: mode === "AA_SUCCESS" ? { data: { details: {} } } : null,
    prefill: null, documents: [], decisions: [], runs: [], demo: null,
  });
  const config = { agencies: [], transactions_count: 3, suggestions: { banks: [], account_types: [], max_shown: 8 } };

  it("AA received: Waive and Retrigger; AA not received: Run verification", async () => {
    const { BankAccountPanel } = await import("../components/BankAccountPanel");
    render(<BankAccountPanel detail={detailOf("AA_SUCCESS") as never} config={config as never} onProcessed={() => {}} onView={() => {}} />);
    expect(screen.getByRole("button", { name: /Waive/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Retrigger/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Run verification/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "AA not received" }));
    expect(screen.getByRole("button", { name: /Run verification/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retrigger/ })).toBeNull();
  });

  it("waiving asks for a reason before it can be confirmed", async () => {
    const fetchMock = respond({ "/waive": { case_id: "BV-2026-001", status: "WAIVED" } });
    vi.stubGlobal("fetch", fetchMock);
    const { BankAccountPanel } = await import("../components/BankAccountPanel");
    const done = vi.fn();
    render(<BankAccountPanel detail={detailOf("AA_SUCCESS") as never} config={config as never} onProcessed={done} onView={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Waive/ }));
    const confirm = screen.getByRole("button", { name: "Waive verification" });
    expect(confirm).toBeDisabled();                                   // no reason yet
    fireEvent.change(screen.getByLabelText("Reason for waiving"), { target: { value: "AA data from the bank" } });
    await act(async () => { fireEvent.click(confirm); });
    await waitFor(() => expect(done).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toContain("/cases/BV-2026-001/waive");
    expect(JSON.parse(String(init.body))).toEqual({ reason: "AA data from the bank" });
  });
});
