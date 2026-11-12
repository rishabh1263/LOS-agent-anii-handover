import { useEffect, useMemo, useState } from "react";
import { ApiError, api } from "../services/api";
import type { AppConfig, CaseDetail, Direction, EnteredDetails, EnteredTransaction, Mode, Provenance, Run } from "../types/api";
import { Icon } from "./Icon";
import type { Viewing } from "./StatementViewer";
import { Suggest } from "./Suggest";
import { Card, Field, Notice, SourceTag, StatusBadge, Value } from "./ui";

const blankRow: EnteredTransaction = { date: null, amount: null, direction: null };

function rowsOf(form: EnteredDetails | undefined, count: number): EnteredTransaction[] {
  const rows = [...(form?.transactions ?? [])];
  while (rows.length < count) rows.push({ ...blankRow });
  return rows.slice(0, count);
}

/** The form shown: the last run's, else the AA prefill (AA received), else empty -- never demo values. */
function formOf(run: Run | null, prefill: EnteredDetails | null, count: number): EnteredDetails {
  const base = run?.form ?? prefill ?? { bank_name: null, account_number: null, account_type: null, transactions: [], provenance: {} };
  return { ...base, transactions: rowsOf(base, count) };
}

/** What the statement read filled, put into the form where the officer has not typed anything yet. */
export function withRead(form: EnteredDetails, read: EnteredDetails | null | undefined): EnteredDetails {
  if (!read) return form;
  const provenance = { ...form.provenance };
  const filled = { bank_name: form.bank_name, account_number: form.account_number, account_type: form.account_type };
  for (const key of ["bank_name", "account_number", "account_type"] as const) {
    if (!filled[key] && read[key]) {
      filled[key] = read[key];
      provenance[key] = read.provenance[key] ?? "EXTRACTED";
    }
  }
  const transactions = form.transactions.map((t, i) => {
    const r = read.transactions[i];
    if (!r || t.date || t.amount || t.direction) return t;
    provenance[`transaction_${i + 1}`] = read.provenance[`transaction_${i + 1}`] ?? "EXTRACTED";
    return { date: r.date, amount: r.amount, direction: r.direction };
  });
  return { ...form, ...filled, transactions, provenance };
}

const today = new Date().toISOString().slice(0, 10);    // a statement transaction is never in the future
const dmy = (iso: string | null) => (iso ? iso.split("-").reverse().join("-") : null);

/**
 * The bank account part of the case, as a form (like the LOS Bank Verification tab): bank name, account number,
 * account type, the latest transactions, the statement and the agency -- each value tagged with where it came from.
 * With AA data received the values arrive filled (Edit to correct); without it the officer types them.
 */
