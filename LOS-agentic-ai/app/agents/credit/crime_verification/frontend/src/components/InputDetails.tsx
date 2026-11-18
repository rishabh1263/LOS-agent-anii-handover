import type { ReactNode } from "react";
import type { UiConfig } from "../types/api";
import { type AddressParts, citiesOf, composeAddress } from "../utils/address";
import { ageOf } from "../utils/sample";
import { validateSubject } from "../utils/validate";
import { t } from "../utils/text";
import { Icon } from "./Icon";

export interface FormValues {
  full_name: string;
  father_name: string;
  date_of_birth: string;
  address: AddressParts;
  /** false: the application's address did not split into line / city / state / PIN -- edited as one text */
  structured: boolean;
  /** undefined: the application's PAN is used (shown masked); a string: the officer is typing the full PAN */
  pan?: string;
}

export type FormField = "full_name" | "father_name" | "date_of_birth" | "pan" | "address_line" | "state" | "city" | "pin";
export type FormErrors = Partial<Record<FormField, string>>;

export const addressOf = (v: FormValues) => (v.structured ? composeAddress(v.address) : v.address.line.trim());

/** Every field error of the form: the server's rules (validateSubject) plus the address parts (config form). */
export function validateForm(v: FormValues, config: UiConfig): FormErrors {
  const base = validateSubject({ role: "PRIMARY", full_name: v.full_name, father_name: v.father_name,
                                 date_of_birth: v.date_of_birth, permanent_address: addressOf(v), pan: v.pan }, config);
  const errors: FormErrors = { full_name: base.full_name, father_name: base.father_name,
                               date_of_birth: base.date_of_birth, pan: base.pan };
  const req = (label: string) => config.messages.required.replace("{field}", label);
  if (v.structured) {
    const f = config.form_fields;
    if (!v.address.line.trim()) errors.address_line = req(f.address_line);
    else if (base.permanent_address) errors.address_line = base.permanent_address;
    if (!v.address.state) errors.state = req(f.state);
    if (!v.address.city) errors.city = req(f.city);
    if (!v.address.pin) errors.pin = req(f.pin);
    else if (!new RegExp(config.form.pin_pattern).test(v.address.pin)) errors.pin = config.messages.pin_format;
  } else if (base.permanent_address) {
    errors.address_line = base.permanent_address;
  }
  return Object.fromEntries(Object.entries(errors).filter(([, m]) => m)) as FormErrors;
}

function Field({ id, label, error, source, hint, wide, children }: {
  id: string; label: string; error?: string; source?: ReactNode; hint?: ReactNode; wide?: boolean; children: ReactNode;
}) {
  return (
    <div className={`field${wide ? " span-2" : ""}${error ? " has-error" : ""}`}>
      <div className="field-label"><label htmlFor={id}>{label} <span className="req" aria-hidden="true">*</span></label>{source}</div>
      {children}
      {error ? <p className="field-error" id={`${id}-err`}><Icon name="alert" size={13} /> {error}</p>
             : hint ? <p className="field-hint">{hint}</p> : null}
    </div>
  );
}

