import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { theme } from "../theme";
import type { Workspace } from "../types";
import fixtures from "./fixtures.json";

// fixtures.json: the REAL API's answers on the synthetic seed (written from the backend, not by hand)
const calls: { url: string; init?: RequestInit }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
let workspace: Workspace;

beforeEach(() => {
  calls.length = 0;
  window.location.hash = "";
  workspace = structuredClone(fixtures.escalate) as unknown as Workspace;
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, init });
    // the sample keeps the case's own city: only the landmark differs, so only it is saved
    if (url.includes("/sample?part=application")) return json({ values: { landmark: "Near Lake Garden",
                                                                          city: String(fixtures.escalate.application.edit.city) } });
    if (init?.method === "POST" || init?.method === "PUT") return json(workspace);
    if (url.endsWith("/config")) return json(fixtures.config);
    if (url.includes("/applications?")) return json(fixtures.worklist);
    if (url.endsWith("/summary")) return json(fixtures.summary);
    if (url.includes("/applications/")) return json(workspace);
    return json({});
  }));
});
afterEach(() => vi.unstubAllGlobals());

const renderApp = () => render(<MantineProvider theme={theme}><Notifications /><App /></MantineProvider>);

async function openCase(id = "LGV-2026-002", name = "Vikas Shetty", step = "application") {
  window.location.hash = `#/cases/${id}/${step}`;
  renderApp();
  return screen.findByRole("heading", { name, level: 1 });
}

