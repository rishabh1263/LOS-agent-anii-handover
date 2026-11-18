import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../services/api";

// AA received AND / OR a statement (Sahil Sir, 2026-10-28): which statement the run uses
describe("the statement a run uses", () => {
  afterEach(() => vi.unstubAllGlobals());

  async function sent(mode: "AA_SUCCESS" | "AA_FAILURE", statementOnFile: boolean): Promise<string> {
    const fetch = vi.fn(async (_url: string, _init?: RequestInit) =>
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    await api.process({ caseId: "BV-1", mode, entered: null, file: null, statementOnFile });
    const init = fetch.mock.calls[0][1]!;
    return JSON.parse(String(init.body)).statement_source;
  }

  it("AA received with no statement on file: an AA-only run", async () => {
    expect(await sent("AA_SUCCESS", false)).toBe("NONE");
  });
  it("AA received with a statement on file: the statement is used (compared with the AA data)", async () => {
    expect(await sent("AA_SUCCESS", true)).toBe("ON_FILE");
  });
  it("AA not received: always the statement on file", async () => {
    expect(await sent("AA_FAILURE", false)).toBe("ON_FILE");
  });
});
