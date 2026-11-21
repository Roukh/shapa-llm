import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { OWNER, env, freshKey, get, mintAll, put, rec, row, send, sync, wiki } from "./helpers.js";
import { mempmIn, sessionText, tool } from "./mcp-helpers.js";
import { migrate, ISSUE_ACCESS_KEY } from "../src/schema.js";

// A mistake is an issue row, and an issue takes no stage tag: a baseline device reads the global wiki's issues that
// still carry a lesson, and never a dismissed, soft or learned one, nor any issue of a repo wiki.

let n = 9600;
const label = () => `I${++n}`;

async function world() {
  const op = (await mintAll("issue-access")).token;
  const base = (await send("POST", "/admin/devices", { token: OWNER, body: { name: `issue-base-${freshKey()}`, scopes: ["read", "write"], reach: "baseline" } })).body.token as string;
  await wiki(op, "global", "global");
  const repo = freshKey();
  await wiki(op, repo);
  return { op, base, repo };
}

describe("baseline reach: mistakes", () => {
  it("reads a live global issue, and never a dismissed, soft, learned or repo-wiki one", async () => {
    const { op, base, repo } = await world();
    const k = freshKey();
    const [live, dismissed, soft, learned] = [label(), label(), label(), label()];
    const r = await sync(op, "global", { ops: [
      put(row(live, `LIVE-${k}`)), put(row(dismissed, `DISMISSED-${k}`, { tags: ["dismissed"] })),
      put(row(soft, `SOFT-${k}`, { tags: ["soft"] })), put(row(learned, `LEARNED-${k}`, { tags: ["learned"] })),
    ] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(Array(4).fill("accepted"));
    const mine = await send("POST", rec(repo), { token: op, body: { type: "issue", summary: `REPO-ISSUE-${k}` } });
    expect(mine.status).toBe(201);

    const start = JSON.stringify(mempmIn(await sessionText(base, { wiki: repo })).X);
    expect(start).toContain(`LIVE-${k}`);
    for (const gone of ["DISMISSED", "SOFT", "LEARNED", "REPO-ISSUE"]) expect(start, gone).not.toContain(`${gone}-${k}`);

    expect((await tool(base, "get", { id: live, wiki: "global" })).text).toContain(`LIVE-${k}`);
    for (const id of [dismissed, soft, learned]) expect((await tool(base, "get", { id, wiki: "global" })).text, id).toBe(`No record ${id}.`);
    expect((await tool(base, "get", { id: mine.body.id, wiki: repo })).text).toBe(`No record ${mine.body.id}.`);
    const found = (await tool(base, "search", { query: `REPO-ISSUE-${k} DISMISSED-${k} SOFT-${k} LEARNED-${k}`, k: 20 })).text;
    for (const gone of ["REPO-ISSUE", "DISMISSED", "SOFT", "LEARNED"]) expect(found, gone).not.toContain(`${gone}-${k}`);
    expect((await get(rec("global", `/${dismissed}`), base)).status).toBe(404);
    // the operator's own device still reads all of them
    expect((await get(rec("global", `/${dismissed}`), op)).status).toBe(200);
  });

  it("recomputes the reach of a store built before issue access, once", async () => {
    const { op, base } = await world();
    const k = freshKey();
    const [live, dropped] = [label(), label()];
    await sync(op, "global", { ops: [put(row(live, `OLD-LIVE-${k}`)), put(row(dropped, `OLD-DISMISSED-${k}`, { tags: ["dismissed"] }))] });
    const stub = env.STORE.getByName("main");
    await runInDurableObject(stub, (_i, state) => {
      const sql = state.storage.sql;
      // a store as it was before: issues hidden from baseline (no stage tag), the flag missing
      sql.exec(`UPDATE records SET base_vis = 0 WHERE kind = 'I'`);
      sql.exec(`UPDATE record_tags SET vis = 0 WHERE kind = 'I'`);
      sql.exec(`DELETE FROM meta WHERE key = ?`, ISSUE_ACCESS_KEY);
    });
    expect((await get(rec("global", `/${live}`), base)).status).toBe(404);
    await runInDurableObject(stub, (_i, state) => {
      migrate(state.storage.sql);
      expect(state.storage.sql.exec(`SELECT 1 FROM meta WHERE key = ?`, ISSUE_ACCESS_KEY).toArray()).toHaveLength(1);
    });
    expect((await get(rec("global", `/${live}`), base)).status).toBe(200);
    expect((await get(rec("global", `/${dropped}`), base)).status).toBe(404);
  });
});
