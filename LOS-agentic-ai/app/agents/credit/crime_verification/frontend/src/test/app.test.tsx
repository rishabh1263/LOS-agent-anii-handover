import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ResultsPage } from "../pages/ResultsPage";
import { VerifyPage } from "../pages/VerifyPage";
import type { ApplicationSubject, ApplicationView, Case, Subject, UiConfig } from "../types/api";
import { composeAddress, parseAddress } from "../utils/address";
import { sampleDetails } from "../utils/sample";
import { setScreen } from "../utils/text";
import { validateSubject } from "../utils/validate";
import screenTexts from "./screen.fixture.json";

const config: UiConfig = {
  demo_mode: true, banner: null,
  user: { id: "officer", permissions: ["view", "initiate", "retrigger", "refresh", "finalize", "download_report"] },
  vendors: [{ id: "CRIME_CHECK", name: "Crime Check" }], default_vendor: "CRIME_CHECK",
  validation: { pan_pattern: "^[A-Z]{3}[PCHFATBLJG][A-Z][0-9]{4}[A-Z]$", name_pattern: "^[A-Za-z][A-Za-z .'-]*$",
                name_max: 80, address_min: 10, address_max: 250, min_age_years: 18, max_age_years: 100 },
  messages: { required: "{field} is required.", name_format: "{field} may contain letters only.",
              too_long: "{field} is too long.", address_short: "Permanent Address must be at least {min} characters.",
              date_format: "Date of Birth must be a valid date (YYYY-MM-DD).", date_future: "Date of Birth cannot be in the future.",
              age_range: "Age must be between {min} and {max} years.", pan_format: "PAN must be 10 characters like ABCPE1234F.",
              pin_format: "PIN code must be 6 digits." },
  fields: { full_name: "Applicant Name", father_name: "Father's Name", date_of_birth: "Date of Birth",
            permanent_address: "Permanent Address", pan: "PAN" },
  roles: { PRIMARY: "Applicant", CO_APPLICANT: "Co-Applicant" },
  risk: { labels: { NO_RISK: "No Risk", LOW_RISK: "Low Risk", AVERAGE_RISK: "Average Risk", HIGH_RISK: "High Risk",
                    VERY_HIGH_RISK: "Very High Risk" }, meaning: {} },
  statuses: { NOT_INITIATED: "Not initiated", COMPLETED: "Completed", REVIEW_REQUIRED: "Review required",
              PROCESSING: "Processing", FAILED: "Failed", WAIVED: "Waived", INITIATED: "Initiated" },
  decisions: [{ id: "ACCEPTED", label: "Accepted" }], remarks_required_for: [], scenarios: [],
  retrigger_needed_after: ["COMPLETED", "REVIEW_REQUIRED", "WAIVED"], blocked_while: ["INITIATED", "PROCESSING"],
  decision_labels: { ACCEPTED: "Accepted" },
  summary_labels: { ai: "AI summary", rules: "Summary", pending: "Writing the AI summary…", ai_note: "", rules_note: "",
                    clean: "Clean result", review: "Needs review" },
  show_reset: false,
  screen: screenTexts, roles_numbered: { CO_APPLICANT: "Co-Applicant {n}" }, max_co_applicants: 10,
  celebration: { title: "All clear", subtitle: "No matches found for {people}.", sound_on: "Sound on", sound_off: "Sound off" },
  alarm: { title: "Review required", subtitle: "Findings for {people}. Look at them before you decide." },
  form: { pin_pattern: "^[1-9][0-9]{5}$",
          states: { Maharashtra: { pin_prefixes: ["40"], cities: ["Mumbai", "Pune"] },
                    Karnataka: { pin_prefixes: ["56"], cities: ["Bengaluru"] } } },
  form_fields: { address_line: "House / street / locality", state: "State", city: "City", pin: "PIN code" },
  samples: { first_names: ["Kavya"], last_names: ["Rao"], father_first_names: ["Mohan"], address_lines: ["{n}, Shanti Nagar, {area}"],
             areas: ["Adyar"], age_years: [30, 30] },
  ui: { verification_tabs: [], active_tab: "", refresh_seconds: 0, celebrate_risks: ["NO_RISK", "LOW_RISK"],
        alarm_risks: ["AVERAGE_RISK", "HIGH_RISK", "VERY_HIGH_RISK"] },
};

