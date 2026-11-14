import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { OWNER, env, freshKey, get, mintAll, put, rec, row, send, sync, wiki } from "./helpers.js";
import { rpc, tool } from "./mcp-helpers.js";
import { oauthLogin } from "./oauth-helpers.js";

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const HIDDEN = "HIDDEN-MARKER";
const skillDoc = (id: string, name: string, version: number, status: string, tags: string[], body: string) => ({
  ...row(id, name), kind: "D",
  data: { title: `${name} v${version}`, body: `---\nname: ${name}\ndescription: ${name} description\n---\n${body}`, alias: null, mime: "text/markdown", source: "" },
  tags: ["skill", `skill:${name}`, `v:${version}`, `status:${status}`, ...tags],
});

interface World {
  op: string;
  w: string;
  project: string;
  hiddenIds: string[];
  visible: { rule: string; idea: string; decision: string; task: string; feature: string; job: string; note: string; doc: string };
}

/** The operator writes everything; two callers of baseline reach then read and write it. */
let seeds = 0;
async function seed(): Promise<World> {
  const k = ++seeds;
  const op = (await mintAll("operator")).token;
  const w = freshKey();
  await wiki(op, w);
  await wiki(op, "global", "global");
  const project = `proj-${w}`.toLowerCase();
  const hiddenIds: string[] = [];
  const hide = async (r: { body: { id: string } }) => { hiddenIds.push(r.body.id); };
  await hide(await create(op, w, { type: "rule", summary: `${HIDDEN}-RULE`, body: `${HIDDEN}-BODY`, tags: ["hidden-only-tag", "shared-tag", `project:${project}`] }));
  await hide(await create(op, w, { type: "rule", summary: `${HIDDEN}-STAGE1`, tags: ["stage:1", "shared-tag"] }));
  await hide(await create(op, w, { type: "memory", summary: `${HIDDEN}-MEMORY`, tags: ["hidden-only-tag", "shared-tag", `project:${project}`] }));
  await hide(await create(op, w, { type: "issue", summary: `${HIDDEN}-ISSUE`, tags: ["hidden-only-tag"] }));
  await sync(op, w, { ops: [put(skillDoc("D80", "hidden-skill", 1, "active", ["stage:1", "hidden-only-tag"], `${HIDDEN}-SKILLBODY`))] });
  hiddenIds.push("D80");
  const stage0 = await sync(op, "global", { ops: [put(row(`R${500 + k}`, "VISIBLE-S0-RULE", { tags: ["stage:0", "shared-tag", `project:${project}`] })), put(skillDoc(`D${800 + k}`, "visible-skill", 1, "active", ["stage:0"], "VISIBLE-SKILLBODY"))] });
  expect(stage0.body.results.map((r: { status: string }) => r.status)).toEqual(["accepted", "accepted"]);
  const idea = await create(op, w, { type: "idea", summary: "VISIBLE-IDEA", tags: ["shared-tag"], project });
  const decision = await create(op, w, { type: "decision", summary: "VISIBLE-DECISION", tags: ["shared-tag"], project });
  const task = await create(op, w, { type: "human_task", title: "VISIBLE-TASK", tags: ["shared-tag"], project });
  const feature = await create(op, w, { type: "feature", title: "VISIBLE-FEATURE" });
  const job = await create(op, w, { type: "job", title: "VISIBLE-JOB", parent: feature.body.id });
  const note = await create(op, w, { type: "note", path: "research/visible.md", body: "VISIBLE-NOTE", tags: ["shared-tag"] });
  const doc = await create(op, w, { type: "document", title: "VISIBLE-DOC", body: "text", project, tags: ["shared-tag"] });
  await send("PUT", `/v1/wikis/${w}/focus`, { token: op, body: { northstar: "VISIBLE-NORTH", fires: [{ feature: feature.body.id }] } });
  await create(op, "global", { type: "human_task", title: "VISIBLE-GLOBAL-TASK" });
  return { op, w, project, hiddenIds, visible: { rule: `R${500 + k}`, idea: idea.body.id, decision: decision.body.id, task: task.body.id, feature: feature.body.id, job: job.body.id, note: note.body.id, doc: doc.body.id } };
}

interface Caller {
  name: string;
  token: string;
}

async function callers(): Promise<Caller[]> {
  const oauth = await oauthLogin({ scopes: ["read", "write", "delete"], name: `chat-${freshKey()}` });
  const minted = await send("POST", "/admin/devices", { token: OWNER, body: { name: `agent-${freshKey()}`, scopes: ["read", "write", "delete"], reach: "baseline" } });
  expect(minted.status).toBe(201);
  expect(minted.body.reach).toBe("baseline");
  return [{ name: "oauth", token: oauth.access }, { name: "baseline token", token: minted.body.token }];
}

