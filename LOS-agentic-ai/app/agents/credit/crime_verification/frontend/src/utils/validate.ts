// The same field rules the server applies (config validation, messages from the wording file via GET /config).
import type { FieldName, SubjectInput, UiConfig } from "../types/api";

export type Errors = Partial<Record<FieldName, string>>;

const fill = (text: string, values: Record<string, string | number>) =>
  text.replace(/\{(\w+)\}/g, (_, k: string) => String(values[k] ?? ""));

function age(born: Date, today: Date) {
  const before = today.getMonth() < born.getMonth()
    || (today.getMonth() === born.getMonth() && today.getDate() < born.getDate());
  return today.getFullYear() - born.getFullYear() - (before ? 1 : 0);
}

function parseDate(value: string): Date | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return d.getFullYear() === Number(m[1]) && d.getMonth() === Number(m[2]) - 1 && d.getDate() === Number(m[3]) ? d : null;
}

/** Field errors of one subject. `pan` is checked only when the officer typed one (else the application's is used). */
export function validateSubject(s: Omit<SubjectInput, "seq">, config: UiConfig, today = new Date()): Errors {
  const v = config.validation;
  const m = config.messages;
  const label = config.fields;
  const errors: Errors = {};
  for (const field of ["full_name", "father_name"] as const) {
    const value = (s[field] ?? "").trim();
    if (!value) errors[field] = fill(m.required, { field: label[field] });
    else if (value.length > v.name_max) errors[field] = fill(m.too_long, { field: label[field], max: v.name_max });
    else if (!new RegExp(v.name_pattern).test(value)) errors[field] = fill(m.name_format, { field: label[field] });
  }
  const address = (s.permanent_address ?? "").trim();
  if (!address) errors.permanent_address = fill(m.required, { field: label.permanent_address });
  else if (address.length < v.address_min) errors.permanent_address = fill(m.address_short, { min: v.address_min });
  else if (address.length > v.address_max)
    errors.permanent_address = fill(m.too_long, { field: label.permanent_address, max: v.address_max });
  const dob = (s.date_of_birth ?? "").trim();
  const born = dob ? parseDate(dob) : null;
  if (!dob) errors.date_of_birth = fill(m.required, { field: label.date_of_birth });
  else if (!born) errors.date_of_birth = m.date_format;
  else if (born > today) errors.date_of_birth = m.date_future;
  else {
    const years = age(born, today);
    if (years < v.min_age_years || years > v.max_age_years)
      errors.date_of_birth = fill(m.age_range, { min: v.min_age_years, max: v.max_age_years });
  }
  if (s.pan !== undefined) {
    const pan = s.pan.trim().toUpperCase();
    if (!pan) errors.pan = fill(m.required, { field: label.pan });
    else if (!new RegExp(v.pan_pattern).test(pan)) errors.pan = m.pan_format;
  }
  return errors;
}

export const maskPan = (pan: string) => (pan.length > 4 ? "X".repeat(pan.length - 4) + pan.slice(-4) : pan);