const ADDRESS = "1 Test Street, Andheri East, Mumbai, Maharashtra 400069";
const person = (role: "PRIMARY" | "CO_APPLICANT", over: Partial<ApplicationSubject> = {}): ApplicationSubject => ({
  role, seq: role === "PRIMARY" ? 0 : 1, party: role === "PRIMARY" ? "PRIMARY" : "CO_APPLICANT-1",
  role_label: config.roles[role], full_name: role === "PRIMARY" ? "Asha Primary" : "Ravi Co",
  father_name: "Kiran Father", date_of_birth: "1990-01-01", permanent_address: ADDRESS,
  pan_masked: "XXXXXX001A", source: "APPLICATION", missing: [], errors: [], latest: null, demo_scenario: null, ...over,
});

const viewOf = (over: Partial<ApplicationView> = {}): ApplicationView => ({
  application: { application_id: "APP-T-1", title: "", branch: "MUM", product: "Home Loan", loan_amount: 100000,
                 stage: "Credit Assessment", status: "In Progress", applicant_name: "Asha Primary" },
  subjects: [person("PRIMARY"), person("CO_APPLICANT")], latest_case: null, cases: [],
  can: { initiate: true, retrigger: true, refresh: true, finalize: true, download_report: true, reset_demo: false },
  ...over,
});

const done = (role: "PRIMARY" | "CO_APPLICANT", risk: string, status: Subject["status"]): Subject => ({
  subject_id: `SUB-${role}`, case_id: "CRV-1", role, seq: role === "PRIMARY" ? 0 : 1,
  party: role === "PRIMARY" ? "PRIMARY" : "CO_APPLICANT-1", role_label: config.roles[role], full_name: role === "PRIMARY" ? "Asha Primary" : "Ravi Co",
  father_name: "y", date_of_birth: "1990-01-01", permanent_address: "z", pan_masked: "XXXXXX001A", status,
  status_label: config.statuses[status], provider_reference: "CC-1", outcome: risk === "NO_RISK" ? "NO_MATCH" : "FINDINGS",
  outcome_label: "", error: null, report: null, updated_at: "",
  result: { result_id: `R-${role}`, provider_reference: "CC-1", outcome: "", outcome_label: "", risk_level: risk,
            risk_label: config.risk.labels[risk], risk_meaning: "", total_cases: risk === "NO_RISK" ? 0 : 3,
            summary: risk === "NO_RISK" ? "No matches." : "3 case(s) found.", groups: [], clusters: [], result_at: "", synthetic: true },
});

const caseOf = (subjects: Subject[], over: Partial<Case> = {}): Case => ({
  case_id: "CRV-1", application_id: "APP-T-1", vendor: "CRIME_CHECK", vendor_name: "Crime Check",
  status: subjects.some((s) => s.status === "REVIEW_REQUIRED") ? "REVIEW_REQUIRED" : "COMPLETED", status_label: "",
  kind: "INITIAL", initiated_by: "officer", initiated_at: "2026-10-27T10:00:00+00:00", updated_at: "t", waived_reason: null,
  final_decision: null, final_decision_label: "", final_remarks: null, finalized_by: null, finalized_at: null, subjects,
  synthetic: true, can: { refresh: false, finalize: false }, ...over,
});

