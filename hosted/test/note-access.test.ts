// A note is visible to a baseline device only with the access marker `stage:0` (hosted.md, "Reach"), and a project keeps one
// note. The marker is not the baseline: it costs no budget and is never printed in the BASELINE section.

import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { BASELINE_BUDGET } from "../src/gates.js";
import { NOTE_ACCESS_KEY, migrate } from "../src/schema.js";
import { OWNER, env, freshKey, get, mint, mintAll, put, rec, row, send, setup, sync, wiki } from "./helpers.js";
import { sessionText, tool } from "./mcp-helpers.js";

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const patch = (token: string, w: string, ref: string, body: Record<string, unknown>) => send("PATCH", rec(w, `/${encodeURIComponent(ref)}`), { token, body });
const read = (token: string, w: string, ref: string) => get(rec(w, `/${encodeURIComponent(ref)}`), token);
const note = (path: string, extra: Record<string, unknown> = {}) => ({ type: "note", path, body: `body of ${path}`, ...extra });
const instructions = (token: string) => sessionText(token);

/** A baseline-reach device token (the shape the outside agents get). */
async function baseline(scopes = ["read", "write", "delete"], wikis: string[] | null = null): Promise<string> {
  const r = await send("POST", "/admin/devices", { token: OWNER, body: { name: `outside-${freshKey()}`, scopes, wikis, reach: "baseline" } });
  expect(r.status).toBe(201);
  return r.body.token;
}

/** A repo wiki with an operator (full, delete scope) and a baseline device. */
async function world() {
  const { token: op, w } = await setup();
  return { op, w, base: await baseline() };
}

