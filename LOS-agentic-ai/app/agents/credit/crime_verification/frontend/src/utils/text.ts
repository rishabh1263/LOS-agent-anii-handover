// Every word on the screen comes from the server's config (crime_verification_wording.yaml `screen`, via GET /config).
// `t("verify.title")`; `t("verify.branch", { branch: "MUM" })` fills {placeholders}. A key that is not in the config
// shows as ⟨key⟩ -- visible, never a silent blank (a backend test checks every key used here exists).
type Tree = { [key: string]: string | Tree };

let screen: Tree = {};

export function setScreen(tree: Tree | undefined) {
  screen = tree ?? {};
}

export function t(path: string, values: Record<string, string | number> = {}): string {
  let node: string | Tree | undefined = screen;
  for (const part of path.split(".")) node = typeof node === "object" ? node[part] : undefined;
  if (typeof node !== "string") return `⟨${path}⟩`;
  return node.replace(/\{(\w+)\}/g, (_, k: string) => String(values[k] ?? `{${k}}`));
}
