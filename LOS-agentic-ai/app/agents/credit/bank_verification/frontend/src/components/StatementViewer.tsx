import { useEffect, useState } from "react";
import { Icon } from "./Icon";
import { Card } from "./ui";

/** A statement on screen: the file just chosen (a local blob URL, before any upload) or the one on file. */
export interface Viewing {
  url: string;
  name: string;
}

const isCsv = (name: string) => /\.csv$/i.test(name);

/** A CSV row split on commas, quotes respected. */
function cells(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let quoted = false;
  for (const ch of line) {
    if (ch === '"') quoted = !quoted;
    else if (ch === "," && !quoted) { out.push(cur); cur = ""; }
    else cur += ch;
  }
  return [...out, cur];
}

/**
 * The statement beside the form, so the officer reads it while typing the details (AA not received). A PDF shows in
 * the browser's own viewer; a CSV as a table.
 */
export function StatementViewer({ view, onClose }: { view: Viewing; onClose: () => void }) {
  const [rows, setRows] = useState<string[][] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setRows(null);
    setFailed(false);
    if (!isCsv(view.name)) return;
    let live = true;
    fetch(view.url)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(r.statusText))))
      .then((text) => live && setRows(text.split(/\r?\n/).filter((l) => l.trim()).map(cells)))
      .catch(() => live && setFailed(true));
    return () => { live = false; };
  }, [view.url, view.name]);

  return (
    <Card className="viewer" icon="file" title="Statement" subtitle={view.name}
          actions={
            <>
              <a className="btn btn-ghost" href={view.url} target="_blank" rel="noopener">Open in new tab</a>
              <button className="btn btn-ghost" aria-label="Close the statement" onClick={onClose}><Icon name="x" /></button>
            </>
          }
          bodyClass="viewer-body">
      {!isCsv(view.name) ? (
        <iframe title="Bank statement" src={view.url} />
      ) : failed ? (
        <p className="viewer-note">This file could not be shown here. Use Open in new tab.</p>
      ) : !rows ? (
        <p className="viewer-note"><span className="spinner" aria-hidden /> Opening…</p>
      ) : (
        <div className="viewer-table">
          <table>
            <thead><tr>{rows[0].map((c, i) => <th key={i}>{c}</th>)}</tr></thead>
            <tbody>{rows.slice(1).map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
          </table>
        </div>
      )}
    </Card>
  );
}