describe("note access: what a baseline device reads", () => {
  it("sees a note only when it carries stage:0: by id, list, counts, search, tags, projects, related", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    const proj = `proj-${k}`;
    expect((await create(op, w, note(`research/open-${k}.md`, { body: `OPENNOTE ${k}`, tags: ["stage:0", `shared-${k}`] }))).status).toBe(201);
    expect((await create(op, w, note(`research/plain-${k}.md`, { body: `PLAINNOTE ${k}`, tags: [`shared-${k}`, `project:${proj}`] }))).status).toBe(201);
    const idea = await create(op, w, { type: "idea", summary: `idea ${k}`, tags: [`shared-${k}`], project: proj });

    expect((await read(base, w, `research/open-${k}.md`)).status).toBe(200);
    const hidden = await read(base, w, `research/plain-${k}.md`);
    const missing = await read(base, w, `research/missing-${k}.md`);
    expect(hidden.status).toBe(404);
    expect(hidden.body.error.message.replace(`plain-${k}`, "X")).toBe(missing.body.error.message.replace(`missing-${k}`, "X"));
    expect((await read(op, w, `research/plain-${k}.md`)).status).toBe(200);

    const listed = (await get(rec(w, "?kind=N"), base)).body.records as { id: string }[];
    expect(listed.map((r) => r.id)).toEqual([`research/open-${k}.md`]);
    expect((await get(rec(w, `?kind=N&tag=shared-${k}`), base)).body.records.map((r: { id: string }) => r.id)).toEqual([`research/open-${k}.md`]);
    expect((await get(rec(w, `?tag=shared-${k}`), base)).text).not.toContain("PLAINNOTE");
    expect((await get("/v1/wikis", base)).body.wikis.find((x: { key: string }) => x.key === w).counts.note).toBe(1);
    expect((await get(`/v1/get?id=${encodeURIComponent(`research/plain-${k}.md`)}&wiki=${w}`, base)).body.matches).toEqual([]);

    for (const url of [`/v1/search?q=PLAINNOTE&wiki=${w}`, `/v1/search?tags=shared-${k}&wiki=${w}&k=50`, `/v1/search?tags=project:${proj}&wiki=${w}`, `/v1/search?q=plain&types=note&wiki=${w}`]) {
      const r = await get(url, base);
      expect(r.text, url).not.toContain("PLAINNOTE");
      expect(JSON.stringify(r.body.results), url).not.toContain(`plain-${k}`);
    }
    const shared = (await get(`/v1/search?tags=shared-${k}&wiki=${w}&k=50`, base)).body.results as { id: string }[];
    expect(shared.map((r) => r.id).sort()).toEqual([`research/open-${k}.md`, idea.body.id].sort());

    const tags = (await get(`/v1/tags?wiki=${w}&system=1`, base)).body.tags as { tag: string; counts: Record<string, number> }[];
    expect(tags.find((x) => x.tag === `shared-${k}`)!.counts).toEqual({ note: 1, memory: 1 });
    expect(tags.find((x) => x.tag === `project:${proj}`)!.counts).toEqual({ memory: 1 }); // only the idea, not the hidden note
    expect(JSON.stringify((await get(`/v1/projects/${proj}?wiki=${w}`, base)).body)).not.toContain("PLAINNOTE");
    const rel = (await get(rec(w, `/${idea.body.id}/related`), base)).text;
    expect(rel).not.toContain(`plain-${k}`);
    expect((await get(rec(w, `/${encodeURIComponent(`research/plain-${k}.md`)}/related`), base)).status).toBe(404);
    // the operator's full view of it is unchanged
    expect((await get(`/v1/search?tags=shared-${k}&wiki=${w}&k=50`, op)).body.results).toHaveLength(3);
  });

  it("stage:0 on a note never reaches the BASELINE text or its budget", async () => {
    const { token: op } = await setup();
    await wiki(op, "global", "global");
    const k = freshKey();
    const big = "x".repeat(BASELINE_BUDGET + 500);
    expect((await create(op, "global", note(`research/big-${k}.md`, { body: `NOTEMARK-${k} ${big}`, tags: ["stage:0"] }))).status).toBe(201);
    // the baseline still takes a stage-0 rule: the note's characters are no part of the budget
    const r = await sync(op, "global", { ops: [put(row("R9301", `S0-RULE-${k}`, { tags: ["stage:0"] }))] });
    expect(r.body.results[0].status).toBe("accepted");
    const text = await instructions(op);
    expect(text).toContain(`S0-RULE-${k}`);
    expect(text).not.toContain(`NOTEMARK-${k}`);
    expect(await instructions(await baseline())).not.toContain(`NOTEMARK-${k}`);
  });

  it("prints the operator's session notes to a baseline device only once they carry stage:0", async () => {
    const { token: op } = await setup();
    await wiki(op, "global", "global");
    const k = freshKey();
    const base = await baseline();
    await patch(op, "global", "session-instructions.md", { body: `PRINTED-${k}` }); // exists from another test? then it is just updated
    const made = await create(op, "global", note("session-instructions.md", { body: `PRINTED-${k}` }));
    expect([201, 409]).toContain(made.status);
    await patch(op, "global", "session-instructions.md", { body: `PRINTED-${k}`, tags: [] });
    expect(await instructions(op)).toContain(`PRINTED-${k}`);
    expect(await instructions(base)).not.toContain(`PRINTED-${k}`);
    expect((await patch(op, "global", "session-instructions.md", { tags: ["stage:0"] })).status).toBe(200);
    expect(await instructions(base)).toContain(`PRINTED-${k}`);
    // the guard still holds for a baseline device that can now read it
    expect((await patch(base, "global", "session-instructions.md", { body: "planted" })).status).toBe(403);
    expect((await send("DELETE", rec("global", "/session-instructions.md"), { token: base })).status).toBe(403);
    await patch(op, "global", "session-instructions.md", { tags: [] });
  });
});