let view: ApplicationView;
let theCase: Case;
let summaryBody: unknown;
let initiate: (init: RequestInit) => Promise<Response>;
const calls: { url: string; init?: RequestInit }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  setScreen(screenTexts);
  calls.length = 0;
  view = viewOf();
  theCase = caseOf([done("PRIMARY", "NO_RISK", "COMPLETED"), done("CO_APPLICANT", "NO_RISK", "COMPLETED")]);
  summaryBody = {};
  try { sessionStorage.clear(); } catch { /* none */ }
  initiate = async () => json({ ...theCase, case_id: "CRV-NEW" }, 201);
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    if (url.endsWith("/initiate")) return initiate(init!);
    if (url.endsWith("/summary")) return json(summaryBody);
    if (url.includes("/audit")) return json({ events: [] });
    if (url.includes("/cases/")) return json(theCase);
    if (url.includes("/applications?")) return json({ applications: [{ application_id: "APP-T-1", title: "", product: "Home Loan",
      loan_amount: 100000, stage: "x", branch: "MUM", applicant: "Asha Primary", latest_status: null, latest_status_label: "" }] });
    if (url.includes("/applications/")) return json(view);
    return json({});
  }));
});
afterEach(() => vi.unstubAllGlobals());

const posts = () => calls.filter((c) => c.init?.method === "POST");

async function openDesk(onOpenCase = vi.fn()) {
  render(<VerifyPage id="APP-T-1" config={config} onSelect={() => {}} onOpenCase={onOpenCase} />);
  await screen.findByRole("button", { name: /Initiate Verification/ });
  return onOpenCase;
}

describe("verification desk", () => {
  it("asks for a case ID first, and finds one typed in", async () => {
    const onSelect = vi.fn();
    render(<VerifyPage id={null} config={config} onSelect={onSelect} onOpenCase={() => {}} />);
    expect(screen.getByText("Enter a case or application ID")).toBeInTheDocument();
    await screen.findByText("Asha Primary");
    fireEvent.change(screen.getByLabelText("Case or application ID"), { target: { value: "app-t-1" } });
    await waitFor(() => expect(screen.getAllByText("APP-T-1").length).toBeGreaterThan(0));
    fireEvent.submit(screen.getByRole("search"));
    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("APP-T-1"));
  });

  it("splits the application address into line, state, city and PIN", async () => {
    await openDesk();
    expect(screen.getByLabelText(/House \/ street/)).toHaveValue("1 Test Street, Andheri East");
    expect(screen.getByLabelText(/^State/)).toHaveValue("Maharashtra");
    expect(screen.getByLabelText(/^City/)).toHaveValue("Mumbai");
    expect(screen.getByLabelText(/PIN code/)).toHaveValue("400069");
  });

  it("Fill sample fills the details but never the PAN", async () => {
    await openDesk();
    fireEvent.click(screen.getByRole("button", { name: /Fill sample/ }));
    expect(screen.getByLabelText(/Applicant Name/)).toHaveValue("Kavya Rao");
    expect(screen.getByLabelText(/Father's Name/)).toHaveValue("Mohan Rao");
    expect(screen.getByLabelText(/^PAN/)).toHaveValue("XXXXXX001A");
  });
});

describe("confirmation popup", () => {
  it("opens on Initiate Verification and Cancel / Escape send nothing", async () => {
    await openDesk();
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    const dialog = await screen.findByRole("dialog", { name: /Confirm Applicant Details and Initiate Crime Verification\?/ });
    expect(within(dialog).getByText("Primary Applicant")).toBeInTheDocument();
    expect(within(dialog).getAllByText("XXXXXX001A")).toHaveLength(2);
    expect(within(dialog).getAllByText(ADDRESS)).toHaveLength(2);
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    await screen.findByRole("dialog");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });

  it("Confirm sends one request and opens the results page", async () => {
    let release!: () => void;
    initiate = () => new Promise((resolve) => { release = () => resolve(json({ ...theCase, case_id: "CRV-NEW" }, 201)); });
    const onOpenCase = await openDesk();
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Confirm & Initiate Verification/ }));
    const busy = await within(dialog).findByRole("button", { name: /Initiating Verification/ });
    expect(busy).toBeDisabled();
    fireEvent.click(busy);
    expect(posts()).toHaveLength(1);
    const body = JSON.parse(String(posts()[0].init!.body));
    expect(body.confirmation_accepted).toBe(true);
    expect(body.subjects.map((s: { role: string }) => s.role)).toEqual(["PRIMARY", "CO_APPLICANT"]);
    expect(body.subjects[0].permanent_address).toBe(ADDRESS);
    expect(body.subjects[0].pan).toBeUndefined();
    expect(new Headers(posts()[0].init!.headers).get("Idempotency-Key")).toBeTruthy();
    expect(onOpenCase).not.toHaveBeenCalled();                // nothing before the server says so
    await act(async () => release());
    await waitFor(() => expect(onOpenCase).toHaveBeenCalledWith("CRV-NEW"));
  });

  it("a failure keeps the popup open with the reason and the details", async () => {
    initiate = async () => json({ error: { code: "VALIDATION_ERROR", message: "Some details are not valid." },
                                  detail: { details: [{ field: "subjects[0].pan", message: "The PAN for the Applicant does not match the application record." }] } }, 422);
    await openDesk();
    fireEvent.change(screen.getByLabelText(/Applicant Name/), { target: { value: "Edited Name" } });
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Confirm & Initiate Verification/ }));
    expect(await within(dialog).findByText(/does not match the application record/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(screen.getByLabelText(/Applicant Name/)).toHaveValue("Edited Name");
  });
});

