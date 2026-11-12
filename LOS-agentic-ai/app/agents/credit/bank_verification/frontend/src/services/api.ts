// The bank verification API client. No role header: in demo mode the server's one demo user does every step
// (config security.default_role); production takes the role from the LOS login.
import type {
  AgencyInput, ApiErrorBody, AppConfig, CaseDetail, EnteredDetails, FinalizeResult, Mode, PlainSummary, ReadingStarted, Run,
  StatementRead,
} from "../types/api";

export const BASE = "/api/v1/bank-verification";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details: unknown = null) {
    super(message);
  }
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = (await res.json()) as ApiErrorBody;
    } catch {
      /* not JSON */
    }
    // the agent's own server sends error.details; the LOS server (8010) keeps them in detail.details
    const detail = (body as unknown as { detail?: { details?: unknown } } | null)?.detail;
    throw new ApiError(res.status, body?.error?.code ?? "HTTP_ERROR", body?.error?.message ?? res.statusText,
                       body?.error?.details ?? (typeof detail === "object" ? detail?.details : null) ?? null);
  }
  return (await res.json()) as T;
}

export const api = {
  config: () => call<AppConfig>("/config"),
  /** The search: ONE case by its exact application number or case id -- the screen never lists cases. */
  lookup: (ref: string) => call<{ case_id: string }>(`/lookup?ref=${encodeURIComponent(ref)}`),
  caseDetail: (id: string) => call<CaseDetail>(`/cases/${encodeURIComponent(id)}`),
  history: (id: string) => call<{ events: { at: string; actor: string; event: string; detail: unknown }[] }>(
    `/cases/${encodeURIComponent(id)}/history`),
  plainSummary: (id: string) => call<PlainSummary>(`/cases/${encodeURIComponent(id)}/summary`),

  /** API 1. A file -> multipart (statement_source UPLOAD); no file -> JSON (the statement already on file). */
  process: (args: { caseId: string; mode: Mode; entered: EnteredDetails | null; file: File | null;
                    agencyId?: string | null; idempotencyKey?: string; statementOnFile?: boolean }) => {
    // the statement: this request's file; else the case's statement on file; else none -- only AA received can run
    // without one (its details are checked against the AA data)
    const source = args.file ? "UPLOAD"
      : args.statementOnFile === false && args.mode === "AA_SUCCESS" ? "NONE" : "ON_FILE";
    const request = {
      case_id: args.caseId, mode: args.mode, statement_source: source,
      ...(args.entered ? { entered: args.entered } : {}), ...(args.agencyId ? { agency_id: args.agencyId } : {}),
    };
    const extra: Record<string, string> = args.idempotencyKey ? { "Idempotency-Key": args.idempotencyKey } : {};
    if (args.file) {
      const body = new FormData();
      body.append("request", JSON.stringify(request));
      body.append("statement", args.file);
      return call<Run | ReadingStarted>("/process", { method: "POST", body, headers: extra });
    }
    return call<Run | ReadingStarted>("/process", { method: "POST", body: JSON.stringify(request),
                                   headers: { "Content-Type": "application/json", ...extra } });
  },

  /** AA received: waive the bank verification (a reason is required). */
  waive: (caseId: string, reason: string) =>
    call<{ case_id: string; status: string }>(`/cases/${encodeURIComponent(caseId)}/waive`, {
      method: "POST", body: JSON.stringify({ reason }), headers: { "Content-Type": "application/json" } }),
  /** AA received: the eight checks again on the AA data (with the officer's corrections, if any). */
  retrigger: (caseId: string, entered: EnteredDetails | null) =>
    call<Run | ReadingStarted>(`/cases/${encodeURIComponent(caseId)}/retrigger`, {
      method: "POST", body: JSON.stringify(entered ? { entered } : {}), headers: { "Content-Type": "application/json" } }),

  /** API 2. With a report file -> multipart; without -> JSON. */
  finalize: (caseId: string, input: AgencyInput, report: File | null) => {
    const { with_report: _ignored, ...payload } = input;
    void _ignored;
    if (report) {
      const body = new FormData();
      body.append("request", JSON.stringify(payload));
      body.append("report", report);
      return call<FinalizeResult>(`/${encodeURIComponent(caseId)}/finalize`, { method: "POST", body });
    }
    return call<FinalizeResult>(`/${encodeURIComponent(caseId)}/finalize`, {
      method: "POST", body: JSON.stringify(payload), headers: { "Content-Type": "application/json" } });
  },

  resetDemo: () => call<{ cases: number }>("/demo/reset", { method: "POST" }),
  /** Read a statement the moment it is chosen: the case keeps it, and the answer says what it fills. */
  readStatement: (caseId: string, file: File) => {
    const body = new FormData();
    body.append("request", "{}");
    body.append("statement", file);
    return call<StatementRead>(`/cases/${encodeURIComponent(caseId)}/statement`, { method: "POST", body });
  },
  reportUrl: (id: string, format: "pdf" | "html") => `${BASE}/cases/${encodeURIComponent(id)}/report?format=${format}`,
  documentUrl: (id: string, doc: string) => `${BASE}/cases/${encodeURIComponent(id)}/documents/${doc}`,
};

/** Save a file the API returns (the PDF report). */
export async function download(url: string, name: string) {
  const res = await fetch(url);
  if (!res.ok) throw new ApiError(res.status, "DOWNLOAD_FAILED", res.statusText);
  const blob = await res.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}

/** Open a stored document / the printable report in a new tab. */
export function openDocument(url: string) {
  window.open(url, "_blank", "noopener");
}
