// The bank verification API's shapes (app/agents/credit/bank_verification/api.py).

export type RuleStatus = "PASS" | "FAIL" | "REVIEW_REQUIRED" | "NOT_APPLICABLE";
export type Mode = "AA_SUCCESS" | "AA_FAILURE";
export type Direction = "CREDIT" | "DEBIT";
export type Provenance = "AA" | "EXTRACTED" | "MANUAL" | "AGENCY" | "APPLICATION" | "CONFIRMED";
export type Role = "CPA" | "AGENCY" | "VIEWER";
export type ChecklistAnswer = "VERIFIED" | "MISMATCH" | "NOT_VERIFIED";
export type FinalStatus = "POSITIVE" | "NEGATIVE" | "REVIEW_REQUIRED";

export interface Comparison {
  field: string;
  left_label: string;
  left: string | null;
  right_label: string;
  right: string | null;
  match: boolean | null;
  note: string | null;
}

export interface RiskFlag {
  code: string;
  severity: "LOW" | "MEDIUM" | "HIGH";
  message: string;
  rule_id: string;
}

export interface RuleResult {
  rule_id: string;
  rule_name: string;
  status: RuleStatus;
  reason: string;
  comparisons: Comparison[];
  evidence: string[];
  missing_evidence: string[];
  risk_flags: RiskFlag[];
  recommended_action: string | null;
  details: Record<string, unknown>;
}

export interface EnteredTransaction {
  date: string | null;
  amount: string | null;
  direction: Direction | null;
}

export interface EnteredDetails {
  bank_name: string | null;
  account_number: string | null;
  account_type: string | null;
  transactions: EnteredTransaction[];
  provenance: Record<string, Provenance>;
}

export interface SourceData {
  details: { bank_name: string | null; account_number: string | null; account_type: string | null;
             ifsc: string | null; account_holder: string | null };
  transactions: { date: string; amount: string; direction: Direction; balance: string | null; narration: string }[];
  opening_balance: string | null;
  closing_balance: string | null;
  period_start: string | null;
  period_end: string | null;
}

export interface StatementFile {
  document_id: string | null;
  file_name: string | null;
  kind: string | null;
  size: number;
  pages: number;
  pages_with_text: number;
  extraction_status: "COMPLETE" | "PARTIAL" | "UNREADABLE" | "INVALID";
  notes: string[];
  seeded: boolean;
}

export interface Run {
  case_id: string;
  run_id: string;
  mode: Mode;
  source: "AA" | "MANUAL";
  processing_status: string;
  final_status: string | null;
  overall_status: RuleStatus;
  rules: RuleResult[];
  risk_flags: RiskFlag[];
  missing_evidence: string[];
  mismatches: string[];
  checks: { checks_applicable: number; checks_passed: number; checks_failed: number; checks_review: number;
            not_applicable: string[]; score_note: string };
  recommended_action: string;
  explanation: string;
  form: EnteredDetails;
  sources: { aa: SourceData | null; statement: SourceData | null; statement_file: StatementFile | null };
  replayed: boolean;
}

export interface CaseRow {
  case_id: string;
  application_ref: string;
  applicant_name: string;
  loan_amount: string;
  product: string;
  stage: string;
  mode: Mode;
  status: string;
  risk_level: string | null;
  assigned_agency: string | null;
  scenario: string | null;
  updated_at: string;
}

export interface AgencyInput {
  agency_id: string;
  checklist: Record<string, ChecklistAnswer>;
  branch_visit: "COMPLETED" | "SCHEDULED" | "NOT_DONE";
  ownership: "CONFIRMED" | "NOT_CONFIRMED" | "PENDING";
  requested_decision: FinalStatus;
  rationale: string;
  remarks?: string | null;
  with_report?: boolean;
}

export interface Decision {
  decision_id: string;
  final_status: string;
  requested: string;
  payload: AgencyInput & { report_document_id: string | null };
  outcome: { blocked_reasons: string[]; negative_findings: string[] };
  decided_by: string;
  created_at: string;
}

export interface DocumentRow {
  document_id: string;
  role: "STATEMENT" | "REPORT";
  file_name: string | null;
  kind: string | null;
  size: number;
  seeded: number;
  created_at: string;
  sha256: string;
}

export interface CaseDetail {
  case: CaseRow;
  latest_run: Run | null;
  aa_response: { provider: string; simulated: boolean; data: SourceData } | null;
  prefill: EnteredDetails | null;
  documents: DocumentRow[];
  decisions: Decision[];
  runs: { run_id: string; created_at: string }[];
  demo: { scenario: string; title: string; suggested_agency_input: AgencyInput } | null;
}

export interface PlainSummary {
  run_id: string;
  text: string;
  source: "ai" | "rules";
  pending: boolean;
  note: string;
  label: string;
  checklist: { rule_id: string; name: string; status: RuleStatus; status_label: string; text: string }[];
}

export interface AppConfig {
  demo_mode: boolean;
  policy_status: string;
  agencies: { id: string; name: string }[];
  checklist: { id: string; label: string }[];
  actions: Record<string, string>;
  transactions_count: number;
  role: Role;
  user: string;
  permissions: string[];
  suggestions: { banks: { name: string; codes: string[] }[]; account_types: string[]; max_shown: number;
                 earliest_date?: string | null };
}

export interface Summary {
  total: number;
  verified: number;
  negative: number;
  review_required: number;
  ready: number;
  pending: number;
  by_source: Record<Mode, number>;
}

/** The agency's report read and checked against the case (basic checks, then report vs screen). */
export interface ReportCheck {
  status: "PASS" | "FAIL" | "REVIEW_REQUIRED";
  reason: string;
  checks: Comparison[];
  comparisons: Comparison[];
}

export interface FinalizeResult {
  case_id: string;
  final_status: FinalStatus;
  requested_decision: FinalStatus;
  is_final: boolean;
  blocked_reasons: string[];
  negative_findings: string[];
  rules: RuleResult[];
  report_document_id: string | null;
  report_check?: ReportCheck | null;
}

/** The statement read at upload (before the run): what it fills on the form, each value for the CPA to check. */
export interface StatementRead {
  case_id: string;
  status: "READ" | "READING";
  extraction_status?: string;
  transactions_read?: number;
  latest_uncertain?: boolean;
  notes?: string[];
  filled?: EnteredDetails | null;
}

export interface ReadingStarted {
  case_id: string;
  processing_status: "READING";
  pages: number;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: unknown };
}