describe("reach: the device row", () => {
  it("shows the reach on whoami and in the admin list; OAuth grants are baseline, tokens full unless minted otherwise", async () => {
    const [oauth, base] = await callers();
    const full = await mintAll("full-one");
    expect((await get("/v1/whoami", oauth!.token)).body.reach).toBe("baseline");
    expect((await get("/v1/whoami", base!.token)).body.reach).toBe("baseline");
    expect((await get("/v1/whoami", full.token)).body.reach).toBe("full");
    const list = (await send("GET", "/admin/devices", { token: OWNER })).body.devices as { id: string; reach: string; source: string }[];
    expect(list.find((d) => d.id === full.id)).toMatchObject({ reach: "full", source: "token" });
    expect(list.filter((d) => d.source === "oauth").every((d) => d.reach === "baseline")).toBe(true);
    expect((await send("POST", "/admin/devices", { token: OWNER, body: { name: "x", scopes: ["read"], reach: "root" } })).status).toBe(400);
  });

  it("treats a row stored before reach existed as full for a token and baseline for an OAuth grant", async () => {
    const stub = env.STORE.getByName("main");
    const ids = await runInDurableObject(stub, (_i, state) => {
      const sql = state.storage.sql;
      sql.exec(`INSERT INTO devices (id, name, scopes, wikis, token_hash, source, created) VALUES ('aaaaaaaaaaaaaaa1', 'legacy token', '["read"]', NULL, 'x', 'token', '2026-01-01T00:00:00Z')`);
      sql.exec(`INSERT INTO devices (id, name, scopes, wikis, token_hash, source, created) VALUES ('aaaaaaaaaaaaaaa2', 'legacy grant', '["read"]', NULL, '', 'oauth', '2026-01-01T00:00:00Z')`);
      return sql.exec(`SELECT id, reach FROM devices WHERE id LIKE 'aaaaaaaaaaaaaaa%' ORDER BY id`).toArray();
    });
    expect(ids.map((r) => r["reach"])).toEqual(["", ""]);
    const list = (await send("GET", "/admin/devices", { token: OWNER })).body.devices as { id: string; reach: string }[];
    expect(list.find((d) => d.id === "aaaaaaaaaaaaaaa1")!.reach).toBe("full");
    expect(list.find((d) => d.id === "aaaaaaaaaaaaaaa2")!.reach).toBe("baseline");
  });
});

