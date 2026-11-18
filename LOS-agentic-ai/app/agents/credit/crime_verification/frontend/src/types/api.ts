// The shapes the crime verification API returns (app/agents/credit/crime_verification/api/routes.py).

/** "PRIMARY" or "CO_APPLICANT"; a person is (role, seq) and is keyed by `party` ("PRIMARY", "CO_APPLICANT-2"). */
export type Role = string;
export type Status = "INITIATED" | "PROCESSING" | "COMPLETED" | "REVIEW_REQUIRED" | "FAILED" | "WAIVED";
export type FieldName = "full_name" | "father_name" | "date_of_birth" | "permanent_address" | "pan";

export interface FieldError { field: string; message: string }

export interface UiConfig {
  demo_mode: boolean;
  banner: string | null;
  user: { id: string; permissions: string[] };
  vendors: { id: string; name: string }[];
  default_vendor: string;
  validation: {
    pan_pattern: string; name_pattern: string; name_max: number; address_min: number; address_max: number;
    min_age_years: number; max_age_years: number;
  };
  messages: Record<string, string>;
  fields: Record<FieldName, string>;
  roles: Record<Role, string>;
  risk: { labels: Record<string, string>; meaning: Record<string, string>; short?: Record<string, string> };
  statuses: Record<string, string>;
  decisions: { id: string; label: string }[];
  remarks_required_for: string[];
  scenarios: { id: string; label: string }[];
  retrigger_needed_after: string[];
  blocked_while: string[];
  decision_labels: Record<string, string>;
  summary_labels: Record<string, string>;
  show_reset: boolean;
  screen: { [key: string]: string | { [key: string]: unknown } };
  roles_numbered: Record<string, string>;
  max_co_applicants: number;
  form: { pin_pattern: string; states: Record<string, { pin_prefixes: string[]; cities: string[] }> };
  form_fields: Record<"address_line" | "state" | "city" | "pin", string>;
  samples: Samples | null;
  celebration: { title: string; subtitle: string; sound_on: string; sound_off: string };
  alarm: { title: string; subtitle: string };
  ui: { verification_tabs: string[]; active_tab: string; refresh_seconds: number; celebrate_risks: string[];
        celebration_sound?: boolean; alarm_risks?: string[]; alarm_sound?: boolean;
        compact_people_from?: number; activity_visible?: number };
}

export interface CoverageItem { group: string; code: string; name: string; coverage_since: number; cases_found: number }

export interface Result {
  result_id: string;
  provider_reference: string;
  outcome: string;
  outcome_label: string;
  risk_level: string;
  risk_label: string;
  risk_meaning: string;
  total_cases: number;
  summary: string;
  groups: { group: string; label: string; items: CoverageItem[] }[];
  clusters: { cluster: string; label: string; count: number }[];
  result_at: string;
  synthetic: boolean;
}

export interface Subject {
  subject_id: string;
  case_id: string;
  role: Role;
  seq: number;
  party: string;
  role_label: string;
  full_name: string;
  father_name: string;
  date_of_birth: string;
  permanent_address: string;
  pan_masked: string;
  status: Status;
  status_label: string;
  provider_reference: string | null;
  outcome: string | null;
  outcome_label: string;
  error: { code: string; message: string } | null;
  result: Result | null;
  report: { report_id: string; version: string; generated_at: string } | null;
  updated_at: string;
}

export interface Case {
  case_id: string;
  application_id: string;
  vendor: string;
  vendor_name: string;
  status: Status;
  status_label: string;
  kind: string;
  initiated_by: string;
  initiated_at: string;
  updated_at: string;
  waived_reason: string | null;
  final_decision: string | null;
  final_decision_label: string;
  final_remarks: string | null;
  finalized_by: string | null;
  finalized_at: string | null;
  subjects: Subject[];
  synthetic: boolean;
  can: { refresh: boolean; finalize: boolean };
  replayed?: boolean;
}

export interface ApplicationSubject {
  role: Role;
  seq: number;
  party: string;
  role_label: string;
  full_name: string;
  father_name: string;
  date_of_birth: string;
  permanent_address: string;
  pan_masked: string;
  source: string;
  missing: string[];
  errors: FieldError[];
  latest: Subject | null;
  demo_scenario: string | null;
}

export interface ApplicationView {
  application: {
    application_id: string; title: string; branch: string; product: string; loan_amount: number; stage: string;
    status: string; applicant_name: string;
  };
  subjects: ApplicationSubject[];
  latest_case: Case | null;
  cases: { case_id: string; status: Status; status_label: string; kind: string; initiated_at: string; initiated_by: string }[];
  can: Record<"initiate" | "retrigger" | "refresh" | "finalize" | "download_report" | "reset_demo", boolean>;
}

export interface ApplicationRow {
  application_id: string; title: string; product: string; loan_amount: number; stage: string; branch: string;
  applicant: string | null; latest_status: Status | null; latest_status_label: string;
}

export interface CaseRow {
  case_id: string; application_id: string; vendor: string; vendor_name: string; status: Status; status_label: string;
  kind: string; initiated_at: string; final_decision: string | null; applicant: string | null; subject_count: number;
  results: { role: Role; seq: number; party: string; role_label: string; status: Status; status_label: string; risk: string | null;
             risk_label: string }[];
}

export interface Dashboard {
  applications: number; total: number; in_progress: number; completed: number; review_required: number;
  failed: number; waived: number;
}

export interface AuditEvent {
  event_id: number; case_id?: string; actor: string; event_type: string; label: string; at: string;
  metadata: Record<string, unknown>;
}

export interface SubjectInput {
  role: Role;
  seq: number;
  full_name: string;
  father_name: string;
  date_of_birth: string;
  permanent_address: string;
  pan?: string;
}

export interface CaseSummary {
  case_id: string; available: boolean; text: string | null; source: "ai" | "rules" | null; pending: boolean;
  label: string | null;
}

export interface Samples {
  first_names: string[]; last_names: string[]; father_first_names: string[]; address_lines: string[]; areas: string[];
  age_years: [number, number];
}