describe("validation", () => {
  it("missing details block the popup and show what to fix", async () => {
    view = viewOf({ subjects: [person("PRIMARY"), person("CO_APPLICANT", { father_name: "", permanent_address: "" })] });
    await openDesk();
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText(/Complete the Co-Applicant's details/)).toBeInTheDocument();
    expect(screen.getByText("Father's Name is required.")).toBeInTheDocument();
    expect(screen.getByText("PIN code is required.")).toBeInTheDocument();
    expect(posts()).toHaveLength(0);
  });

  it("checks PAN, dates and the address like the server", () => {
    const base = { role: "PRIMARY", full_name: "A B", father_name: "C D", date_of_birth: "1990-01-01",
                   permanent_address: "1 Test Street, City" };
    expect(validateSubject(base, config)).toEqual({});
    expect(validateSubject({ ...base, pan: "ABC123" }, config).pan).toMatch(/PAN must be/);
    expect(validateSubject({ ...base, date_of_birth: "1990-02-30" }, config).date_of_birth).toMatch(/valid date/);
    expect(validateSubject({ ...base, date_of_birth: "2999-01-01" }, config).date_of_birth).toMatch(/future/);
    const parts = parseAddress(ADDRESS, config);
    expect(parts).toEqual({ line: "1 Test Street, Andheri East", city: "Mumbai", state: "Maharashtra", pin: "400069" });
    expect(composeAddress(parts)).toBe(ADDRESS);
    const s = sampleDetails(config.samples!, config);
    expect(s.address.pin).toMatch(/^(40|56)\d{4}$/);
  });
});

describe("results page", () => {
  it("shows each person's result and celebrates a clean result", async () => {
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    expect(await screen.findByText("All clear")).toBeInTheDocument();
    expect(screen.getAllByRole("img", { name: "Overall risk: No Risk" })).toHaveLength(2);
    expect(document.querySelector(".petals")).not.toBeNull();
    expect(screen.getByText("No matches found for Applicant and Co-Applicant.")).toBeInTheDocument();
  });

  it("no petals when someone needs review; the AI summary shows", async () => {
    theCase = caseOf([done("PRIMARY", "NO_RISK", "COMPLETED"), done("CO_APPLICANT", "AVERAGE_RISK", "REVIEW_REQUIRED")]);
    summaryBody = { case_id: "CRV-1", available: true, source: "ai", pending: false, label: "AI summary",
                    text: "The Co-Applicant has 3 cases found." };
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    expect(await screen.findByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("AI summary")).toBeInTheDocument();
    expect(screen.getByText("3 case(s) found.")).toBeInTheDocument();
    expect(document.querySelector(".petals")).toBeNull();
    // a result that needs review is flagged instead: the alarm names who has findings
    expect(document.querySelector(".alarm")).not.toBeNull();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/Findings for Co-Applicant \(Average Risk\)/)).toBeInTheDocument();
  });

  it("no alarm for a clean result", async () => {
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    await screen.findByText("All clear");
    expect(document.querySelector(".alarm")).toBeNull();
  });

  it("a provider failure shows the reason", async () => {
    theCase = caseOf([{ ...done("PRIMARY", "NO_RISK", "FAILED"), result: null, outcome: "PROVIDER_FAILURE",
                        outcome_label: "Provider failure — no result",
                        error: { code: "PROVIDER_UNAVAILABLE", message: "The verification provider did not respond." } }],
                     { status: "FAILED" });
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    expect(await screen.findByText("The verification provider did not respond.")).toBeInTheDocument();
  });

  it("a case that is not there offers the way back", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: { code: "CASE_NOT_FOUND", message: "No verification case CRV-X is open to you." } }, 404)));
    const back = vi.fn();
    render(<ResultsPage caseId="CRV-X" config={config} onBack={back} />);
    expect(await screen.findByText("No verification case CRV-X is open to you.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Go to verification/ }));
    expect(back).toHaveBeenCalled();
  });

  it("shows nothing that says demo", async () => {
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    await screen.findByText("All clear");
    expect(document.body.textContent ?? "").not.toMatch(/demo|synthetic|mock/i);
  });
});