describe("note access: who sets stage:0", () => {
  it("lets a full device add and remove it on any note, by REST and MCP, without converting it; sync never changes it", async () => {
    const { w } = await setup();
    const mid = await mint(`mid-${freshKey()}`, ["read", "write"]); // no delete scope: the unprivileged writer of the stage rules
    const k = freshKey();
    const made = await create(mid.token, w, note(`a-${k}.md`, { tags: ["stage:0", "x"] }));
    expect(made.status).toBe(201);
    expect(made.body.tags).toEqual(["stage:0", "x"]);
    expect(made.headers.get("x-shapa-notice")).toBeNull();
    expect((await patch(mid.token, w, `a-${k}.md`, { tags: ["x"] })).body.record?.tags ?? (await read(mid.token, w, `a-${k}.md`)).body.tags).toEqual(["x"]);
    expect((await patch(mid.token, w, `a-${k}.md`, { tags: ["x", "stage:0"] })).status).toBe(200);
    expect((await read(mid.token, w, `a-${k}.md`)).body.tags).toEqual(["stage:0", "x"]);
    const viaMcp = await tool(mid.token, "save", { wiki: w, type: "note", path: `b-${k}.md`, body: "b", tags: ["stage:0"] });
    expect(viaMcp.isError).toBe(false);
    expect(viaMcp.text).not.toContain("stage-proposed");
    expect((await read(mid.token, w, `b-${k}.md`)).body.tags).toEqual(["stage:0"]);
    expect((await tool(mid.token, "update", { wiki: w, id: `b-${k}.md`, tags: [] })).isError).toBe(false);
    expect((await read(mid.token, w, `b-${k}.md`)).body.tags).toEqual([]);
    // sync: a note file has no tags, so a note's tags are the server's. A put never shares a new note,
    // never strips the marker the operator set, and never puts back one the operator removed.
    const wire = (id: string, tags: string[], body: string) => ({ id, uid: "", kind: "N", created: "", updated: "2026-02-01T00:00:00Z", deleted: false, data: { body }, tags, links: [] });
    const s1 = await sync(mid.token, w, { ops: [put(wire(`c-${k}.md`, ["stage:0"], "c"))] });
    expect(s1.body.results[0].status).toBe("accepted");
    expect((await read(mid.token, w, `c-${k}.md`)).body.tags).toEqual([]);
    expect((await patch(mid.token, w, `c-${k}.md`, { tags: ["stage:0"] })).status).toBe(200);
    let rev = (await read(mid.token, w, `c-${k}.md`)).body.rev;
    const s2 = await sync(mid.token, w, { ops: [put(wire(`c-${k}.md`, [], "c2"), rev)] });
    expect(s2.body.results[0].status).toBe("accepted");
    expect((await read(mid.token, w, `c-${k}.md`)).body.tags).toEqual(["stage:0"]);
    expect((await patch(mid.token, w, `c-${k}.md`, { tags: [] })).status).toBe(200);
    rev = (await read(mid.token, w, `c-${k}.md`)).body.rev;
    const s3 = await sync(mid.token, w, { ops: [put(wire(`c-${k}.md`, ["stage:0"], "c3"), rev)] });
    expect(s3.body.results[0].status).toBe("accepted");
    const after = (await read(mid.token, w, `c-${k}.md`)).body;
    expect(after.tags).toEqual([]);
    expect(after.data?.body ?? after.body).toBe("c3");
  });

  it("allows only exactly stage:0 on a note", async () => {
    const { op, w } = await world();
    const k = freshKey();
    for (const tags of [["stage:1"], ["stage-proposed:0"], ["stage:0", "stage:3"], ["Stage:0"], ["stage:00"], ["ｓｔａｇｅ:0"]]) {
      expect((await create(op, w, note(`bad-${k}.md`, { tags }))).status, JSON.stringify(tags)).toBe(400);
    }
    expect((await create(op, w, note(`ok-${k}.md`, { tags: ["stage:0"] }))).status).toBe(201);
    expect((await patch(op, w, `ok-${k}.md`, { tags: ["stage:2"] })).status).toBe(400);
    // a rule still treats stage:0 as the operator's (proposed, not stored as the baseline)
    expect((await create(op, w, { type: "rule", summary: `r ${k}`, tags: ["stage:2"] })).status).toBe(201);
  });

  it("marks every note a baseline device creates stage:0, on REST and MCP, so it reads its own note back", async () => {
    const { w, base } = await world();
    const k = freshKey();
    const made = await create(base, w, note(`mine-${k}.md`, { tags: ["topic"] }));
    expect(made.status).toBe(201);
    expect(made.body.tags).toEqual(["stage:0", "topic"]);
    expect((await read(base, w, `mine-${k}.md`)).status).toBe(200);
    const viaMcp = await tool(base, "save", { wiki: w, type: "note", path: `mcp-${k}.md`, body: "m" });
    expect(viaMcp.isError).toBe(false);
    expect((await read(base, w, `mcp-${k}.md`)).body.tags).toEqual(["stage:0"]);
    // asking for it explicitly is fine too
    expect((await create(base, w, note(`ask-${k}.md`, { tags: ["stage:0"] }))).body.tags).toEqual(["stage:0"]);
    // a hundred tags still leave room for the marker
    const many = Array.from({ length: 100 }, (_, i) => `t${String(i).padStart(3, "0")}`);
    expect((await create(base, w, note(`many-${k}.md`, { tags: many }))).body.tags).toContain("stage:0");
  });

  it("never lets a baseline device remove stage:0 from a note, set another stage tag, or mark a hidden note", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    await create(base, w, note(`mine-${k}.md`, { tags: ["topic"] }));
    for (const tags of [["topic"], [], ["x"]]) {
      const r = await patch(base, w, `mine-${k}.md`, { tags });
      expect(r.status, JSON.stringify(tags)).toBe(403);
      expect((await tool(base, "update", { wiki: w, id: `mine-${k}.md`, tags })).isError).toBe(true);
    }
    expect((await read(base, w, `mine-${k}.md`)).body.tags).toEqual(["stage:0", "topic"]);
    expect((await patch(base, w, `mine-${k}.md`, { tags: ["stage:0", "other"] })).status).toBe(200);
    for (const tags of [["stage:0", "stage:1"], ["stage:0", "stage-proposed:0"], ["stage:1"], ["stage-proposed:0"]]) {
      expect([400, 403], JSON.stringify(tags)).toContain((await patch(base, w, `mine-${k}.md`, { tags })).status);
      expect([400, 403]).toContain((await create(base, w, note(`other-${k}.md`, { tags }))).status);
    }
    // a hidden note is out of its reach: it cannot mark it, edit it or tell it from a missing one
    await create(op, w, note(`hid-${k}.md`));
    expect((await patch(base, w, `hid-${k}.md`, { tags: ["stage:0"] })).status).toBe(404);
    expect((await read(op, w, `hid-${k}.md`)).body.tags).toEqual([]);
    // it cannot put stage:0 on anything else it writes
    for (const body of [{ type: "idea", summary: `i ${k}`, tags: ["stage:0"] }, { type: "document", title: `d ${k}`, tags: ["stage:0"] }, { type: "human_task", title: `t ${k}`, tags: ["stage:0"] }]) {
      expect([400, 403], body.type).toContain((await create(base, w, body)).status);
    }
    // a full device that has to give a hidden one up still can
    expect((await patch(op, w, `hid-${k}.md`, { tags: ["stage:0"] })).status).toBe(200);
    expect((await read(base, w, `hid-${k}.md`)).status).toBe(200);
  });

  it("does not let a baseline device create a note over a hidden one, or learn that one is there", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    await create(op, w, note(`held-${k}.md`, { body: "OPERATOR-SECRET" }));
    await create(op, w, note(`seen-${k}.md`, { tags: ["stage:0"] }));
    const hidden = await create(base, w, note(`held-${k}.md`, { body: "overwrite" }));
    const visible = await create(base, w, note(`seen-${k}.md`, { body: "overwrite" }));
    expect(hidden.status).toBe(409);
    expect(visible.status).toBe(409);
    expect(hidden.body.error.message.replace(`held-${k}`, "X")).toBe(visible.body.error.message.replace(`seen-${k}`, "X"));
    expect((await read(op, w, `held-${k}.md`)).body.data.body).toBe("OPERATOR-SECRET");
  });
});

