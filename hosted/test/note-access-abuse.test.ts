// The break-it pass for note access, run as a baseline token (see note-access.test.ts for the contract tests).

import { describe, expect, it } from "vitest";
import { OWNER, freshKey, get, rec, send, setup } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const patch = (token: string, w: string, ref: string, body: Record<string, unknown>) => send("PATCH", rec(w, `/${encodeURIComponent(ref)}`), { token, body });
const read = (token: string, w: string, ref: string) => get(rec(w, `/${encodeURIComponent(ref)}`), token);
const note = (path: string, extra: Record<string, unknown> = {}) => ({ type: "note", path, body: `body of ${path}`, ...extra });
const instructions = async (token: string) => (await get("/v1/instructions?client=mcp", token)).body.text as string;

async function world() {
  const { token: op, w } = await setup();
  const r = await send("POST", "/admin/devices", { token: OWNER, body: { name: `outside-${freshKey()}`, scopes: ["read", "write", "delete"], reach: "baseline" } });
  expect(r.status).toBe(201);
  return { op, w, base: r.body.token as string };
}

describe("note access: break-it probes with a baseline token", () => {
  it("reads a hidden note by neither its alias, nor search words, nor the cross-wiki get; and its alias is not a 409", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    const alias = `secret-alias-${k}`;
    expect((await create(op, w, note(`hid-${k}.md`, { body: `---\nid: ${alias}\n---\nHIDDEN-ALIAS-BODY ${k}` }))).status).toBe(201);
    expect((await read(base, w, alias)).status).toBe(404);
    expect((await get(`/v1/get?id=${alias}`, base)).body.matches).toEqual([]);
    expect((await get(`/v1/get?id=${encodeURIComponent(`hid-${k}.md`)}`, base)).body.matches).toEqual([]);
    for (const q of ["HIDDEN-ALIAS-BODY", alias, `hid-${k}`]) expect((await get(`/v1/search?q=${q}&wiki=${w}`, base)).body.results, q).toEqual([]);
    // taking the alias is dropped silently, never a 409 that would say the alias exists
    const taker = await create(base, w, note(`mine-${k}.md`, { body: `---\nid: ${alias}\n---\nmine` }));
    expect(taker.status).toBe(201);
    expect((await read(op, w, alias)).body.id).toBe(`hid-${k}.md`);
  });

  it("answers an MCP get, search, tags, related and project for a hidden note as it would for none", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    const proj = `p-${k}`;
    await create(op, w, note(`hid-${k}.md`, { body: `MCP-HIDDEN-${k}`, tags: [`project:${proj}`, `mcp-tag-${k}`] }));
    const idea = await create(op, w, { type: "idea", summary: `idea ${k}`, tags: [`mcp-tag-${k}`], project: proj });
    const calls: [string, Record<string, unknown>][] = [
      ["get", { id: `hid-${k}.md`, wiki: w }], ["search", { query: `MCP-HIDDEN-${k}` }], ["search", { query: "", tags: [`mcp-tag-${k}`], k: 50 }],
      ["tags", { wiki: w, system: true }], ["related", { wiki: w, id: idea.body.id }], ["related", { wiki: w, id: `hid-${k}.md` }], ["project", { key: proj, wiki: w }],
    ];
    for (const [name, args] of calls) {
      const r = await tool(base, name, args);
      expect(r.text, `${name} ${JSON.stringify(args)}`).not.toContain(`MCP-HIDDEN-${k}`);
      if (!JSON.stringify(args).includes(`hid-${k}`)) expect(r.text, name).not.toContain(`hid-${k}`); // a refusal may echo the id the caller sent
    }
    expect((await instructions(base)).includes(`hid-${k}`)).toBe(false);
  });

  it("cannot forge the marker or an engine tag through another record or a PATCH", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    const rule = await create(op, w, { type: "rule", summary: `rule ${k}` });
    for (const tags of [["stage:0"], ["stage:1"], ["stage-proposed:0"]]) {
      expect((await patch(base, w, rule.body.id, { tags })).status).toBe(404); // a rule it cannot read
      for (const type of ["idea", "decision", "document", "human_task"]) {
        const body = type === "idea" || type === "decision" ? { type, summary: `x ${k}`, tags } : { type, title: `x ${k}`, tags };
        expect([400, 403], `${type} ${tags}`).toContain((await create(base, w, body)).status);
      }
    }
    await create(base, w, note(`mine-${k}.md`));
    expect((await patch(base, w, `mine-${k}.md`, { tags: ["stage:0", "skill:evil"] })).status).toBe(403);
    expect((await patch(base, w, `mine-${k}.md`, { tags: ["stage:0", "principle"] })).status).toBe(403);
    expect((await patch(base, w, `mine-${k}.md`, { tags: ["stage:0", "rank:1"] })).status).toBe(403);
  });
});
