// The legal verification API on the LOS server. The LOS login decides who the caller is; this client never claims a
// role. Errors arrive in the LOS shape: {"error": {"code", "message"}, "detail": {"code", "message", "details"}}.
import type { Config, Row, Summary, Workspace } from "../types";
import { t } from "../utils/text";

export const BASE = "/api/v1/credit/legal-verification";

export interface FieldError { field: string; message: string }

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public fields: FieldError[] = []) {
    super(message);
  }
}

async function raw(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body && typeof init.body === "string" && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, { ...init, headers, credentials: "same-origin" });
  } catch {
    throw new ApiError(0, "NETWORK", t("common.network"));
  }
  if (!res.ok) {
    let body: { error?: { code?: string; message?: string }; detail?: { details?: FieldError[] } | unknown } | null = null;
    try { body = await res.json(); } catch { /* not JSON */ }
    const details = (body?.detail as { details?: unknown } | undefined)?.details;
    throw new ApiError(res.status, body?.error?.code ?? "HTTP_ERROR", body?.error?.message ?? t("common.failed"),
                       Array.isArray(details) ? (details as FieldError[]) : []);
  }
  return res;
}

const call = async <T,>(path: string, init: RequestInit = {}) => (await (await raw(path, init)).json()) as T;
const q = encodeURIComponent;
const post = (body: unknown, headers: Record<string, string> = {}) =>
  ({ method: "POST", headers, body: JSON.stringify(body) }) as RequestInit;

export const api = {
  config: () => call<Config>("/config"),
  worklist: (search = "") => call<{ applications: Row[] }>(`/applications?q=${q(search)}`),
  workspace: (id: string) => call<Workspace>(`/applications/${q(id)}`),
  upload: (id: string, kind: string, file: File) => {
    const form = new FormData();
    form.append("kind", kind);
    form.append("file", file);
    return call<Workspace>(`/applications/${q(id)}/documents`, { method: "POST", body: form });
  },
  fileBlob: async (documentId: string) => (await raw(`/documents/${q(documentId)}/file`)).blob(),
  correct: (documentId: string, field: string, value: string, reason: string) =>
    call<Workspace>(`/documents/${q(documentId)}/corrections`, post({ field, value, reason })),
  /** the CPA enters / edits the Sale Deed's details (no OCR); changing an entered value needs `reason` */
  saveDeed: (id: string, values: Record<string, unknown>, reason: string) =>
    call<Workspace>(`/applications/${q(id)}/sale-deed`, post({ values, reason })),
  /** "Fill sample" (demo): random realistic values for the Sale Deed details or the agency report */
  sample: (id: string, part: "deed" | "report" | "application") => call<{ values: Record<string, unknown> }>(`/applications/${q(id)}/sample?part=${part}`),
  /** Save: the legal vendor chosen in the Application Details (the case is not sent) */
  /** the CPA's edit of the Application Details (audited; PANs masked by the server) */
  editApplication: (id: string, values: Record<string, unknown>) =>
    call<Workspace>(`/applications/${q(id)}/details`, { method: "PUT", body: JSON.stringify({ values }) }),
  saveVendor: (id: string, agency: string) => call<Workspace>(`/applications/${q(id)}/vendor`, post({ agency })),
  /** Waive the legal verification (a reason is required) */
  waive: (id: string, reason: string) => call<Workspace>(`/applications/${q(id)}/waive`, post({ reason })),
  send: (id: string, agency: string, key: string) =>
    call<Workspace>(`/applications/${q(id)}/send`, post({ agency, confirmation_accepted: true }, { "Idempotency-Key": key })),
  submitReport: (id: string, answers: Record<string, unknown>) =>
    call<Workspace>(`/applications/${q(id)}/agency-report`, post({ answers })),
  /** after the report is submitted: generate the agency's report PDF from it (analysis + answers) and attach it */
  generateReport: (id: string) => call<Workspace>(`/applications/${q(id)}/agency-report/generate`, { method: "POST" }),
  validate: (id: string) => call<Workspace>(`/applications/${q(id)}/validate`, { method: "POST" }),
  summary: (id: string) => call<Summary>(`/applications/${q(id)}/summary`),
  decide: (id: string, decision: string, remarks: string) =>
    call<Workspace>(`/applications/${q(id)}/decision`, post({ decision, remarks })),
  reportBlob: async (id: string) => (await raw(`/applications/${q(id)}/report`)).blob(),
};

export function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
}

export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
