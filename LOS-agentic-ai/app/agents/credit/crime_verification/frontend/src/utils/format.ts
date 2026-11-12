export const rupees = (n: number) =>
  "₹" + new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 }).format(n);

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function dob(iso: string): string {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  return d ? `${d[3]}/${d[2]}/${d[1]}` : iso || "—";
}

export function newKey(): string {
  try { return crypto.randomUUID(); } catch { return `k-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
}

/** The status / risk -> one of five tones for badges. */
export function tone(value: string | null | undefined): "ok" | "warn" | "bad" | "info" | "na" {
  switch (value) {
    case "COMPLETED": case "NO_RISK": case "LOW_RISK": case "ACCEPTED": return "ok";
    case "REVIEW_REQUIRED": case "AVERAGE_RISK": case "REFER_TO_CREDIT": return "warn";
    case "FAILED": case "HIGH_RISK": case "VERY_HIGH_RISK": case "DECLINE_RECOMMENDED": return "bad";
    case "PROCESSING": case "INITIATED": return "info";
    default: return "na";
  }
}
