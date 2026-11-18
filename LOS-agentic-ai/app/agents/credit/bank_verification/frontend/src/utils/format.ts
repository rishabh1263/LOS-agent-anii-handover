// Display helpers: Indian rupee grouping, dates, status words. Never parses money into a float for arithmetic.

export function rupees(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  const negative = text.startsWith("-");
  const [whole, paise = "00"] = text.replace("-", "").split(".");
  const head = whole.slice(0, -3);
  const tail = whole.slice(-3);
  const grouped = head ? `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${tail}` : tail;
  return `${negative ? "-" : ""}₹${grouped}.${paise.padEnd(2, "0").slice(0, 2)}`;
}

export function day(value: string | null | undefined): string {
  if (!value) return "—";
  const [y, m, d] = value.slice(0, 10).split("-");
  return d && m && y ? `${d}-${m}-${y}` : value;
}

export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export function words(code: string | null | undefined): string {
  if (!code) return "—";
  return code.replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());
}

export function tone(status: string | null | undefined): "good" | "bad" | "warn" | "neutral" {
  switch (status) {
    case "PASS":
    case "POSITIVE":
    case "CONFIRMED":
    case "COMPLETE":
      return "good";
    case "FAIL":
    case "NEGATIVE":
    case "NOT_CONFIRMED":
    case "HIGH":
    case "INVALID":
      return "bad";
    case "REVIEW_REQUIRED":
    case "PENDING":
    case "READY_FOR_FINALIZATION":
    case "READING":
    case "MEDIUM":
    case "PARTIAL":
    case "UNREADABLE":
      return "warn";
    default:
      return "neutral";
  }
}