describe("reach: what a baseline device reads", () => {
  it("sees stage-0 rules, ideas, decisions, notes, documents, tasks and the focus tree, and nothing else", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const t = c.token;
      const list = async (kind: string) => ((await get(rec(s.w, `?kind=${kind}`), t)).body.records as { id: string; data: Record<string, string> }[]);
      expect(await list("R"), c.name).toEqual([]); // the repo wiki's rules are stage-less
      expect((await list("I")).length).toBe(0);
      expect((await list("M")).map((r) => r.data["summary"]).sort(), c.name).toEqual(["VISIBLE-DECISION", "VISIBLE-IDEA"]);
      expect((await list("D")).map((r) => r.data["title"]).sort()).toEqual(expect.arrayContaining(["VISIBLE-DOC"]));
      expect(JSON.stringify(await list("D"))).not.toContain(HIDDEN);
      expect((await get(rec("global", `/${s.visible.rule}`), t)).body.data.summary, c.name).toBe("VISIBLE-S0-RULE");
      expect((await get(rec(s.w, `/${encodeURIComponent(s.visible.note)}`), t)).status).toBe(200);
      expect((await get(rec(s.w, `/${s.visible.task}`), t)).status).toBe(200);
      expect((await get(`/v1/wikis/${s.w}/focus`, t)).body.fires[0].jobs[0].title).toBe("VISIBLE-JOB");
      const counts = (await get("/v1/wikis", t)).body.wikis.find((x: { key: string }) => x.key === s.w).counts;
      expect(counts, c.name).toMatchObject({ memory: 2, feature: 1, job: 1, note: 1, task: 1 });
      expect(counts.rule, c.name).toBeUndefined();
      expect(counts.issue).toBeUndefined();
    }
  });

  it("answers about a hidden record exactly as about one that does not exist", async () => {
    const s = await seed();
    for (const c of await callers()) {
      for (const id of s.hiddenIds.filter((i) => i !== "D80")) {
        const hidden = await get(rec(s.w, `/${id}`), c.token);
        const missing = await get(rec(s.w, `/${id[0]}99999`), c.token);
        expect(hidden.status, `${c.name} ${id}`).toBe(404);
        expect(hidden.body.error.message.replace(id, "X")).toBe(missing.body.error.message.replace(`${id[0]}99999`, "X"));
        expect((await get(`/v1/get?id=${id}&wiki=${s.w}`, c.token)).body.matches).toEqual([]);
        expect((await get(rec(s.w, `/${id}/related`), c.token)).status).toBe(404);
        for (const body of [{ summary: "edit" }, { tags: ["x"] }]) expect((await send("PATCH", rec(s.w, `/${id}`), { token: c.token, body })).status, `${c.name} patch ${id}`).toBe(404);
        expect((await send("DELETE", rec(s.w, `/${id}`), { token: c.token })).status).toBe(403); // never deletes, hidden or not
      }
      expect((await get(`/v1/wikis/${s.w}/skills/hidden-skill?version=1`, c.token)).status).toBe(404);
      expect((await get(`/v1/wikis/${s.w}/skills`, c.token)).body.skills).toEqual([]);
      expect((await get(`/v1/wikis/global/skills/visible-skill`, c.token)).body.body).toBe("VISIBLE-SKILLBODY");
    }
  });

  it("finds nothing hidden by search, tag, topic count, project or related", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const t = c.token;
      expect((await get(`/v1/search?q=${HIDDEN}&wiki=${s.w}`, t)).body.results, c.name).toEqual([]);
      expect((await get(`/v1/search?tags=hidden-only-tag&wiki=${s.w}`, t)).body.results, c.name).toEqual([]);
      expect((await get(`/v1/search?q=hidden&types=rule,memory,issue`, t)).body.results).toEqual([]);
      const shared = (await get(`/v1/search?tags=shared-tag&wiki=${s.w}`, t)).body.results as { summary: string }[];
      expect(shared.map((r) => r.summary).sort(), c.name).toEqual(["VISIBLE-DECISION", "VISIBLE-DOC", "VISIBLE-IDEA", "VISIBLE-NOTE", "VISIBLE-TASK"]);
      const tags = (await get(`/v1/tags?system=1`, t)).body.tags as { tag: string; total: number; counts: Record<string, number> }[];
      expect(tags.find((x) => x.tag === "hidden-only-tag"), c.name).toBeUndefined();
      const inGlobal = (await get(`/v1/tags?system=1&wiki=global`, t)).body.tags as { tag: string; counts: Record<string, number> }[];
      expect(inGlobal.find((x) => x.tag === "shared-tag")!.counts["rule"], c.name).toBe(seeds); // only the stage-0 rule each seed made
      const inRepo = (await get(`/v1/tags?system=1&wiki=${s.w}`, t)).body.tags as { tag: string; counts: Record<string, number> }[];
      expect(inRepo.find((x) => x.tag === "shared-tag")!.counts, c.name).toEqual({ memory: 2, document: 1, task: 1, note: 1 }); // the hidden rule, stage-1 rule and memory are not counted
      expect(JSON.stringify((await get(`/v1/tags?wiki=${s.w}&system=1`, t)).body)).not.toContain("stage:1");
      const shown = (await get(`/v1/projects/${s.project}?wiki=${s.w}`, t)).body;
      expect(JSON.stringify(shown), c.name).not.toContain(HIDDEN);
      expect(shown.rules, c.name).toEqual([]);
      expect(shown.memories).toEqual([]);
      expect(shown.more.rules).toBe(0);
      expect(shown.more.memories).toBe(0);
      const rel = (await get(rec(s.w, `/${s.visible.idea}/related`), t)).body.related as { summary: string }[];
      expect(JSON.stringify(rel)).not.toContain(HIDDEN);
      expect(rel.length).toBeGreaterThan(0);
      expect((await get(`/v1/projects?wiki=${s.w}`, t)).body.projects.find((p: { key: string }) => p.key === s.project).open_tasks).toBe(1);
    }
  });

  it("serves only the baseline in the session text, whatever stage it asks for", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const text = (await get(`/v1/instructions?client=mcp&wiki=${s.w}&stage=9&full=1`, c.token)).body.text as string;
      expect(text, c.name).not.toContain(HIDDEN);
      expect(text).not.toContain("hidden-only-tag");
      expect(text).not.toContain("hidden-skill");
      expect(text).toContain(`${s.visible.rule} VISIBLE-S0-RULE`);
      expect(text).toContain("visible-skill - visible-skill description");
      expect(text).toContain("VISIBLE-NORTH");
      expect(text).toContain("VISIBLE-JOB");
      const viaMcp = (await tool(c.token, "session_start", { wiki: s.w, stage: 9, full: true })).text;
      expect(viaMcp).not.toContain(HIDDEN);
      expect(viaMcp).not.toContain("hidden-skill");
    }
    // the operator's own device sees them with the stage raised
    const full = (await get(`/v1/instructions?client=mcp&wiki=${s.w}&stage=9&full=1`, s.op)).body.text as string;
    expect(full).toContain(`${HIDDEN}-RULE`);
  });

  it("is checked on every read route and every MCP tool, so a new one cannot forget the filter", async () => {
    const s = await seed();
    const spec = (await send("GET", "/openapi.json")).body.paths as Record<string, Record<string, unknown>>;
    const getPaths = Object.entries(spec).filter(([p, m]) => p.startsWith("/v1/") && "get" in m).map(([p]) => p).sort();
    const w = s.w;
    const urls: Record<string, string[]> = {
      "/v1/whoami": ["/v1/whoami"],
      "/v1/wikis": ["/v1/wikis"],
      "/v1/instructions": ["/v1/instructions?stage=9&full=1", `/v1/instructions?wiki=${w}&stage=9&full=1&client=rest`],
      "/v1/wikis/{wiki}/focus": [`/v1/wikis/${w}/focus`],
      "/v1/wikis/{wiki}/skills": [`/v1/wikis/${w}/skills`, "/v1/wikis/global/skills"],
      "/v1/wikis/{wiki}/skills/{name}": [`/v1/wikis/${w}/skills/hidden-skill?version=1`, `/v1/wikis/${w}/skills/hidden-skill`, "/v1/wikis/global/skills/visible-skill"],
      "/v1/search": [`/v1/search?q=${HIDDEN}`, `/v1/search?q=hidden&k=50`, "/v1/search?tags=hidden-only-tag", "/v1/search?tags=shared-tag&k=50", `/v1/search?q=VISIBLE&types=rule,memory,issue,skill`],
      "/v1/tags": ["/v1/tags?system=1&limit=1000", `/v1/tags?wiki=${w}&system=1`, "/v1/tags?prefix=hidden"],
      "/v1/get": [`/v1/get?id=R1`, `/v1/get?id=M1&wiki=${w}`, "/v1/get?id=D80", `/v1/get?id=${s.visible.rule}`],
      "/v1/wikis/{wiki}/records": ["R", "M", "I", "D", "T", "F", "J", "N"].map((k) => `/v1/wikis/${w}/records?kind=${k}&limit=200`).concat([`/v1/wikis/${w}/records?tag=hidden-only-tag`, `/v1/wikis/${w}/records`]),
      "/v1/wikis/{wiki}/records/{id}": [`/v1/wikis/${w}/records/R1`, `/v1/wikis/${w}/records/M1`, `/v1/wikis/${w}/records/I1`, `/v1/wikis/${w}/records/D80`, `/v1/wikis/global/records/${s.visible.rule}`],
      "/v1/wikis/{wiki}/records/{id}/related": [`/v1/wikis/${w}/records/R1/related`, `/v1/wikis/${w}/records/${s.visible.idea}/related?limit=50`, `/v1/wikis/${w}/records/${s.visible.doc}/related?limit=50`],
      "/v1/tasks": ["/v1/tasks?status=all", "/v1/tasks?owner=agent"],
      "/v1/projects": [`/v1/projects?wiki=${w}`, "/v1/projects"],
      "/v1/projects/{key}": [`/v1/projects/${s.project}?wiki=${w}`, `/v1/projects/${s.project}?wiki=global`],
    };
    expect(getPaths, "a read route has no reach test: add it to this table").toEqual(Object.keys(urls).sort());
    for (const c of await callers()) {
      for (const [path, list] of Object.entries(urls)) {
        for (const url of list) {
          const r = await get(url, c.token);
          expect([200, 404], `${c.name} ${url} -> ${r.status}`).toContain(r.status);
          expect(r.text, `${c.name} ${url}`).not.toContain(HIDDEN);
          expect(r.text, `${c.name} ${url}`).not.toContain("hidden-only-tag");
          if (r.status === 200) expect(r.text, `${c.name} ${path}`).not.toContain("hidden-skill"); // a 404 may echo the name asked for
          expect(r.text).not.toContain("stage:1");
        }
      }
      // every MCP read tool
      const listed = ((await rpc(c.token, "tools/list")).json.result.tools as { name: string; annotations?: { readOnlyHint?: boolean } }[]);
      const reads = listed.filter((t) => t.annotations?.readOnlyHint).map((t) => t.name).sort();
      const calls: Record<string, Record<string, unknown>[]> = {
        session_start: [{ stage: 9, full: true, wiki: w }, { client: "rest", stage: 9 }],
        focus: [{ wiki: w }, { wiki: "global" }],
        skills: [{ wiki: w }, { wiki: "global" }],
        skill: [{ name: "hidden-skill", wiki: w, version: 1 }, { name: "hidden-skill", wiki: w }, { name: "visible-skill", wiki: "global" }],
        search: [{ query: HIDDEN }, { query: "hidden", k: 50 }, { query: "", tags: ["hidden-only-tag"] }, { query: "", tags: ["shared-tag"], k: 50 }],
        get: [{ id: "R1", wiki: w }, { id: "D80" }, { id: "M1" }],
        ledger_list: [{ wiki: w }, {}],
        tags: [{ system: true, limit: 1000 }, { wiki: w, system: true }],
        related: [{ id: "R1", wiki: w }, { id: s.visible.idea, wiki: w, limit: 50 }],
        tasks: [{ status: "all" }, { owner: "agent" }, { project: s.project }],
        projects: [{ wiki: w }, {}],
        project: [{ key: s.project, wiki: w }],
        wikis: [{}],
      };
      expect(reads, "a read tool has no reach test: add it to this table").toEqual(Object.keys(calls).sort());
      for (const [name, argsList] of Object.entries(calls)) {
        for (const args of argsList) {
          const r = await tool(c.token, name, args);
          expect(r.text, `${c.name} ${name} ${JSON.stringify(args)}`).not.toContain(HIDDEN);
          expect(r.text).not.toContain("hidden-only-tag");
          if (!r.isError) expect(r.text).not.toContain("hidden-skill"); // an error may echo the name that was asked for
          expect(r.text).not.toContain("stage:1");
        }
      }
    }
  });
});