/** Input Details of one person: identity, then the permanent address (line + state + city + PIN). */
export function InputDetails({ values, original, panMasked, errors, touched, readOnly, config, onChange, onTouch }: {
  values: FormValues;
  original: FormValues;
  panMasked: string;
  errors: FormErrors;
  touched: boolean;
  readOnly: boolean;
  config: UiConfig;
  onChange: (next: FormValues) => void;
  onTouch: () => void;
}) {
  const L = config.fields;
  const F = config.form_fields;
  const show = (field: FormField, changed: boolean) => (touched || changed ? errors[field] : undefined);
  const src = (same: boolean, filled: boolean) =>
    same && filled ? <span className="source">{t("form.from_application")}</span>
                   : filled ? <span className="source edited">{t("form.edited")}</span> : null;
  const a = values.address;
  const o = original.address;
  const set = (patch: Partial<FormValues>) => onChange({ ...values, ...patch });
  const setAddress = (patch: Partial<AddressParts>) => onChange({ ...values, address: { ...a, ...patch } });
  const aria = (field: FormField, err?: string) => ({ "aria-invalid": !!err, "aria-describedby": err ? `f-${field}-err` : undefined });
  const age = ageOf(values.date_of_birth);

  const text = (field: "full_name" | "father_name", icon: string) => {
    const err = show(field, values[field] !== original[field]);
    return (
      <Field id={`f-${field}`} label={L[field]} error={err} source={src(values[field] === original[field], !!values[field])}>
        <div className="input-icon">
          <Icon name={icon} />
          <input id={`f-${field}`} className={`input${err ? " has-error" : ""}`} value={values[field]} disabled={readOnly}
                 autoComplete="off" onBlur={onTouch} onChange={(e) => set({ [field]: e.target.value } as Partial<FormValues>)}
                 {...aria(field, err)} />
        </div>
      </Field>
    );
  };

  const dobErr = show("date_of_birth", values.date_of_birth !== original.date_of_birth);
  const panErr = values.pan !== undefined ? show("pan", true) : undefined;
  const lineErr = show("address_line", a.line !== o.line);
  const stateErr = show("state", a.state !== o.state);
  const cityErr = show("city", a.city !== o.city);
  const pinErr = show("pin", a.pin !== o.pin);
  const addressSame = addressOf(values) === addressOf(original);

  return (
    <div className="form">
      <section className="form-section">
        <h3 className="form-section-title"><Icon name="user" /> {t("form.identity")}</h3>
        <div className="form-grid">
          {text("full_name", "user")}
          {text("father_name", "users")}
          <Field id="f-date_of_birth" label={L.date_of_birth} error={dobErr}
                 source={src(values.date_of_birth === original.date_of_birth, !!values.date_of_birth)}
                 hint={age !== null ? t("form.age", { n: age }) : undefined}>
            <input id="f-date_of_birth" type="date" className={`input${dobErr ? " has-error" : ""}`} value={values.date_of_birth}
                   max={new Date().toISOString().slice(0, 10)} disabled={readOnly} onBlur={onTouch}
                   onChange={(e) => set({ date_of_birth: e.target.value })} {...aria("date_of_birth", dobErr)} />
          </Field>
          <Field id="f-pan" label={L.pan} error={panErr}
                 source={values.pan === undefined ? <span className="source">{t("form.from_application")}</span>
                                                  : <span className="source edited">{t("form.entered")}</span>}
                 hint={values.pan !== undefined ? t("form.pan_must_match") : t("form.pan_private")}>
            {values.pan === undefined ? (
              <div className="pan-row">
                <div className="input-icon"><Icon name="lock" />
                  <input id="f-pan" className="input mono" value={panMasked} readOnly aria-readonly="true" /></div>
                {!readOnly && (
                  <button type="button" className="btn btn-ghost btn-sm" onClick={() => set({ pan: "" })}><Icon name="edit" /> {t("form.pan_enter")}</button>
                )}
              </div>
            ) : (
              <div className="pan-row">
                <input id="f-pan" className={`input mono${panErr ? " has-error" : ""}`} value={values.pan} maxLength={10} autoComplete="off"
                       spellCheck={false} disabled={readOnly} onBlur={onTouch} placeholder="ABCPE1234F"
                       onChange={(e) => set({ pan: e.target.value.toUpperCase() })} {...aria("pan", panErr)} />
                <button type="button" className="btn btn-ghost btn-sm" onClick={() => set({ pan: undefined })}>{t("form.pan_use_application")}</button>
              </div>
            )}
          </Field>
        </div>
      </section>

      <section className="form-section">
        <h3 className="form-section-title"><Icon name="home" /> {t("form.address")} {src(addressSame, !!addressOf(values))}</h3>
        {values.structured ? (
          <div className="form-grid cols-3">
            <Field id="f-address_line" label={F.address_line} error={lineErr} wide>
              <input id="f-address_line" className={`input${lineErr ? " has-error" : ""}`} value={a.line} disabled={readOnly}
                     onBlur={onTouch} onChange={(e) => setAddress({ line: e.target.value })} {...aria("address_line", lineErr)} />
            </Field>
            <Field id="f-state" label={F.state} error={stateErr}>
              <select id="f-state" className={`input${stateErr ? " has-error" : ""}`} value={a.state} disabled={readOnly} onBlur={onTouch}
                      onChange={(e) => setAddress({ state: e.target.value, city: "" })} {...aria("state", stateErr)}>
                <option value="">{t("form.choose_state")}</option>
                {Object.keys(config.form.states).map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </Field>
            <Field id="f-city" label={F.city} error={cityErr}>
              <select id="f-city" className={`input${cityErr ? " has-error" : ""}`} value={a.city} disabled={readOnly || !a.state}
                      onBlur={onTouch} onChange={(e) => setAddress({ city: e.target.value })} {...aria("city", cityErr)}>
                <option value="">{a.state ? t("form.choose_city") : t("form.choose_state_first")}</option>
                {citiesOf(a.state, a.city, config).map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
            </Field>
            <Field id="f-pin" label={F.pin} error={pinErr}>
              <input id="f-pin" className={`input mono${pinErr ? " has-error" : ""}`} value={a.pin} inputMode="numeric" maxLength={6}
                     disabled={readOnly} onBlur={onTouch} placeholder="400069"
                     onChange={(e) => setAddress({ pin: e.target.value.replace(/\D/g, "") })} {...aria("pin", pinErr)} />
            </Field>
          </div>
        ) : (
          <Field id="f-address_line" label={L.permanent_address} error={lineErr} wide
                 hint={!readOnly ? <button type="button" className="btn btn-link" onClick={() => set({ structured: true })}>
                   {t("form.structured")}</button> : undefined}>
            <textarea id="f-address_line" className={`input${lineErr ? " has-error" : ""}`} rows={2} value={a.line} disabled={readOnly}
                      onBlur={onTouch} onChange={(e) => setAddress({ line: e.target.value })} {...aria("address_line", lineErr)} />
          </Field>
        )}
        {addressOf(values) && <p className="address-preview"><Icon name="info" size={13} /> {addressOf(values)}</p>}
      </section>
    </div>
  );
}