export function BankAccountPanel({ detail, config, onProcessed, onView }:
  { detail: CaseDetail; config: AppConfig; onProcessed: () => void; onView: (v: Viewing) => void }) {
  const hints = config.suggestions ?? { banks: [], account_types: [], max_shown: 8 };   // an older server sends none
  const run = detail.latest_run;
  const count = config.transactions_count;
  const closed = ["POSITIVE", "NEGATIVE", "WAIVED"].includes(detail.case.status);
  const reading = detail.case.status === "READING";
  const hasAA = Boolean(detail.aa_response);
  const [mode, setMode] = useState<Mode>(detail.case.mode);
  const [form, setForm] = useState<EnteredDetails>(() => formOf(run, detail.prefill, count));
  const [editing, setEditing] = useState(detail.case.mode === "AA_FAILURE");
  const [agency, setAgency] = useState<string>(detail.case.assigned_agency ?? "");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [readingFile, setReadingFile] = useState(false);
  const [readNote, setReadNote] = useState<{ tone: "ok" | "warn"; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waiving, setWaiving] = useState(false);
  const [reason, setReason] = useState("");
  const waived = detail.case.status === "WAIVED";
  const aaActions = mode === "AA_SUCCESS" && hasAA;      // AA received: Waive / Retrigger; not received: Run verification

  useEffect(() => {
    setForm(formOf(run, detail.prefill, count));
    setMode(detail.case.mode);
    setEditing(detail.case.mode === "AA_FAILURE");
    setAgency(detail.case.assigned_agency ?? "");
    setFile(null);
  }, [run?.run_id, detail.case.case_id]); // eslint-disable-line react-hooks/exhaustive-deps

  // a statement read in the background fills the form when its read arrives (only where nothing was typed)
  const prefillKey = JSON.stringify(detail.prefill ?? null);
  useEffect(() => {
    if (!run) setForm((f) => withRead(f, detail.prefill));
  }, [prefillKey]); // eslint-disable-line react-hooks/exhaustive-deps

  /** The file chosen: shown beside the form at once, and read now -- what it says fills the form for checking. */
  async function choose(chosen: File | null) {
    setFile(chosen);
    setReadNote(null);
    setError(null);
    if (!chosen) return;
    onView({ url: URL.createObjectURL(chosen), name: chosen.name });
    setReadingFile(true);
    try {
      const out = await api.readStatement(detail.case.case_id, chosen);
      setFile(null);                                  // the case holds it now: the run uses the statement on file
      if (mode === "AA_SUCCESS") {
        // AA received: the form stays as the AA data filled it; the statement is compared with the AA data at the run
        setReadNote({ tone: "ok", text: out.status === "READING"
          ? "This statement is long; it is being read. At Run verification it is compared with the AA data."
          : "Statement on file. At Run verification it is compared with the AA data: account, holder, balances and transactions." });
      } else if (out.status === "READING") {
        setReadNote({ tone: "ok", text: "This statement is long; it is being read and the details will fill in when it is done." });
      } else if (out.filled && out.latest_uncertain) {
        setForm((f) => withRead(f, out.filled));
        setReadNote({ tone: "warn", text: "Part of the end of this statement could not be read, so the transactions filled here may not be the latest ones. Type the latest three from the statement." });
      } else if (out.filled) {
        setForm((f) => withRead(f, out.filled));
        setReadNote({ tone: "ok", text: `Filled from the statement (${out.transactions_read} transactions read). Check each value against the statement, then press Check.` });
      } else {
        setReadNote({ tone: "warn", text: "The statement could not be read, so nothing was filled. Type the details from it." });
      }
      onProcessed();                                  // the case screen shows the statement on file
    } catch (e) {
      // a server without the read-at-upload step (404 / 405): the file is simply checked at the run, as before
      if (!(e instanceof ApiError && [404, 405].includes(e.status))) setError(e instanceof ApiError ? e.message : String(e));
      else setFile(chosen);
    } finally {
      setReadingFile(false);
    }
  }

  const fromStatement = (key: string) => ["EXTRACTED", "CONFIRMED"].includes(form.provenance[key] ?? "");
  const unchecked = Object.values(form.provenance).filter((p) => p === "EXTRACTED").length;
  const toggle = (key: string) => setForm((f) => ({
    ...f, provenance: { ...f.provenance, [key]: f.provenance[key] === "CONFIRMED" ? "EXTRACTED" : "CONFIRMED" },
  }));
  const checkButton = (key: string, label: string) => fromStatement(key) && !locked ? (
    <button type="button" className={`check-toggle ${form.provenance[key] === "CONFIRMED" ? "is-on" : ""}`}
            aria-pressed={form.provenance[key] === "CONFIRMED"} aria-label={`${label}: checked against the statement`}
            onClick={(e) => { e.preventDefault(); toggle(key); }}>
      {form.provenance[key] === "CONFIRMED" ? "✔ Checked" : "Check"}
    </button>
  ) : null;

  const typed = (key: string): Provenance => (form.provenance[key] === "AGENCY" ? "AGENCY" : "MANUAL");
  const set = (key: "bank_name" | "account_number" | "account_type", value: string) =>
    setForm((f) => ({ ...f, [key]: value || null, provenance: { ...f.provenance, [key]: typed(key) } }));
  const setRow = (i: number, patch: Partial<EnteredTransaction>) =>
    setForm((f) => ({
      ...f,
      transactions: f.transactions.map((t, j) => (j === i ? { ...t, ...patch } : t)),
      provenance: { ...f.provenance, [`transaction_${i + 1}`]: typed(`transaction_${i + 1}`) },
    }));

  const statementFile = run?.sources.statement_file;
  const statementDoc = useMemo(() => detail.documents.filter((d) => d.role === "STATEMENT").at(-1), [detail.documents]);
  // AA not received: nothing to verify until a statement is on file or chosen (the run would only be refused)
  const noStatement = mode === "AA_FAILURE" && !statementDoc && !file;
  // AA received: the details are checked against the AA data; a statement on file is ALSO compared with the AA data
  const aaWithStatement = mode === "AA_SUCCESS" && Boolean(statementDoc || file);
  const locked = closed || !editing;

  async function retrigger() {
    setBusy(true);
    setError(null);
    try {
      const entered: EnteredDetails | null = editing ? {
        ...form, transactions: form.transactions.map((t) => ({ date: t.date || null, amount: t.amount || null, direction: t.direction || null })),
      } : null;
      await api.retrigger(detail.case.case_id, entered);
      onProcessed();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function waive() {
    setBusy(true);
    setError(null);
    try {
      await api.waive(detail.case.case_id, reason);
      setWaiving(false);
      setReason("");
      onProcessed();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const entered: EnteredDetails = {
        ...form,
        transactions: form.transactions.map((t) => ({ date: t.date || null, amount: t.amount || null, direction: t.direction || null })),
      };
      await api.process({ caseId: detail.case.case_id, mode, entered, file, agencyId: agency || null,
                          idempotencyKey: crypto.randomUUID(), statementOnFile: Boolean(statementDoc) });
      onProcessed();                                  // a long statement answers READING: the page refreshes until it is in
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      // the screen was older than the case (its statement is gone): show the case as it is now
      if (e instanceof ApiError && e.code === "NO_STATEMENT_ON_FILE") onProcessed();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div id="sec-details">
      <Card icon="bank" title={`${detail.case.applicant_name} Account`}
            actions={
              <>
                <div className="toggle" role="radiogroup" aria-label="Account Aggregator data">
                  <button className={mode === "AA_SUCCESS" ? "is-on" : ""} disabled={closed || !hasAA}
                          onClick={() => { setMode("AA_SUCCESS"); setEditing(false); }}>AA received</button>
                  <button className={mode === "AA_FAILURE" ? "is-on" : ""} disabled={closed}
                          onClick={() => { setMode("AA_FAILURE"); setEditing(true); }}>AA not received</button>
                </div>
                {!closed && !editing && <button className="btn btn-ghost" onClick={() => setEditing(true)}><Icon name="edit" /> Edit</button>}
              </>
            }
            foot={!closed && (
              <>
                <span className="hint">{reading || readingFile ? "The statement is being read; the details will fill in here."
                  : noStatement ? "Upload the bank statement first: there is nothing to check these details against."
                  : unchecked ? `${unchecked} value(s) from the statement not checked yet: unchecked values cannot be confirmed`
                  : mode === "AA_SUCCESS"
                    ? (aaWithStatement ? "These details are checked against the AA data, and the statement is compared with the AA data"
                                       : "These details are checked against the AA data. Upload the statement too to compare it with the AA data")
                  : file ? `${file.name} will be checked` : "The statement on file is checked against these details"}</span>
                {aaActions ? (
                  <span className="aa-actions">
                    <button className="btn btn-ghost btn-waive" disabled={busy || reading || readingFile} onClick={() => setWaiving(true)}>
                      <Icon name="x" /> Waive
                    </button>
                    <button className="btn btn-primary" disabled={busy || reading || readingFile} onClick={retrigger}>
                      {busy ? <><span className="spinner" aria-hidden /> Checking…</> : <><Icon name="shield" /> Retrigger</>}
                    </button>
                  </span>
                ) : (
                  <button className="btn btn-primary" disabled={busy || reading || readingFile || noStatement} onClick={submit}>
                    {busy ? <><span className="spinner" aria-hidden /> Verifying…</> : <><Icon name="shield" /> Run verification</>}
                  </button>
                )}
              </>
            )}>
        {waived && (
          <div style={{ marginBottom: 16 }}>
            <Notice tone="warn">Bank verification <b>waived</b>: the Account Aggregator data came from the bank itself. The reason is in the case history.</Notice>
          </div>
        )}
        <div className="section-title">Details</div>
        {mode === "AA_SUCCESS" && (
          <p className="muted form-note">
            These details came from the Account Aggregator (AA data). {aaWithStatement
              ? "The bank statement on file is compared with the AA data as well."
              : "If the customer also gave a bank statement, upload it below: it is compared with the AA data."}
          </p>
        )}
        {!locked && mode === "AA_FAILURE" && (
          <p className="muted form-note">
            {form.provenance.account_number === "APPLICATION"
              ? "Bank and account number are the ones the customer gave on the application; the statement is checked against them. "
              : ""}
            {Object.values(form.provenance).some((p) => p === "EXTRACTED" || p === "CONFIRMED")
              ? "Values marked From statement were read from the uploaded statement: compare each with it and press Check."
              : "Upload the statement: the details it shows fill in here for you to check."}
          </p>
        )}
        <div className="form">
          {locked ? (
            <>
              <Value label="Bank Name" value={form.bank_name} tag={<SourceTag source={form.provenance.bank_name} />} />
              <Value label="Account Number" value={form.account_number} tag={<SourceTag source={form.provenance.account_number} />} />
              <Value label="Account Type" value={form.account_type} tag={<SourceTag source={form.provenance.account_type} />} />
            </>
          ) : (
            <>
              <Field label="Bank Name" tag={<><SourceTag source={form.provenance.bank_name} />
                                                {checkButton("bank_name", "Bank Name")}</>}>
                <Suggest label="Bank Name" value={form.bank_name ?? ""} placeholder="Start typing, e.g. Canara"
                         options={hints.banks.map((b) => ({ value: b.name, keys: b.codes }))} max={hints.max_shown}
                         onChange={(v) => set("bank_name", v)} />
              </Field>
              <Field label="Account Number" tag={<><SourceTag source={form.provenance.account_number} />
                                                     {checkButton("account_number", "Account Number")}</>}>
                <input value={form.account_number ?? ""} inputMode="numeric" autoComplete="off"
                       placeholder="Digits only" maxLength={20}
                       onChange={(e) => set("account_number", e.target.value.toUpperCase().replace(/[^\dX]/g, ""))} />
              </Field>
              <Field label="Account Type" tag={<><SourceTag source={form.provenance.account_type} />
                                                   {checkButton("account_type", "Account Type")}</>}>
                <Suggest label="Account Type" value={form.account_type ?? ""} placeholder="Start typing, e.g. Sav"
                         options={hints.account_types.map((t) => ({ value: t }))} max={hints.max_shown}
                         onChange={(v) => set("account_type", v)} />
              </Field>
            </>
          )}
        </div>

        <div className="section-title">Latest transactions</div>
        {form.transactions.map((t, i) => (
          <div className="txn" key={i}>
            <span className="txn-no">{i + 1}</span>
            {locked ? (
              <>
                <Value label={`Transaction ${i + 1} Date`} value={dmy(t.date)}
                       tag={<SourceTag source={form.provenance[`transaction_${i + 1}`]} />} />
                <Value label={`Transaction ${i + 1} Amount (₹)`} value={t.amount} />
                <Value label="Credit / Debit" value={t.direction ? (t.direction === "CREDIT" ? "Credit" : "Debit") : null} />
              </>
            ) : (
              <>
                <Field label={`Transaction ${i + 1} Date`} tag={<><SourceTag source={form.provenance[`transaction_${i + 1}`]} />
                                                                {checkButton(`transaction_${i + 1}`, `Transaction ${i + 1}`)}</>}>
                  <input type="date" value={t.date ?? ""} max={today} min={hints.earliest_date ?? undefined}
                         onChange={(e) => setRow(i, { date: e.target.value || null })} />
                </Field>
                <Field label={`Transaction ${i + 1} Amount (₹)`}>
                  <input value={t.amount ?? ""} inputMode="decimal" placeholder="0.00"
                         onChange={(e) => setRow(i, { amount: e.target.value.replace(/[^\d.]/g, "") || null })} />
                </Field>
                <Field label="Credit / Debit">
                  <select value={t.direction ?? ""} onChange={(e) => setRow(i, { direction: (e.target.value || null) as Direction | null })}>
                    <option value="">Choose…</option><option value="CREDIT">Credit</option><option value="DEBIT">Debit</option>
                  </select>
                </Field>
              </>
            )}
          </div>
        ))}

        <div className="section-title">Statement and agency</div>
        <div className="form">
          <Field label="Bank Statement" tag={statementFile && <StatusBadge status={statementFile.extraction_status} />}>
            <label className="file">
              <Icon name="upload" />
              <span>{readingFile ? "Reading the statement…" : file ? file.name : "Upload PDF or CSV"}</span>
              <input type="file" accept=".pdf,.csv,application/pdf,text/csv" disabled={closed}
                     onChange={(e) => { void choose(e.target.files?.[0] ?? null); e.target.value = ""; }} />
            </label>
          </Field>
          <Value label="Statement on file" value={statementDoc
            ? <button className="btn-text" onClick={() => onView({ url: api.documentUrl(detail.case.case_id, statementDoc.document_id),
                                                                  name: statementDoc.file_name ?? "statement.pdf" })}>
                <Icon name="file" size={14} /> View statement</button>
            : "No statement yet"} />
          <Field label="Select Agency">
            <select value={agency} disabled={closed} onChange={(e) => setAgency(e.target.value)}>
              <option value="">Choose…</option>
              {config.agencies.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </Field>
        </div>
        {statementFile && statementFile.notes.length > 0 && (
          <div style={{ marginTop: 16 }}><Notice tone="warn">{statementFile.notes.map((n, i) => <div key={i}>{n}</div>)}</Notice></div>
        )}
        {readNote && <div style={{ marginTop: 16 }}><Notice tone={readNote.tone}>{readNote.text}</Notice></div>}
        {error && <div style={{ marginTop: 16 }}><Notice tone="bad">{error}</Notice></div>}
      </Card>
      {waiving && (
        <div className="overlay" role="presentation" onClick={() => !busy && setWaiving(false)}>
          <div className="popup waive-popup" role="dialog" aria-modal="true" aria-labelledby="waive-title" onClick={(e) => e.stopPropagation()}>
            <header className="popup-head">
              <span className="popup-icon"><Icon name="shield" size={18} /></span>
              <div>
                <h2 id="waive-title">Waive the bank verification?</h2>
                <p>The Account Aggregator data came from the bank itself. The case closes as Waived; who, when and why are recorded.</p>
              </div>
            </header>
            <div className="waive-body">
              <label className="waive-label" htmlFor="waive-reason">Reason for waiving</label>
              <textarea id="waive-reason" rows={3} value={reason} maxLength={1000} autoFocus
                        placeholder="e.g. AA data received directly from the bank; account and holder match the application"
                        onChange={(e) => setReason(e.target.value)} />
            </div>
            <footer className="waive-foot">
              <button className="btn btn-ghost" disabled={busy} onClick={() => setWaiving(false)}>Cancel</button>
              <button className="btn btn-primary" disabled={busy || !reason.trim()} onClick={waive}>
                {busy ? <><span className="spinner" aria-hidden /> Waiving…</> : "Waive verification"}
              </button>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
