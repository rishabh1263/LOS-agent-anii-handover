// The crime verification API on the LOS server (Credit Underwriting). The LOS login decides who the caller is and
// what it may do; this client never claims a role. Errors come in the LOS shape:
//   {"error": {"code", "message"}, "detail": {"code", "message", "details"}}  (details: field errors)
import { t } from "../utils/text";
import type {
  ApplicationRow, ApplicationView, AuditEvent, Case, CaseRow, CaseSummary, Dashboard, FieldError, SubjectInput, UiConfig,
} from "../types/api";

export const BASE = "/api/v1/credit/crime-verification";

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public fields: FieldError[] = []) {
    super(message);
  }
}

function fieldErrors(detail: unknown): FieldError[] {
  if (Array.isArray(detail)) {                                  // a request the schema refused
    return detail.map((d: { loc?: unknown[]; msg?: string }) => ({
      field: (d.loc ?? []).filter((p) => p !== "body").join("."), message: d.msg ?? "",
    }));
  }
  const details = (detail as { details?: unknown } | null)?.details;
  return Array.isArray(details) ? (details as FieldError[]) : [];
}

async function raw(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "NETWORK", t("common.network"));
  }
  if (!res.ok) {
    let body: { error?: { code?: string; message?: string }; detail?: unknown } | null = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    throw new ApiError(res.status, body?.error?.code ?? "HTTP_ERROR",
                       body?.error?.message ?? t("common.request_failed"), fieldErrors(body?.detail));
  }
  return res;
}

const call = async <T,>(path: string, init: RequestInit = {}) => (await (await raw(path, init)).json()) as T;
const q = (v: string) => encodeURIComponent(v);

export const api = {
  config: () => call<UiConfig>("/config"),
  dashboard: () => call<Dashboard>("/dashboard"),
  applications: (search = "") => call<{ applications: ApplicationRow[] }>(`/applications?q=${q(search)}`),
  application: (id: string) => call<ApplicationView>(`/applications/${q(id)}`),
  applicationAudit: (id: string) => call<{ events: AuditEvent[] }>(`/applications/${q(id)}/audit`),
  caseDetail: (id: string) => call<Case>(`/cases/${q(id)}`),
  caseAudit: (id: string) => call<{ events: AuditEvent[] }>(`/cases/${q(id)}/audit`),
  cases: (search = "", status = "") => call<{ cases: CaseRow[] }>(`/cases?q=${q(search)}&status=${q(status)}`),

  /** API 1 -- called only from the confirmation popup's Confirm button. */
  initiate: (body: { application_id: string; vendor: string; subjects: SubjectInput[]; demo_scenario?: string | null;
                     retrigger?: boolean }, idempotencyKey: string) =>
    call<Case>("/initiate", {
      method: "POST", headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ ...body, confirmation_accepted: true }),
    }),
  summary: (caseId: string) => call<CaseSummary>(`/cases/${q(caseId)}/summary`),
  refresh: (caseId: string) => call<Case>(`/${q(caseId)}/refresh`, { method: "POST" }),
  /** API 2 */
  finalize: (caseId: string, decision: string, remarks: string) =>
    call<Case>(`/${q(caseId)}/finalize`, { method: "POST", body: JSON.stringify({ decision, remarks }) }),
  reportBlob: async (subjectId: string) => (await raw(`/subjects/${q(subjectId)}/report`)).blob(),
  caseReportBlob: async (caseId: string) => (await raw(`/cases/${q(caseId)}/report`)).blob(),
  resetDemo: () => call<{ counts: Record<string, number> }>("/demo/reset", { method: "POST" }),
};