describe("note access: the store is rebuilt once for the marker", () => {
  it("hides the notes already stored from a baseline device after the one-time rebuild, and never relabels a stage:0", async () => {
    const { op, w, base } = await world();
    await wiki(op, "global", "global");
    const k = freshKey();
    const g = await create(op, "global", { type: "rule", summary: `S0 rule ${k}` });
    const s0 = await sync(op, "global", { ops: [put(row("R9302", `OPERATOR-S0-${k}`, { tags: ["stage:0"] }))] });
    const s0id = s0.body.results[0].new_id ?? "R9302";
    await create(op, w, note(`old-${k}.md`, { tags: [`legacy-${k}`] }));
    await create(op, w, note(`new-${k}.md`, { tags: ["stage:0"] }));
    const stub = env.STORE.getByName("main");
    await runInDurableObject(stub, (_i, state) => {
      const sql = state.storage.sql;
      // a store as it was before the marker: every note visible, the flag missing
      sql.exec(`UPDATE records SET base_vis = 1 WHERE kind = 'N'`);
      sql.exec(`UPDATE record_tags SET vis = 1 WHERE kind = 'N'`);
      sql.exec(`DELETE FROM meta WHERE key = ?`, NOTE_ACCESS_KEY);
    });
    expect((await read(base, w, `old-${k}.md`)).status).toBe(200); // the old rows are what the index still says
    await runInDurableObject(stub, (_i, state) => {
      migrate(state.storage.sql);
      expect(state.storage.sql.exec(`SELECT 1 FROM meta WHERE key = ?`, NOTE_ACCESS_KEY).toArray()).toHaveLength(1);
      migrate(state.storage.sql); // once: a second migrate changes nothing
    });
    expect((await read(base, w, `old-${k}.md`)).status).toBe(404);
    expect((await read(base, w, `new-${k}.md`)).status).toBe(200);
    expect((await get(rec(w, "?kind=N"), base)).body.records.map((r: { id: string }) => r.id)).toEqual([`new-${k}.md`]);
    expect((await get("/v1/tags?wiki=" + w + "&system=1", base)).text).not.toContain(`legacy-${k}`);
    // a stage:0 the operator set is still the operator's
    expect((await get(rec("global", `/${s0id}`), op)).body.tags).toEqual(["stage:0"]);
    expect(g.status).toBe(201);
  });
});