describe("reach: what a baseline device writes", () => {
  it("writes ideas, decisions, notes, documents, tasks, project files and items, and works its tasks", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const t = c.token;
      const tag = freshKey();
      for (const body of [
        { type: "idea", summary: `idea ${tag}` }, { type: "decision", summary: `decision ${tag}` }, { type: "human_task", title: `human ${tag}`, project: s.project },
        { type: "agent_task", title: `agent ${tag}` }, { type: "task", title: `plain ${tag}` }, { type: "document", title: `doc ${tag}`, body: "b", project: `doc-${tag}`.toLowerCase() },
        { type: "note", path: `research/${tag}.md`, body: "n" },
      ]) {
        const r = await create(t, s.w, body);
        expect(r.status, `${c.name} ${body.type}: ${r.text}`).toBe(201);
      }
      expect((await send("PUT", `/v1/projects/${tag}`, { token: t, body: { wiki: s.w } })).status).toBe(201);
      expect((await send("POST", `/v1/projects/${tag}/items`, { token: t, body: { section: "issues", text: "an issue", wiki: s.w } })).status).toBe(201);
      expect((await tool(t, "project_add", { key: tag, section: "questions", text: "a question", wiki: s.w })).isError).toBe(false);
      // it reads its own ideas and decisions back, and edits them
      const mine = (await create(t, s.w, { type: "idea", summary: `own ${tag}` })).body;
      expect((await get(rec(s.w, `/${mine.id}`), t)).status).toBe(200);
      expect((await send("PATCH", rec(s.w, `/${mine.id}`), { token: t, body: { summary: `edited ${tag}` } })).body.data.summary).toBe(`edited ${tag}`);
      // it works tasks: claim, release, close
      const task = (await create(t, s.w, { type: "human_task", title: `work ${tag}` })).body.id;
      expect((await send("POST", rec(s.w, `/${task}/claim`), { token: t })).status).toBe(200);
      expect((await send("POST", rec(s.w, `/${task}/release`), { token: t })).status).toBe(200);
      expect((await send("POST", rec(s.w, `/${task}/close`), { token: t, body: { reason: "agent" } })).status).toBe(200);
      expect((await tool(t, "save", { wiki: s.w, type: "decision", summary: `via mcp ${tag}` })).isError).toBe(false);
      expect((await tool(t, "save", { wiki: "global", type: "human_task", summary: `global ${tag}` })).isError).toBe(false);
    }
  });

  it("never writes a rule, memory, issue or skill version, at any stage, a proposal included", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const t = c.token;
      for (const type of ["rule", "memory", "issue"]) {
        for (const tags of [undefined, ["stage:0"], ["stage:1"], ["stage-proposed:0"], ["idea"]]) {
          const r = await create(t, s.w, { type, summary: `try ${type}`, ...(tags ? { tags } : {}) });
          expect(r.status, `${c.name} ${type} ${JSON.stringify(tags)}`).toBe(403);
          expect(r.body.error.message).toBe("this device writes notes, tasks, ideas, decisions and files");
        }
      }
      for (const type of ["feature", "job"]) expect((await create(t, s.w, { type, title: "x" })).status, type).toBe(403);
      // an idea or decision may not carry a stage, nor be turned into a memory
      expect((await create(t, "global", { type: "idea", summary: "staged idea", tags: ["stage:0"] })).status).toBe(403);
      expect((await create(t, s.w, { type: "idea", summary: "staged idea", tags: ["stage:2"] })).status).toBe(403);
      const idea = (await create(t, "global", { type: "idea", summary: `turn ${freshKey()}` })).body;
      expect((await send("PATCH", rec("global", `/${idea.id}`), { token: t, body: { tags: [] } })).status).toBe(403);
      expect((await send("PATCH", rec("global", `/${idea.id}`), { token: t, body: { tags: ["idea", "stage:0"] } })).status).toBe(403);
      // the visible stage-0 rule is read-only for it, and so is a skill version
      expect((await send("PATCH", rec("global", `/${s.visible.rule}`), { token: t, body: { summary: "rewritten" } })).status).toBe(403);
      expect((await send("PATCH", rec("global", `/D${800 + seeds}`), { token: t, body: { body: "rewritten" } })).status).toBe(403);
      expect((await tool(t, "save", { wiki: s.w, type: "rule", summary: "x" })).text).toMatch(/^forbidden:/);
      expect((await tool(t, "save", { wiki: s.w, type: "memory", summary: "x" })).isError).toBe(true);
      expect((await tool(t, "update", { wiki: "global", id: s.visible.rule, summary: "x" })).isError).toBe(true);
      expect((await tool(t, "ledger_add", { wiki: s.w, kind: "feature", title: "x" })).isError).toBe(true);
      expect((await tool(t, "ledger_add", { wiki: s.w, kind: "human_task", title: `ok ${freshKey()}` })).isError).toBe(false);
      // a sync put is not a way round it
      const sy = await sync(t, s.w, { ops: [put(row("R70", "synced", { tags: ["stage:0"] }))] });
      expect(sy.status, c.name).toBe(403);
      expect((await get(`/v1/search?q=synced&wiki=${s.w}`, s.op)).body.results).toEqual([]);
    }
  });

  it("never sets the focus, closes a feature or job, or deletes anything", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const t = c.token;
      expect((await send("PUT", `/v1/wikis/${s.w}/focus`, { token: t, body: { northstar: "taken over" } })).status).toBe(403);
      expect((await send("POST", `/v1/wikis/${s.w}/focus/promote`, { token: t, body: {} })).status).toBe(403);
      expect((await tool(t, "focus_set", { wiki: s.w, northstar: "taken over" })).isError).toBe(true);
      for (const id of [s.visible.feature, s.visible.job]) {
        for (const action of ["claim", "release"]) expect((await send("POST", rec(s.w, `/${id}/${action}`), { token: t })).status, `${action} ${id}`).toBe(403);
        expect((await send("POST", rec(s.w, `/${id}/close`), { token: t, body: { reason: "agent" } })).status).toBe(403);
        expect((await tool(t, "ledger_set", { wiki: s.w, id, action: "close" })).isError).toBe(true);
      }
      // the focus record itself, by alias, is not a document to edit
      expect((await send("PATCH", rec(s.w, "/focus"), { token: t, body: { body: "{\"northstar\":\"x\"}" } })).status).toBe(403);
      for (const id of [s.visible.task, s.visible.note, s.visible.doc, s.visible.idea]) {
        expect((await send("DELETE", rec(s.w, `/${encodeURIComponent(id)}`), { token: t })).status, `delete ${id}`).toBe(403);
      }
      const focus = (await get(`/v1/wikis/${s.w}/focus`, s.op)).body;
      expect(focus.northstar).toBe("VISIBLE-NORTH");
      expect(focus.fires[0].feature).toBe(s.visible.feature);
    }
  });

  it("never creates, edits or deletes the notes printed into every session text", async () => {
    const s = await seed();
    const note = (path: string, body = "planted") => ({ type: "note", path, body });
    // the operator's own notes exist
    expect((await create(s.op, "global", { type: "note", path: "session-instructions.md", body: "OPERATOR-INSTRUCTIONS" })).status).toBeLessThan(500);
    expect((await create(s.op, "global", { type: "note", path: "response-style.md", body: "OPERATOR-STYLE" })).status).toBeLessThan(500);
    for (const c of await callers()) {
      const t = c.token;
      for (const path of ["session-instructions.md", "response-style.md", "Session-Instructions.md", "RESPONSE-STYLE.MD", "arch/session-instructions.md", "research/Response-Style.md"]) {
        expect([400, 403], `${c.name} ${path}`).toContain((await create(t, "global", note(path))).status); // 400: not even a valid note path
      }
      // a note whose frontmatter names it, a document or an idea that takes the alias, a rename
      expect((await create(t, "global", note("research/innocent.md", "---\nid: response-style\n---\nplanted"))).status).toBe(403);
      expect((await create(t, "global", { type: "document", title: "d", alias: "session-instructions" })).status).toBe(403);
      expect((await create(t, "global", { type: "idea", summary: "i", alias: "Response-Style" })).status).toBe(403);
      const mine = (await create(t, "global", { type: "idea", summary: `rename ${freshKey()}` })).body;
      expect((await send("PATCH", rec("global", `/${mine.id}`), { token: t, body: { alias: "session-instructions" } })).status).toBe(403);
      // editing or deleting the operator's: by id, by alias, with and without .md
      for (const ref of ["session-instructions.md", "session-instructions", "response-style.md", "response-style"]) {
        expect((await send("PATCH", rec("global", `/${ref}`), { token: t, body: { body: "planted" } })).status, `patch ${ref}`).toBe(403);
        expect((await send("DELETE", rec("global", `/${ref}`), { token: t })).status, `delete ${ref}`).toBe(403);
      }
      // a repo wiki's note of that name is no printed note
      expect((await create(t, s.w, note(`session-instructions.md`))).status).toBeLessThan(500);
    }
    const text = (await get("/v1/instructions?client=mcp", s.op)).body.text as string;
    expect(text).toContain("OPERATOR-INSTRUCTIONS");
    expect(text).toContain("OPERATOR-STYLE");
    expect(text).not.toContain("planted");
  });

  it("neither sets nor drops a rank tag: the order of the operator's list is the operator's", async () => {
    const s = await seed();
    const ranked = await create(s.op, s.w, { type: "human_task", title: "operator's ranked task", tags: ["rank:1"] });
    for (const c of await callers()) {
      expect((await create(c.token, s.w, { type: "human_task", title: `jump the queue ${c.name}`, tags: ["rank:1"] })).status, c.name).toBe(403);
      expect((await send("PATCH", rec(s.w, `/${ranked.body.id}`), { token: c.token, body: { tags: ["human_task"] } })).status).toBe(403); // dropping it
      const mine = (await create(c.token, s.w, { type: "human_task", title: `mine ${c.name}` })).body;
      expect((await send("PATCH", rec(s.w, `/${mine.id}`), { token: c.token, body: { tags: ["human_task", "rank:1"] } })).status).toBe(403); // adding it
      expect((await send("PATCH", rec(s.w, `/${ranked.body.id}`), { token: c.token, body: { tags: ["human_task", "rank:1", "extra"] } })).status).toBe(200); // keeping it is fine
    }
    expect((await get(rec(s.w, `/${ranked.body.id}`), s.op)).body.tags).toContain("rank:1");
  });

  it("never registers a wiki: a baseline device may use the ones that exist and no more", async () => {
    const s = await seed();
    for (const c of await callers()) {
      const key = freshKey();
      const made = await send("PUT", `/v1/wikis/${key}`, { token: c.token, body: { name: key, kind: "repo", origin: "" } });
      expect(made.status, `${c.name} new wiki`).toBe(403);
      expect((await get("/v1/wikis", s.op)).body.wikis.some((w: { key: string }) => w.key === key), `${c.name} stored nothing`).toBe(false);
      // an existing wiki answers as before (idempotent), and a full device still registers one
      expect((await send("PUT", `/v1/wikis/${s.w}`, { token: c.token, body: { name: s.w, kind: "repo", origin: "" } })).status, `${c.name} existing wiki`).toBe(200);
    }
    const key = freshKey();
    expect((await send("PUT", `/v1/wikis/${key}`, { token: s.op, body: { name: key, kind: "repo", origin: "" } })).status).toBe(201);
  });

  it("counts only the projects it can see against the cap, so a full wiki's hidden projects are not revealed", async () => {
    const s = await seed();
    const w = freshKey();
    await wiki(s.op, w);
    // 1,000 projects that exist only on records a baseline device may not read (a rule below stage 0)
    await runInDurableObject(env.STORE.getByName("main"), (_i, state) => {
      for (let i = 0; i < 1000; i++) state.storage.sql.exec(`INSERT INTO tag_counts (wiki, tag, bucket, vis, n) VALUES (?, ?, 'R', 0, 1)`, w, `project:hidden-${i}`);
    });
    const key = `cap-${freshKey()}`.toLowerCase();
    const full = await send("PUT", `/v1/projects/${key}`, { token: s.op, body: { wiki: w } });
    expect(full.status).toBe(413);
    for (const c of await callers()) {
      expect((await send("PUT", `/v1/projects/${key}-${c.name.replace(/\W/g, "")}`, { token: c.token, body: { wiki: w } })).status, c.name).toBe(201);
    }
  });

  it("neither sets nor adds an engine tag: only idea, decision, the task owner and one project", async () => {
    const s = await seed();
    const operatorsTask = await create(s.op, s.w, { type: "human_task", title: "operator's served task", tags: ["serves:northstar", "rank:1"] });
    for (const c of await callers()) {
      for (const tags of [["principle"], ["churn"], ["violations:3"], ["serves:northstar"], ["repeat-of:R1"], ["evidence:x"], ["learned"], ["status:active"], ["v:2"], ["focus"], ["rank:2"]]) {
        const r = await create(c.token, s.w, { type: "idea", summary: `tagged ${tags[0]} ${c.name} ${freshKey()}`, tags });
        expect(r.status, `${c.name} create ${tags[0]}`).toBe(403);
      }
      const ok = await create(c.token, s.w, { type: "idea", summary: `topic ${c.name} ${freshKey()}`, tags: ["topic-x", "decision", "project:fine-one"] });
      expect(ok.status, `${c.name} topic tags: ${ok.text}`).toBe(201);
      // adding one on an edit is refused too, through REST and MCP
      expect((await send("PATCH", rec(s.w, `/${ok.body.id}`), { token: c.token, body: { tags: [...ok.body.tags, "churn"] } })).status, `${c.name} patch`).toBe(403);
      expect((await tool(c.token, "update", { wiki: s.w, id: ok.body.id, tags: [...ok.body.tags, "violations:1"] })).isError, `${c.name} mcp update`).toBe(true);
      expect((await tool(c.token, "save", { wiki: s.w, type: "idea", summary: `mcp ${c.name} ${freshKey()}`, tags: ["principle"] })).isError, `${c.name} mcp save`).toBe(true);
      // an engine tag the operator put on a task stays when the device edits around it
      const kept = await send("PATCH", rec(s.w, `/${operatorsTask.body.id}`), { token: c.token, body: { tags: ["human_task", "serves:northstar", "rank:1", "extra"] } });
      expect(kept.status, `${c.name} keeps: ${kept.text}`).toBe(200);
      expect((await send("PATCH", rec(s.w, `/${operatorsTask.body.id}`), { token: c.token, body: { title: `renamed ${c.name}` } })).status).toBe(200);
    }
    // a full device writes them as before
    expect((await create(s.op, s.w, { type: "idea", summary: `full ${freshKey()}`, tags: ["principle", "churn", "violations:3", "serves:northstar"] })).status).toBe(201);
    expect((await sync(s.op, s.w, { ops: [put(row(`R${900000 + ++seeds}`, `full sync ${freshKey()}`, { tags: ["principle", "violations:2", "serves:northstar"] }))] })).body.results[0].status).toBe("accepted");
  });

  it("drops an alias that a live record holds, for a baseline device, so nothing tells it the alias exists", async () => {
    const s = await seed();
    const taken = `secret-${freshKey()}`;
    await create(s.op, s.w, { type: "rule", summary: "has an alias", alias: taken });
    await create(s.op, s.w, { type: "decision", summary: "visible holder", alias: `${taken}-v` });
    for (const c of await callers()) {
      // the same answer for an alias a hidden record holds, a visible record holds, and one nobody holds
      for (const alias of [taken, `${taken}-v`]) {
        const r = await create(c.token, s.w, { type: "idea", summary: `wants it ${freshKey()}`, alias });
        expect(r.status, `${c.name} ${alias}`).toBe(201);
        expect(r.body.data.alias).toBeNull();
        expect(JSON.stringify(r.body)).not.toMatch(/R\d/);
      }
      const free = await create(c.token, s.w, { type: "idea", summary: `free ${freshKey()}`, alias: `free-${freshKey()}` });
      expect(free.status).toBe(201);
      expect(free.body.data.alias).toMatch(/^free-/);
      // a rename onto a taken alias keeps the alias the record had
      const mine = await create(c.token, s.w, { type: "idea", summary: `mine ${freshKey()}`, alias: `mine-${freshKey()}` });
      const renamed = await send("PATCH", rec(s.w, `/${mine.body.id}`), { token: c.token, body: { alias: taken } });
      expect(renamed.status).toBe(200);
      expect(renamed.body.data.alias).toBe(mine.body.data.alias);
    }
    // a full device is still told which record holds it
    const full = await create(s.op, s.w, { type: "idea", summary: `full wants it ${freshKey()}`, alias: taken });
    expect(full.status).toBe(409);
    expect(full.body.error.message).toMatch(/R\d/);
  });

  it("shows a baseline device only the write-back it may use, and a full device everything", async () => {
    const s = await seed();
    const mcpFull = (await get("/v1/instructions?client=mcp", s.op)).body.text as string;
    expect(mcpFull).toContain("- rule:");
    expect(mcpFull).toContain("focus_set");
    for (const c of await callers()) {
      for (const client of ["mcp", "rest"]) {
        const t = (await get(`/v1/instructions?client=${client}`, c.token)).body.text as string;
        const block = t.slice(t.indexOf("HOW TO WRITE BACK"));
        for (const needle of ["decision", "idea", "human_task", "agent_task", "document", "note"]) expect(block, `${c.name} ${client} ${needle}`).toContain(needle);
        for (const gone of ["- rule:", "- memory:", "- issue:", "focus_set", "PUT it {\"northstar\"", "rule (a standing", "issue (a mistake", "feature | job"]) expect(block, `${c.name} ${client} ${gone}`).not.toContain(gone);
      }
    }
  });
});