describe("worklist", () => {
  it("lists the applications with status and recommendation", async () => {
    renderApp();
    const row = (await screen.findByText("Vikas Shetty")).closest("tr")!;
    expect(within(row).getByText("Escalate")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(fixtures.worklist.applications.length + 1);
  });
});

describe("the case", () => {
  it("has the Case ID box and only the case's four steps in the sidebar", async () => {
    await openCase();
    const rail = document.querySelector(".steps")!;
    const steps = Array.from(rail.querySelectorAll(".step")).map((s) => s.querySelector("p")?.textContent);
    expect(steps).toEqual(["Application Details", "Verification Report", "Automated Rule Results", "Final Status"]);
    expect(screen.getByLabelText("Enter case / application ID")).toBeInTheDocument();
    expect(document.querySelector(".side")?.textContent).not.toContain("Bureau Analysis");     // no LOS-wide list
    // the brand title types itself; its full name is always the accessible name
    expect(document.querySelector(".brand-title .typewriter")?.getAttribute("aria-label")).toBe(fixtures.config.screen.brand.title);
    // the case's status: a filled badge in its own colour, never white
    expect(document.querySelector(".status-chip")?.getAttribute("data-variant")).toBe("filled");
    fireEvent.click(within(rail as HTMLElement).getByText("Verification Report"));
    await waitFor(() => expect(window.location.hash).toBe("#/cases/LGV-2026-002/report"));
  });

  it("shows the recommendation and the AI summary large in Final Status, not beside the form", async () => {
    await openCase();
    expect(document.querySelector(".points")).toBeNull();
    fireEvent.click(within(document.querySelector(".steps") as HTMLElement).getByText("Final Status"));
    expect(await screen.findByText(/Critical:/)).toBeInTheDocument();
    expect(document.querySelectorAll(".points li").length).toBe(fixtures.summary.points.length);
    expect(document.querySelector(".points li.p-red")).not.toBeNull();                       // each point coloured
  });

  it("opens Application Details as a form with dropdowns, Fill sample, upload under it, then Save / Initiate / Waive", async () => {
    await openCase();
    // every Leviosa Application Details field, by its Leviosa name
    for (const label of ["Applicant Name", "Reference ID", "Property Owner First Name", "Property Owner Middle Name",
                         "Property Owner Last Name", "Flat No./House No.", "Address Line 1", "Address Line 2",
                         "Project Name/Colony Name/Building Name", "Nearest Landmark", "District", "Pin Code", "City/Town/Village",
                         "Complete Property Address", "Owner's PAN No.", "Applicant's PAN No."]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    for (const dropdown of ["Property Type", "Property Usage", "Property Nature", "Document", "State", "Legal Vendor Name"]) {
      expect(screen.getAllByLabelText(dropdown).length).toBeGreaterThan(0);
    }
    const upload = screen.getByText("Sale Deed and Supporting Documents");
    const save = screen.getByRole("button", { name: /^Save$/ });
    expect(upload.compareDocumentPosition(save) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByRole("button", { name: /Initiate/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Waive/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Fill sample/ }));
    await waitFor(() => expect((screen.getByLabelText("Nearest Landmark") as HTMLInputElement).value).toBe("Near Lake Garden"));
    fireEvent.click(save);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/details") && c.init?.method === "PUT")).toBe(true));
    expect(JSON.parse(String(calls.find((c) => c.url.endsWith("/details"))!.init!.body)).values).toEqual({ landmark: "Near Lake Garden" });
    expect(document.body.textContent).not.toMatch(/ABCP|AKKP|BVSP/);                       // a PAN only ever masked
  });

  it("gives the agency every Verification Report field", async () => {
    workspace = structuredClone(fixtures.with_agency) as unknown as Workspace;
    await openCase("LGV-2026-006", "Karan Mehta", "report");
    for (const label of ["Taluka", "Signing authority", "Sub Registrar", "No. of years of property documents",
                         "Can an equitable mortgage be created on the listed documents?",
                         // the advocate's report as it really comes: header, identity, boundaries, the lists, the certificate
                         "Report / file reference no.", "City Survey No.", "Boundary: North", "Documents examined by the agency",
                         "Documents the bank must take", "Conditions the opinion is subject to",
                         "Entries in the Encumbrance Certificate", "Title chain, oldest first"]) {
      expect((await screen.findAllByText(label, { exact: false })).length).toBeGreaterThan(0);
    }
    // the report is generated only after Submit, on request -- no Generate before the answers are in
    expect(screen.getByText(/After you submit, you can generate the agency report/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Generate report/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Submit report/ }));
    expect(await screen.findByText(/required answer\(s\) missing/)).toBeInTheDocument();
  });

  it("pops up Generate report once the agency has submitted, and generates it on click", async () => {
    workspace = structuredClone(fixtures.escalate) as unknown as Workspace;
    workspace.agency_report = { ...workspace.agency_report!, document_id: null, analysis: [] };
    workspace.documents = workspace.documents.filter((d) => !fixtures.config.agency_kinds.includes(d.kind));   // no file yet
    workspace.can = { ...workspace.can, generate_report: true };
    await openCase("LGV-2026-002", "Vikas Shetty", "report");
    // the pop-up card asks at once; generating attaches the report, then the rules run on it
    const pop = await screen.findByRole("dialog");
    expect(within(pop).getByText("The agency's report is in")).toBeInTheDocument();
    workspace = structuredClone(fixtures.escalate) as unknown as Workspace;      // what the server returns: generated
    fireEvent.click(within(pop).getByRole("button", { name: /Generate report/ }));
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/agency-report/generate") && c.init?.method === "POST")).toBe(true));
    expect(await screen.findByText("title-verification-summary-LGV-2026-002.pdf")).toBeInTheDocument();
    expect(screen.queryByText("Report submitted. Generate the agency report?")).toBeNull();
  });

  it("shows ONE agency report card and only that: no rules, no summary in the Verification Report", async () => {
    await openCase("LGV-2026-002", "Vikas Shetty", "report");
    await waitFor(() => expect(document.querySelector(".report-card")).not.toBeNull());
    const card = document.querySelector(".report-card") as HTMLElement;
    expect(within(card).getByText("Agency report")).toBeInTheDocument();
    expect(within(card).getByText(fixtures.escalate.case.agency_name!)).toBeInTheDocument();
    expect(card.querySelectorAll(".report-file")).toHaveLength(1);                       // one report, not a list
    expect(within(card).getByText("title-verification-summary-LGV-2026-002.pdf")).toBeInTheDocument();
    fireEvent.click(within(card).getByRole("button", { name: /Download agency report/ }));
    await waitFor(() => expect(calls.some((c) => c.url.includes("/documents/") && c.url.endsWith("/file"))).toBe(true));
    // every field of the report shows on the page, open -- not folded away
    for (const label of ["Sub Registrar", "Taluka", "Title chain, oldest first", "Has the history and flow of title been traced adequately?"]) {
      expect(within(card).getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(card.querySelectorAll(".answer-section").length).toBe(fixtures.config.form.sections.length);
    // the rules and the summary are not repeated here (they are in Rule Results and Final Status)
    expect(document.querySelector(".rules")).toBeNull();
    expect(document.querySelector(".assessment")).toBeNull();
  });

  it("lists the rules red first, with the values by source", async () => {
    await openCase("LGV-2026-002", "Vikas Shetty", "rules");
    await waitFor(() => expect(document.querySelectorAll(".rules .rule").length).toBeGreaterThan(0));
    const rules = Array.from(document.querySelectorAll(".rules .rule"));
    const order = rules.map((r) => (r.className.match(/rule-(FAIL|REVIEW|PASS)/) ?? [])[1]);
    expect(order).toEqual([...order].sort((a, b) => ["FAIL", "REVIEW", "PASS"].indexOf(a!) - ["FAIL", "REVIEW", "PASS"].indexOf(b!)));
    expect(within(rules[0] as HTMLElement).getByText("Prakash Shetty")).toBeInTheDocument();
    // the comparison headings in bold text, the failed rule's cross beating, the LLM summary in points under the rules
    const heads = Array.from((rules[0] as HTMLElement).querySelectorAll(".cmp-head th")).map((th) => th.textContent);
    expect(heads).toEqual(expect.arrayContaining(["Checked", fixtures.config.sources.APPLICATION, fixtures.config.sources.AGENCY]));
    expect((rules[0] as HTMLElement).querySelector(".beat")).not.toBeNull();
    // the comparison table has its lines, across and down
    const cell = (rules[0] as HTMLElement).querySelector(".cmp-table tbody td");
    expect(cell?.hasAttribute("data-with-column-border")).toBe(true);
    expect(cell?.closest("tr")?.hasAttribute("data-with-row-border")).toBe(true);
    expect(document.querySelector(".assessment")).toBeNull();          // the rules only: the summary is in Final Status
    expect(screen.queryByText(/Last run by/)).toBeNull();               // no "run by" line
    expect(document.body.textContent).not.toContain("⟨");               // every word found
  });

  it("keeps Final Status locked, with no decision to take, until the rules have run", async () => {
    workspace = structuredClone(fixtures.fresh) as unknown as Workspace;
    await openCase("LGV-2026-005", "Sneha Iyer", "final");
    expect(await screen.findByText("The decision opens after the checks")).toBeInTheDocument();
    expect(screen.queryByText("Submit decision")).toBeNull();
    expect(document.querySelector(".assessment")).toBeNull();
  });

  it("says why Initiate is off", async () => {
    workspace = structuredClone(fixtures.fresh) as unknown as Workspace;           // a new case: no Sale Deed yet
    await openCase("LGV-2026-005", "Sneha Iyer");
    expect(await screen.findByText("To initiate, upload the Sale Deed below first.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Initiate$/ })).toBeDisabled();
  });

  it("opens the Verification Report once Initiate brings the agency's report", async () => {
    workspace = structuredClone(fixtures.fresh) as unknown as Workspace;           // ready: documents in, vendor chosen
    workspace.deed_document_id = "DOC-1";
    workspace.can = { ...workspace.can, send: true };
    workspace.case = { ...workspace.case, selected_agency: "LEX_ASSOCIATES" };
    await openCase("LGV-2026-005", "Sneha Iyer");
    fireEvent.click(await screen.findByRole("button", { name: /^Initiate$/ }));
    // the large confirm card: the vendor, the owners, the property, the documents, what follows
    const card = await screen.findByRole("dialog");
    for (const text of ["Send this case to the agency?", "Owners on record", "Property", "Sneha Iyer"]) {
      expect(within(card).getAllByText(text, { exact: false }).length).toBeGreaterThan(0);
    }
    workspace = structuredClone(fixtures.escalate) as unknown as Workspace;        // the server: sent, report in
    fireEvent.click(await screen.findByRole("button", { name: /Confirm and send/ }));
    await waitFor(() => expect(window.location.hash).toBe("#/cases/LGV-2026-005/report"));
  });

  it("waives only with a reason", async () => {
    workspace = structuredClone(fixtures.fresh) as unknown as Workspace;
    await openCase("LGV-2026-005", "Sneha Iyer");
    fireEvent.click(screen.getByRole("button", { name: /^Waive$/ }));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: /Waive verification/ });
    expect(confirm).toBeDisabled();
    fireEvent.change(within(dialog).getByRole("textbox"), { target: { value: "Already under our charge" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(calls.some((c) => c.url.endsWith("/waive"))).toBe(true));
  });

  it("shows nothing of the backend and no missing words", async () => {
    await openCase();
    const clean = () => {
      const text = document.body.textContent ?? "";
      expect(text).not.toContain("⟨");
      expect(text).not.toMatch(/\bR\d{1,2}\b/);
      expect(text).not.toMatch(/demo|seed|synthetic|mock|undefined/i);
      expect(text).not.toMatch(/\b[A-Z]+_[A-Z_]+\b/);
    };
    await screen.findByText("Applicant Name");
    clean();                                                        // the form
    fireEvent.click(within(document.querySelector(".steps") as HTMLElement).getByText("Final Status"));
    await screen.findByText(/Critical:/);
    clean();                                                        // and the summary
  });
});