describe("one note per project", () => {
  it("refuses a second note for a project on REST and MCP, naming the first, and never merges", async () => {
    const { op, w } = await world();
    const k = freshKey();
    const proj = `p-${k}`;
    expect((await create(op, w, note(`one-${k}.md`, { tags: [`project:${proj}`] }))).status).toBe(201);
    const two = await create(op, w, note(`two-${k}.md`, { tags: [`project:${proj}`, "x"] }));
    expect(two.status).toBe(409);
    expect(two.body.error.message).toContain(`one-${k}.md`);
    expect(two.body.error.message).toContain("edit");
    expect((await read(op, w, `two-${k}.md`)).status).toBe(404);
    for (const extra of [{ merge: true }, { merge: false }]) expect((await create(op, w, note(`three-${k}.md`, { tags: [`project:${proj}`], ...extra }))).status, JSON.stringify(extra)).toBe(400);
    const viaMcp = await tool(op, "save", { wiki: w, type: "note", path: `mcp-${k}.md`, body: "m", tags: [`project:${proj}`] });
    expect(viaMcp.isError).toBe(true);
    expect(viaMcp.text).toContain(`one-${k}.md`);
    expect((await tool(op, "save", { wiki: w, type: "note", path: `mcp2-${k}.md`, body: "m", project: proj })).isError).toBe(true); // `project` is no way round it
    // the same note, another project, another wiki, a document for the same project: all fine
    expect((await create(op, w, note(`other-${k}.md`, { tags: [`project:${proj}-b`] }))).status).toBe(201);
    expect((await create(op, w, { type: "document", title: `doc ${k}`, project: proj })).status).toBe(201);
    const w2 = freshKey();
    await wiki(op, w2);
    expect((await create(op, w2, note(`one-${k}.md`, { tags: [`project:${proj}`] }))).status).toBe(201);
    // and notes with no project tag are not capped
    for (const i of [1, 2, 3]) expect((await create(op, w, note(`free-${k}-${i}.md`))).status).toBe(201);
    // the holder gone, the project may have a note again
    expect((await send("DELETE", rec(w, `/${encodeURIComponent(`one-${k}.md`)}`), { token: op })).status).toBe(200);
    expect((await create(op, w, note(`two-${k}.md`, { tags: [`project:${proj}`] }))).status).toBe(201);
  });

  it("refuses to retag a note into a project another note holds, but lets a note keep its own", async () => {
    const { op, w } = await world();
    const k = freshKey();
    const proj = `p-${k}`;
    await create(op, w, note(`one-${k}.md`, { tags: [`project:${proj}`] }));
    await create(op, w, note(`two-${k}.md`, { tags: ["x"] }));
    const r = await patch(op, w, `two-${k}.md`, { tags: ["x", `project:${proj}`] });
    expect(r.status).toBe(409);
    expect(r.body.error.message).toContain(`one-${k}.md`);
    expect((await tool(op, "update", { wiki: w, id: `two-${k}.md`, tags: [`project:${proj}`] })).isError).toBe(true);
    expect((await read(op, w, `two-${k}.md`)).body.tags).toEqual(["x"]);
    expect((await patch(op, w, `one-${k}.md`, { tags: [`project:${proj}`, "more"], body: "edited" })).status).toBe(200); // its own project
    // a note moves between projects when the target is free, and frees the one it left
    expect((await patch(op, w, `one-${k}.md`, { tags: [`project:${proj}-moved`] })).status).toBe(200);
    expect((await patch(op, w, `two-${k}.md`, { tags: [`project:${proj}`] })).status).toBe(200);
    // a lookalike of the tag is no way round it
    for (const t of [`Project:${proj}`, `project:${proj.toUpperCase()}`, `project：${proj}`]) {
      expect((await create(op, w, note(`look-${k}.md`, { tags: [t] }))).status, t).toBe(400);
    }
  });

  it("never refuses a sync put of a note, and a sync cannot tag a second note into a project", async () => {
    const { op, w } = await world();
    const k = freshKey();
    const proj = `p-${k}`;
    const wire = (id: string) => ({ id, uid: "", kind: "N", created: "", updated: "2026-02-01T00:00:00Z", deleted: false, data: { body: id }, tags: [`project:${proj}`], links: [] });
    await create(op, w, note(`one-${k}.md`, { tags: [`project:${proj}`] }));
    const r = await sync(op, w, { ops: [put(wire(`two-${k}.md`)), put(wire(`three-${k}.md`))] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["accepted", "accepted"]);
    expect((await read(op, w, `two-${k}.md`)).body.tags).toEqual([]);
    // the project still keeps one note: tagging an extra into it through the API is refused
    expect((await patch(op, w, `two-${k}.md`, { body: "edited", tags: [`project:${proj}`] })).status).toBe(409);
  });

  it("does not tell a baseline device which hidden note holds a project, yet still refuses", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    const proj = `p-${k}`;
    await create(op, w, note(`secret-${k}.md`, { body: "OPERATOR-SECRET-BODY", tags: [`project:${proj}`] })); // hidden: no stage:0
    const second = await create(base, w, note(`mine-${k}.md`, { tags: [`project:${proj}`] }));
    expect(second.status).toBe(409);
    expect(second.text).not.toContain(`secret-${k}`);
    expect(second.text).not.toContain("OPERATOR-SECRET");
    expect(second.body.error.existing).toBeUndefined();
    expect(JSON.stringify(second.body)).not.toContain(`secret-${k}`);
    const viaMcp = await tool(base, "save", { wiki: w, type: "note", path: `mcp-${k}.md`, body: "m", tags: [`project:${proj}`] });
    expect(viaMcp.isError).toBe(true);
    expect(viaMcp.text).not.toContain(`secret-${k}`);
    // a retag of its own note into that project, the same
    await create(base, w, note(`own-${k}.md`));
    const retag = await patch(base, w, `own-${k}.md`, { tags: ["stage:0", `project:${proj}`] });
    expect(retag.status).toBe(409);
    expect(JSON.stringify(retag.body)).not.toContain(`secret-${k}`);
    expect((await read(base, w, `own-${k}.md`)).body.tags).toEqual(["stage:0"]);
    // nothing it asks leaves the project list or counts different
    expect(JSON.stringify((await get(`/v1/projects?wiki=${w}`, base)).body)).not.toContain(proj);
    // when the holder is one it can read, the refusal names it
    const seen = `p-seen-${k}`;
    await create(op, w, note(`seen-${k}.md`, { tags: ["stage:0", `project:${seen}`] }));
    const again = await create(base, w, note(`mine2-${k}.md`, { tags: [`project:${seen}`] }));
    expect(again.status).toBe(409);
    expect(again.body.error.message).toContain(`seen-${k}.md`);
  });

  it("holds the cap for a baseline device's own notes too", async () => {
    const { w, base } = await world();
    const k = freshKey();
    expect((await create(base, w, note(`a-${k}.md`, { tags: [`project:p-${k}`] }))).status).toBe(201);
    expect((await create(base, w, note(`b-${k}.md`, { tags: [`project:p-${k}`] }))).status).toBe(409);
    expect((await create(base, w, note(`c-${k}.md`))).status).toBe(201);
    expect((await patch(base, w, `c-${k}.md`, { tags: ["stage:0", `project:p-${k}`] })).status).toBe(409);
    // another full device is told by name: the project has the note a baseline device wrote
    const full = await mintAll(`full-${k}`);
    const r = await create(full.token, w, note(`d-${k}.md`, { tags: [`project:p-${k}`] }));
    expect(r.status).toBe(409);
    expect(r.body.error.message).toContain(`a-${k}.md`);
  });
});
