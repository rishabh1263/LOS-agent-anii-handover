// Every word on the screen comes from the server's config (legal_verification_wording.yaml `screen`, via GET /config).
// t("rules.title"); t("docs.drop", { mb: 25 }) fills {placeholders}. A key not in the config shows as ⟨key⟩ -- visible,
// never a silent blank (a backend test checks every key used here exists).
type Tree = { [key: string]: string | Tree };

let screen: Tree = {};

export function setScreen(tree: unknown) {
  screen = (tree ?? {}) as Tree;
}

/** The screen's timings from config `ui` (set with the words, when GET /config arrives). */
export type UiTimings = { loader_line_ms?: number; switch_ms?: number;
                          typewriter?: { type_ms?: number; erase_ms?: number; hold_ms?: number; rest_ms?: number } };
let timings: UiTimings = {};
export function setUi(ui: unknown) {
  timings = (ui ?? {}) as UiTimings;
}
export function ui(): UiTimings {
  return timings;
}

export function t(path: string, values: Record<string, string | number> = {}): string {
  let node: string | Tree | undefined = screen;
  for (const part of path.split(".")) node = typeof node === "object" ? node[part] : undefined;
  if (typeof node !== "string") return `⟨${path}⟩`;
  return node.replace(/\{(\w+)\}/g, (_, k: string) => String(values[k] ?? `{${k}}`));
}

export const rupees = (n: number) => `₹${Math.round(n).toLocaleString("en-IN")}`;

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-IN", { day: "2-digit", month: "short", year: "numeric",
                                                                       hour: "2-digit", minute: "2-digit" });
}

export const initials = (name: string) => (name || "?").split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
