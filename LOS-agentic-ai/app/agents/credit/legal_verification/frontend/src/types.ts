// The shapes the legal verification API returns (app/agents/credit/legal_verification/services/cases.py).

export type RuleStatus = "FAIL" | "REVIEW" | "PASS" | "NOT_APPLICABLE";
export type Recommendation = "CLEAR_FOR_REVIEW" | "REVIEW_REQUIRED" | "ESCALATE";
export type Source = "APPLICATION" | "SALE_DEED" | "CORRECTED" | "AGENCY" | "AGENT";

export interface Option { id: string; label: string }

export interface FormField {
  id: string; label: string; type: "text" | "textarea" | "date" | "number" | "select" | "yesno" | "list" | "chain"
    | "searches" | "checks" | "people" | "yesnona" | "table";
  required?: boolean; options: Option[];
  /** a table field's columns (their type, label and options from config) */
  columns: { id: string; label: string; type: "text" | "date" | "select"; options: Option[] }[];
}

export interface Config {
  user: { id: string; name: string; permissions: string[] };
  screen: Record<string, unknown>;
  statuses: Record<string, string>;
  rule_statuses: Record<string, string>;
  sources: Record<string, string>;
  recommendations: Record<string, string>;
  document_kinds: Option[];
  max_mb: number;
  allowed_types: string[];
  agencies: { id: string; name: string }[];
  default_agency: string | null;
  decisions: { id: string; label: string; remarks_required: boolean }[];
  proceed_allowed_for: string[];
  deed_fields: Option[];
  form: {
    sections: { id: string; label: string; fields: FormField[] }[];
    table_words: { add: string; remove: string };
    check_states: Option[];
    search_types: Option[];
    legal_checks: Record<string, Option[]>;
  };
  rules: { id: string; name: string; explain: string }[];
  ui: { activity_visible?: number; switch_ms?: number; loader_line_ms?: number;
        typewriter?: { type_ms?: number; erase_ms?: number; hold_ms?: number; rest_ms?: number } };
  show_reset: boolean;
  show_fill_sample: boolean;
  los_nav: { id: string; label: string; link: string; active: boolean }[];
  application_fields: { id: string; label: string; options: Option[] | null }[];
  application_form: { id: string; label: string; fields: string[] }[];
  title_search_years: number;
  agency_kinds: string[];
}

export interface Comparison { label: string; values: Partial<Record<Source, string>>; match: boolean | null; note: string | null }

export interface Rule {
  id: string; number: number; name: string; explain: string; why: string | null; status: RuleStatus;
  status_label: string; severity: string | null; severity_label: string | null; reason: string;
  comparisons: Comparison[]; missing: string[]; action: string | null; action_text: string | null;
}

export interface Overview {
  recommendation: Recommendation; recommendation_label: string; recommendation_note: string;
  counts: { critical: number; failed: number; review: number; passed: number };
  findings: string[]; missing: string[]; next_action: string; next_action_text: string;
}

export interface Run { run_id: string; run_at: string; run_by: string; rules: Rule[]; overview: Overview }

export interface Doc {
  document_id: string; kind: string; kind_label: string; file_name: string; content_type: string; size_bytes: number;
  pages: number | null; read_status: string; read_label: string; notes: string[]; uploaded_by: string; uploaded_at: string;
}

export interface DeedField { field: string; label: string; value: string | null; source: Source | null }

export interface Workspace {
  application: {
    application_id: string; applicant_name: string; branch: string; product: string; loan_amount: number; stage: string;
    property_type: string; property_type_label: string; property_address: string; property_pin: string;
    owner_names: string[];
    view: { id: string; label: string; fields: { id: string; label: string; value: string | null }[] }[];
    edit: Record<string, string | null>;
    geo: { id: string; label: string; value: string | null }[];
    collaterals: string[];
    assigned_to: string | null; los_status: string | null; collateral_no: string | null;
  };
  case: {
    case_id: string; status: string; status_label: string; agency: string | null; agency_name: string | null;
    sent_by: string | null; sent_at: string | null; decision: string | null; decision_label: string | null;
    decision_remarks: string | null; decided_by: string | null; decided_at: string | null;
    selected_agency: string | null; waive_reason: string | null; waived_by: string | null; waived_at: string | null;
  };
  documents: Doc[];
  deed: DeedField[] | null;
  deed_document_id: string | null;
  agency_report: { report_id: string; agency: string; submitted_by: string; submitted_at: string;
                   answers: Record<string, unknown>; document_id: string | null;
                   analysis: { tone: "ok" | "warn" | "bad" | "info"; text: string }[] } | null;
  run: Run | null;
  events: { event_id: number; actor: string; event: string; label: string; detail: string; at: string }[];
  can: { upload: boolean; correct: boolean; send: boolean; submit_report: boolean; validate: boolean; decide: boolean;
         download: boolean; save: boolean; waive: boolean; edit_application: boolean; attach_report: boolean;
         generate_report: boolean };
}

export interface Row {
  application_id: string; applicant_name: string; branch: string; loan_amount: number; property_type_label: string;
  status: string; status_label: string; agency_name: string | null; recommendation: Recommendation | null;
  recommendation_label: string | null;
}

export interface Summary {
  available: boolean; run_id?: string; points?: string[]; source?: "ai" | "rules"; pending?: boolean; label?: string;
  note?: string;
}