describe("more than one co-applicant", () => {
  const co = (n: number, name: string): ApplicationSubject =>
    person("CO_APPLICANT", { seq: n, party: `CO_APPLICANT-${n}`, role_label: `Co-Applicant ${n}`, full_name: name });

  it("lists every person, and sends each with its number", async () => {
    view = viewOf({ subjects: [person("PRIMARY"), co(1, "Ravi One"), co(2, "Sita Two"), co(3, "Hari Three")] });
    await openDesk();
    expect(screen_(/Co-Applicant 3/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: /Co-Applicant 2/ }));
    expect(screen_getValue(/Applicant Name/)).toBe("Sita Two");
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getAllByRole("region")).toHaveLength(4);
    fireEvent.click(within(dialog).getByRole("button", { name: /Confirm & Initiate Verification/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const body = JSON.parse(String(posts()[0].init!.body));
    expect(body.subjects.map((s: { role: string; seq: number }) => `${s.role}-${s.seq}`))
      .toEqual(["PRIMARY-0", "CO_APPLICANT-1", "CO_APPLICANT-2", "CO_APPLICANT-3"]);
  });

  it("can leave some co-applicants out", async () => {
    view = viewOf({ subjects: [person("PRIMARY"), co(1, "Ravi One"), co(2, "Sita Two")] });
    await openDesk();
    fireEvent.click(screen.getByLabelText("Include Co-Applicant 1 in this verification"));
    expect(screen.getByText("2 of 3 people selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Initiate Verification/ }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: /Confirm & Initiate Verification/ }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const body = JSON.parse(String(posts()[0].init!.body));
    expect(body.subjects.map((s: { seq: number }) => s.seq)).toEqual([0, 2]);
  });

  it("puts the summary first, with every person at a glance", async () => {
    theCase = caseOf([done("PRIMARY", "NO_RISK", "COMPLETED"),
                      { ...done("CO_APPLICANT", "AVERAGE_RISK", "REVIEW_REQUIRED"), party: "CO_APPLICANT-1", role_label: "Co-Applicant 1" },
                      { ...done("CO_APPLICANT", "NO_RISK", "COMPLETED"), subject_id: "SUB-C2", seq: 2, party: "CO_APPLICANT-2",
                        role_label: "Co-Applicant 2", full_name: "Sita Two" }]);
    summaryBody = { case_id: "CRV-1", available: true, source: "ai", pending: false, label: "AI summary", text: "Summary text." };
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    const card = (await screen.findByText("Summary text.")).closest("section")!;
    const glance = within(card).getByLabelText("At a glance");
    expect(within(glance).getByText("Co-Applicant 2")).toBeInTheDocument();
    expect(within(glance).getByText("Average Risk")).toBeInTheDocument();
    const order = Array.from(document.querySelectorAll(".ai-card, .result-card"));
    expect(order[0].classList.contains("ai-card")).toBe(true);                // the summary comes first
    expect(document.querySelectorAll(".result-card")).toHaveLength(3);
  });

  // owner 2026-10-29: up to 10 co-applicants -- one compact list + one result, not a tall card per person
  const eleven = () => [done("PRIMARY", "NO_RISK", "COMPLETED"),
    ...Array.from({ length: 10 }, (_, i) => ({
      ...done("CO_APPLICANT", i === 6 ? "HIGH_RISK" : "NO_RISK", i === 6 ? "REVIEW_REQUIRED" : "COMPLETED"),
      subject_id: `SUB-C${i + 1}`, seq: i + 1, party: `CO_APPLICANT-${i + 1}`, role_label: `Co-Applicant ${i + 1}`,
      full_name: `Person ${i + 1}` }))];
  const compact = { ...config, ui: { ...config.ui, compact_people_from: 4 } };

  it("with many people shows one list and the result of the one who needs review", async () => {
    theCase = caseOf(eleven());
    render(<ResultsPage caseId="CRV-1" config={compact} onBack={() => {}} />);
    const list = (await screen.findByText("People (11)")).closest("section")!;
    expect(within(list).getAllByRole("tab", { name: /Co-Applicant \d+/ })).toHaveLength(10);
    expect(document.querySelectorAll(".result-card")).toHaveLength(1);                // one result, not eleven cards
    expect(within(document.querySelector(".people-detail") as HTMLElement).getByText("Co-Applicant 7")).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("tab", { name: /Co-Applicant 10/ }));
    expect(within(document.querySelector(".people-detail") as HTMLElement).getByText("Person 10")).toBeInTheDocument();
    fireEvent.click(within(list).getByRole("tab", { name: "Needs review (1)" }));
    expect(within(list).getAllByRole("tab", { name: /Co-Applicant \d+/ })).toHaveLength(1);
  });

  it("every PDF download is the final report of the whole case", async () => {
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    const buttons = await screen.findAllByRole("button", { name: /Download report \(PDF\)/ });
    fireEvent.click(buttons[buttons.length - 1]);                  // a person's own button: still everyone
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/cases/CRV-1/report"))).toBe(true));
    expect(calls.some((c) => c.url.includes("/subjects/"))).toBe(false);
  });

  it("with many people the details form wraps its tabs and names only who is left out", async () => {
    view = viewOf({ subjects: [person("PRIMARY"), ...Array.from({ length: 10 }, (_, i) => co(i + 1, `Person ${i + 1}`))] });
    render(<VerifyPage id="APP-T-1" config={compact} onSelect={() => {}} onOpenCase={() => {}} />);
    await screen.findByText("Asha Primary", { selector: "h2" });
    expect(document.querySelector(".person-tabs.is-many")).not.toBeNull();
    fireEvent.click(screen.getByLabelText("Include Co-Applicant 4 in this verification"));
    expect(screen.getByText("10 of 11 people selected")).toBeInTheDocument();
    expect(document.querySelectorAll(".action-people .pill")).toHaveLength(1);
  });
});

describe("every word comes from config", () => {
  it("no screen text is missing from the config", async () => {
    await openDesk();
    expect(document.body.textContent ?? "").not.toContain("⟨");
    render(<ResultsPage caseId="CRV-1" config={config} onBack={() => {}} />);
    await screen.findByText("All clear");
    expect(document.body.textContent ?? "").not.toContain("⟨");
  });
});

function screen_(pattern: RegExp) { return screen.getAllByText(pattern)[0]; }
function screen_getValue(label: RegExp) { return (screen.getByLabelText(label) as HTMLInputElement).value; }
