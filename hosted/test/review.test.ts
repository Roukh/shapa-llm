import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { Repo } from "../src/repo.js";
import { OWNER, env, freshKey, get, mint, mintAll, put, rec, row, send, sync, wiki } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

// The review round: identical text and edits it must not make, skills a baseline device must not
// touch, hidden records it must not detect, one stage marker, lookalike tags, the printed notes, the
// baseline for a device with a short wiki list, and the tag listing's index.

const ZW = String.fromCharCode(0x200b);
const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const rev = async (token: string): Promise<number> => Number(String((await get("/v1/instructions?client=rest", token)).body.version).split(".")[1]);

let next = 9000;
const uniq = (): number => ++next;

async function operator(): Promise<string> {
  const op = (await mintAll("operator")).token;
  await wiki(op, "global", "global");
  return op;
}

async function baselineToken(wikis: string[] | null = null): Promise<string> {
  const r = await send("POST", "/admin/devices", { token: OWNER, body: { name: `api-${freshKey()}`, scopes: ["read", "write", "delete"], wikis, reach: "baseline" } });
  expect(r.status).toBe(201);
  return r.body.token as string;
}

const skillDoc = (id: string, name: string, version: number, status: string, tags: string[], body: string) => ({
  ...row(id, name), kind: "D",
  data: { title: `${name} v${version}`, body: `---\nname: ${name}\ndescription: ${name} description\n---\n${body}`, alias: null, mime: "text/markdown", source: "" },
  tags: ["skill", `skill:${name}`, `v:${version}`, `status:${status}`, ...tags],
});

describe("identical text edits a record only as far as its writer could edit it directly", () => {
  it("hands a stage-0 holder back unchanged to a baseline device and to a read/write token", async () => {
    const op = await operator();
    const base = await baselineToken();
    const rw = (await mint(`rw-${freshKey()}`, ["read", "write"])).token;
    const text = `SAME-TEXT-${freshKey()}`;
    const id = `M${uniq()}`;
    expect((await sync(op, "global", { ops: [put(row(id, text, { tags: ["stage:0"] }))] })).body.results[0].status).toBe("accepted");
    const before = (await get(rec("global", `/${id}`), op)).body;
    for (const [who, token, body] of [
      ["baseline", base, { type: "idea", summary: text, tags: ["planted-tag", "topic-x"] }],
      ["read/write", rw, { type: "memory", summary: text, tags: ["stage:3", "planted-tag"] }],
      ["read/write rule tags", rw, { type: "memory", summary: text, tags: ["project:planted", "stage:0"] }],
    ] as const) {
      const r = await create(token, "global", body);
      expect(r.status, who).toBe(200);
      expect(r.body.id, who).toBe(id);
      expect(r.body.tags, who).toEqual(["stage:0"]);
    }
    const after = (await get(rec("global", `/${id}`), op)).body;
    expect(after.tags).toEqual(["stage:0"]);
    expect(after.device).toBe(before.device);
    expect(after.rev).toBe(before.rev);
    expect(after.links).toEqual(before.links);
  });

  it("gives a writer that cannot read the holder a new separate record, and never touches the hidden one", async () => {
    const op = await operator();
    const base = await baselineToken();
    const text = `HIDDEN-HOLDER-${freshKey()}`;
    const held = await create(op, "global", { type: "memory", summary: text, tags: ["stage:1", "keep-me"] });
    const r = await create(base, "global", { type: "idea", summary: text, tags: ["planted-tag", "skill-free"] });
    expect(r.status).toBe(201);
    expect(r.body.id).not.toBe(held.body.id);
    expect(r.body.tags).toEqual(expect.arrayContaining(["idea", "planted-tag"]));
    const stored = (await get(rec("global", `/${held.body.id}`), op)).body;
    expect(stored.tags).toEqual(["keep-me", "stage:1"]);
    expect(stored.rev).toBe(held.body.rev);
    expect(stored.device).toBe(held.body.device);
  });

  it("still merges where the writer may edit the holder, but never a rank tag from a baseline device", async () => {
    const op = await operator();
    const base = await baselineToken();
    const text = `MINE-${freshKey()}`;
    const a = await create(base, "global", { type: "idea", summary: text, tags: ["first"] });
    const b = await create(base, "global", { type: "idea", summary: text, tags: ["second"] });
    expect(b.body.id).toBe(a.body.id);
    expect(b.body.tags).toEqual(expect.arrayContaining(["first", "second", "idea"]));
    // a task the operator ranked: the same text from a baseline device may add a tag but not move the rank
    const title = `TASK-${freshKey()}`;
    const ranked = await create(op, "global", { type: "human_task", title, tags: ["rank:3"] });
    const plain = await create(base, "global", { type: "human_task", title, tags: ["extra-tag"] });
    expect(plain.status, plain.text).toBe(200);
    expect(plain.body.id).toBe(ranked.body.id);
    expect(plain.body.tags).toEqual(expect.arrayContaining(["rank:3", "extra-tag"]));
    // asking for a rank is refused outright (it is not a way to move the operator's list)
    expect((await create(base, "global", { type: "human_task", title, tags: ["rank:1"] })).status).toBe(403);
    expect((await get(rec("global", `/${ranked.body.id}`), op)).body.tags).toContain("rank:3");
    expect((await get(rec("global", `/${ranked.body.id}`), op)).body.tags).not.toContain("rank:1");
  });

  it("refuses a merge that would give the holder two stage tags or two projects, as an edit would", async () => {
    const op = await operator();
    const text = `TWO-STAGES-${freshKey()}`;
    await create(op, "global", { type: "rule", summary: text, tags: ["stage:2"] });
    const r = await create(op, "global", { type: "rule", summary: text, tags: ["stage:4"] });
    expect(r.status).toBe(400);
    const mem = `TWO-PROJECTS-${freshKey()}`;
    await create(op, "global", { type: "idea", summary: mem, tags: ["project:one-key"] });
    expect((await create(op, "global", { type: "idea", summary: mem, tags: ["project:other-key"] })).status).toBe(400);
  });
});

describe("a baseline device cannot reach skills", () => {
  it("refuses a skill tag on any write and moves no revision, whether the skill exists or not", async () => {
    const op = await operator();
    const base = await baselineToken();
    const name = `secret-skill-${uniq()}`;
    expect((await sync(op, "global", { ops: [put(skillDoc(`D${uniq()}`, name, 1, "active", ["stage:1"], "SECRET-BODY"))] })).body.results[0].status).toBe("accepted");
    const before = await rev(op);
    for (const tags of [[`skill:${name}`], ["skill:no-such-skill-here"], ["skill"], [`Skill:${name}`]]) {
      const r = await create(base, "global", { type: "idea", summary: `skill try ${JSON.stringify(tags)}`, tags });
      expect(r.status, JSON.stringify(tags)).toBeGreaterThanOrEqual(400);
      expect(r.status).toBeLessThan(500);
    }
    expect((await create(base, "global", { type: "decision", summary: "named skill", tags: [`skill:${name}`] })).status).toBe(403);
    const idea = (await create(base, "global", { type: "idea", summary: `plain ${freshKey()}` })).body;
    const sofar = await rev(op);
    expect((await send("PATCH", rec("global", `/${idea.id}`), { token: base, body: { tags: ["idea", `skill:${name}`] } })).status).toBe(403);
    expect((await tool(base, "save", { wiki: "global", type: "idea", summary: "via mcp", tags: [`skill:${name}`] })).isError).toBe(true);
    expect((await tool(base, "update", { wiki: "global", id: idea.id, tags: ["idea", `skill:${name}`] })).isError).toBe(true);
    expect(await rev(op)).toBe(sofar); // none of the refusals wrote anything
    expect(sofar).toBeGreaterThan(before); // (the one plain idea did)
    expect((await get(`/v1/wikis/global/skills/${name}?version=2`, op)).status).toBe(404); // no pending version was filed
  });

  it("files no lesson from a baseline device's saved text, an identical-text save included", async () => {
    const op = await operator();
    const base = await baselineToken();
    const name = `lesson-skill-${uniq()}`;
    expect((await sync(op, "global", { ops: [put(skillDoc(`D${uniq()}`, name, 1, "active", [], "BODY"))] })).body.results[0].status).toBe("accepted");
    const text = `LESSON-TEXT-${freshKey()}`;
    // the operator's own idea names the skill: a lesson is filed (the existing rule), once
    const held = (await create(op, "global", { type: "idea", summary: text, tags: [`skill:${name}`] })).body;
    expect((await get(`/v1/wikis/global/skills/${name}?version=2`, op)).status).toBe(200);
    // a baseline device saving the same text can read that idea but not edit it: it gets it back unchanged, no lesson
    const again = await create(base, "global", { type: "idea", summary: text, tags: ["extra"] });
    expect(again.status).toBe(200);
    expect(again.body.id).toBe(held.id);
    expect(again.body.tags).toEqual(held.tags);
    expect((await get(`/v1/wikis/global/skills/${name}?version=3`, op)).status).toBe(404);
    // and a record of its own that matches nothing files none either
    expect((await create(base, "global", { type: "idea", summary: `${text} (own)` })).status).toBe(201);
    expect((await get(`/v1/wikis/global/skills/${name}?version=3`, op)).status).toBe(404);
  });
});

describe("a baseline device cannot detect hidden records through a project", () => {
  it("creating a project links only the records it can read and edit, and leaves the rest alone", async () => {
    const op = await operator();
    const base = await baselineToken();
    const key = `guess-${uniq()}`;
    const tag = `project:${key}`;
    const hiddenRule = (await create(op, "global", { type: "rule", summary: `HIDDEN-R ${key}`, tags: ["stage:1", tag] })).body;
    const hiddenMemory = (await create(op, "global", { type: "memory", summary: `HIDDEN-M ${key}`, tags: [tag] })).body;
    const s0 = `R${uniq()}`;
    expect((await sync(op, "global", { ops: [put(row(s0, `S0-RULE ${key}`, { tags: ["stage:0", tag] }))] })).body.results[0].status).toBe("accepted");
    const idea = (await create(op, "global", { type: "idea", summary: `IDEA ${key}`, project: key })).body;
    const task = (await create(op, "global", { type: "human_task", title: `TASK ${key}`, project: key })).body;
    const snap = async (id: string) => { const r = (await get(rec("global", `/${id}`), op)).body; return { rev: r.rev, device: r.device, links: r.links }; };
    const untouched = [hiddenRule.id, hiddenMemory.id, s0];
    const was = await Promise.all(untouched.map(snap));
    const before = await rev(op);
    const made = await send("PUT", `/v1/projects/${key}`, { token: base });
    expect(made.status).toBe(201);
    expect(await Promise.all(untouched.map(snap))).toEqual(was);
    // the file (one revision) and the idea and task it may edit (one each): nothing else moved
    expect(await rev(op)).toBe(before + 3);
    for (const id of [idea.id, task.id]) expect((await get(rec("global", `/${id}`), op)).body.links.length, id).toBe(1);
    // project reads resolve membership by tag, so the unlinked records still belong
    const full = (await get(`/v1/projects/${key}`, op)).body;
    expect(full.rules.map((r: { id: string }) => r.id).sort()).toEqual([hiddenRule.id, s0].sort());
    expect(full.memories.map((r: { id: string }) => r.id)).toEqual([hiddenMemory.id]);
    const seen = (await get(`/v1/projects/${key}`, base)).body;
    expect(seen.rules.map((r: { id: string }) => r.id)).toEqual([s0]);
    expect(seen.memories).toEqual([]);
    expect(JSON.stringify(seen)).not.toContain("HIDDEN");
    // a full device's next write of a record links it
    const patched = await send("PATCH", rec("global", `/${hiddenRule.id}`), { token: op, body: { tags: ["stage:1", tag, "touched"] } });
    expect(patched.body.links).toHaveLength(1);
  });
});

describe("lookalike tags", () => {
  const lookalikes = [
    "Human_Task", `agent${ZW}_task`, "ｈuman_task", "IDEA", "Decision", "Focus", "SKILL", "Skill:abc", "Project:abc", `proj${ZW}ect:abc`,
    "Rank:1", "RANK:2", "Status:active", "V:3", "Stage:1", "stage-Proposed:0", "ｓｋｉｌｌ:abc",
  ];

  it("refuses a lookalike of any system tag, on REST, MCP and sync, from every kind of device", async () => {
    const op = await operator();
    const base = await baselineToken();
    for (const token of [op, base]) {
      for (const tag of lookalikes) {
        const r = await create(token, "global", { type: "human_task", title: `look ${freshKey()}`, tags: ["human_task", tag] });
        expect(r.status, JSON.stringify(tag)).toBe(400);
        expect(r.body.error.message, JSON.stringify(tag)).toContain("imitates a system tag");
      }
    }
    const idea = (await create(base, "global", { type: "idea", summary: `look-idea ${freshKey()}` })).body;
    for (const tag of ["Human_Task", "IDEA", "Project:abc"]) {
      expect((await send("PATCH", rec("global", `/${idea.id}`), { token: base, body: { tags: ["idea", tag] } })).status, tag).toBe(400);
    }
    expect((await tool(base, "save", { wiki: "global", type: "human_task", title: "mcp look", tags: ["Human_Task"] })).isError).toBe(true);
    const sy = await sync(op, "global", { ops: [put({ ...row(`M${uniq()}`, "sync look"), tags: ["IDEA"] }), put({ ...row(`T${uniq()}`, "sync task"), kind: "T", data: { title: "sync task", body: "", parent: null, status: "open", closed_reason: null, git_ref: null, alias: null, claimed_by: null, closed: null }, tags: ["Human_Task"] })] });
    expect(sy.body.results.map((x: { status: string }) => x.status)).toEqual(["rejected", "rejected"]);
  });

  it("keeps accepting the exact system tags and ordinary topics", async () => {
    const op = await operator();
    const ok = await create(op, "global", { type: "human_task", title: `exact ${freshKey()}`, tags: ["human_task", "planning", "v-notes", "ideas", "projection", "stagecraft"] });
    expect(ok.status).toBe(201);
    expect((await create(op, "global", { type: "idea", summary: `exact idea ${freshKey()}`, tags: ["idea", "decisions"] })).status).toBe(201);
  });

  it("never lets a lookalike owner tag drop the operator's task on its way to the local engine", async () => {
    const base = await baselineToken();
    for (const tag of ["Human_Task", `agent${ZW}_task`]) {
      const r = await create(base, "global", { type: "human_task", title: `owner ${freshKey()}`, tags: [tag] });
      expect(r.status).toBe(400);
    }
    const good = await create(base, "global", { type: "human_task", title: `owner ${freshKey()}` });
    expect(good.status).toBe(201);
    expect(good.body.tags).toEqual(["human_task"]);
  });
});

describe("the about link follows the kinds the contract names", () => {
  it("links tasks, memories, ideas, decisions, rules, issues and skill versions, never a feature, a job or the file", async () => {
    const op = await operator();
    const w = freshKey();
    await wiki(op, w);
    const key = `kinds-${uniq()}`;
    const tag = `project:${key}`;
    const doc = (await send("PUT", `/v1/projects/${key}`, { token: op, body: { wiki: w } })).body.document.id as string;
    const made: Record<string, string> = {};
    for (const [name, body] of Object.entries({
      task: { type: "task", title: "kinds task", tags: [tag] },
      idea: { type: "idea", summary: "kinds idea", project: key },
      decision: { type: "decision", summary: "kinds decision", project: key },
      memory: { type: "memory", summary: "kinds memory", project: key },
      rule: { type: "rule", summary: "kinds rule", project: key },
      issue: { type: "issue", summary: "kinds issue", project: key },
    })) {
      const r = await create(op, w, body);
      expect(r.status, name).toBe(201);
      made[name] = r.body.id;
    }
    const feature = (await create(op, w, { type: "feature", title: "kinds feature", tags: [tag] })).body;
    const job = (await create(op, w, { type: "job", title: "kinds job", parent: feature.id, tags: [tag] })).body;
    const linked = async (id: string) => ((await get(rec(w, `/${id}`), op)).body.links as [string, string][]).some(([dst, rel]) => dst === doc && rel === "about");
    for (const [name, id] of Object.entries(made)) expect(await linked(id), name).toBe(true);
    expect(await linked(feature.id)).toBe(false);
    expect(await linked(job.id)).toBe(false);
    expect(await linked(doc)).toBe(false);
    // a skill version carrying the tag, put by sync, is linked too
    const sk = await sync(op, w, { ops: [put(skillDoc(`D${uniq()}`, `kinds-skill-${uniq()}`, 1, "pending", [tag], "BODY"))] });
    expect(sk.body.results[0].status).toBe("accepted");
    const skillId = sk.body.results[0].new_id ?? sk.body.results[0].id;
    expect(await linked(skillId)).toBe(true);
  });

  it("links them later too, when the project's file is created after the records", async () => {
    const op = await operator();
    const w = freshKey();
    await wiki(op, w);
    const key = `later-${uniq()}`;
    const tag = `project:${key}`;
    const issue = (await create(op, w, { type: "issue", summary: "later issue", project: key })).body;
    const rule = (await create(op, w, { type: "rule", summary: "later rule", project: key })).body;
    const feature = (await create(op, w, { type: "feature", title: "later feature", tags: [tag] })).body;
    const job = (await create(op, w, { type: "job", title: "later job", parent: feature.id, tags: [tag] })).body;
    const doc = (await send("PUT", `/v1/projects/${key}`, { token: op, body: { wiki: w } })).body.document.id as string;
    const linked = async (id: string) => ((await get(rec(w, `/${id}`), op)).body.links as [string, string][]).some(([dst]) => dst === doc);
    expect(await linked(issue.id)).toBe(true);
    expect(await linked(rule.id)).toBe(true);
    expect(await linked(feature.id)).toBe(false);
    expect(await linked(job.id)).toBe(false);
  });
});

describe("the two notes printed into every session", () => {
  const NOTES = ["session-instructions", "response-style"];

  async function ensureNotes(op: string): Promise<void> {
    for (const n of NOTES) {
      await create(op, "global", { type: "note", path: `${n}.md`, body: `${n} text` });
      await send("PATCH", rec("global", `/${n}`), { token: op, body: { body: `${n} text` } });
    }
  }

  it("changes only from a device token with the delete scope", async () => {
    const op = await operator();
    await ensureNotes(op);
    const rw = (await mint(`rw-${freshKey()}`, ["read", "write"])).token;
    for (const n of NOTES) {
      for (const ref of [n, `${n}.md`]) {
        expect((await send("PATCH", rec("global", `/${ref}`), { token: rw, body: { body: "planted" } })).status, `patch ${ref}`).toBe(403);
      }
      expect((await send("DELETE", rec("global", `/${n}`), { token: rw })).status).toBe(403);
      expect((await create(rw, "global", { type: "note", path: `arch/${n}.md`, body: "planted" })).status).toBe(403);
      expect((await create(rw, "global", { type: "document", title: "d", alias: n })).status).toBe(403);
      const synced = await sync(rw, "global", { ops: [put({ id: `${n}.md`, uid: "", kind: "N", created: "", updated: "2026-06-01T00:00:00Z", deleted: false, links: [], data: { body: "planted" }, tags: [] }, (await get(rec("global", `/${n}`), op)).body.rev)] });
      expect(synced.body.results[0].status, `sync ${n}`).toBe("rejected");
      expect(synced.body.results[0].error).toContain("delete scope");
      // the operator's own device token (read, write, delete) may
      expect((await send("PATCH", rec("global", `/${n}`), { token: op, body: { body: `${n} text` } })).status).toBe(200);
    }
    for (const n of NOTES) expect((await get(rec("global", `/${n}`), op)).body.data.body).toBe(`${n} text`);
  });

  it("masks their text when printed, and keeps their lines", async () => {
    const op = await operator();
    await ensureNotes(op);
    const secret = "sk-ant-api03-Q7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7x";
    const body = `First line\nsecret ${secret}\nbidi${String.fromCharCode(0x202e)}hidden${ZW}end\n\nLast line`;
    expect((await send("PATCH", rec("global", "/session-instructions"), { token: op, body: { body } })).status).toBe(200);
    expect((await send("PATCH", rec("global", "/response-style"), { token: op, body: { body: `style ${secret}\nsecond style line` } })).status).toBe(200);
    const text = (await get("/v1/instructions?client=mcp", op)).body.text as string;
    expect(text).toContain("OPERATOR'S SESSION INSTRUCTIONS\nFirst line\n");
    expect(text).toContain("\n\nLast line");
    expect(text).toContain("\nsecond style line");
    expect(text).not.toContain("sk-ant-api03");
    expect(text).not.toContain(String.fromCharCode(0x202e));
    expect(text).not.toContain(ZW);
    await ensureNotes(op); // the wiki outlives the test: put the canonical text back
  });
});

describe("the baseline reaches every authenticated device", () => {
  it("prints stage 0 for a device whose wiki list leaves out the global wiki, and nothing else of global", async () => {
    const op = await operator();
    const w = freshKey();
    await wiki(op, w);
    const marker = `LIMITED-S0-${freshKey()}`;
    const only = `GLOBAL-ONLY-TASK-${freshKey()}`;
    expect((await sync(op, "global", { ops: [put(row(`R${uniq()}`, marker, { tags: ["stage:0"] }))] })).body.results[0].status).toBe("accepted");
    await create(op, "global", { type: "human_task", title: only });
    const limited = (await mint(`lim-${freshKey()}`, ["read", "write"], [w])).token;
    const limitedBase = await baselineToken([w]);
    for (const [who, token] of [["full", limited], ["baseline", limitedBase]] as const) {
      for (const client of ["mcp", "rest"]) {
        const text = (await get(`/v1/instructions?client=${client}`, token)).body.text as string;
        expect(text, `${who} ${client}`).toContain(marker);
        expect(text, `${who} ${client}`).not.toContain(only);
      }
    }
    expect((await tool(limitedBase, "session_start", {})).text).toContain(marker);
  });

  it("asks for stage 9 and still gets only stage 0 when its wiki list leaves out the global wiki", async () => {
    const op = await operator();
    const w = freshKey();
    await wiki(op, w);
    const s0 = `LIM-S0-${freshKey()}`;
    const rule5 = `LIM-RULE5-${freshKey()}`;
    const skill4 = `lim-skill4-${freshKey()}`;
    const body4 = `LIM-SKILL4-BODY-${freshKey()}`;
    const mem3 = `LIM-MEM3-${freshKey()}`;
    expect((await sync(op, "global", { ops: [
      put(row(`R${uniq()}`, s0, { tags: ["stage:0"] })),
      put(row(`R${uniq()}`, rule5, { tags: ["stage:5"] })),
      put(row(`M${uniq()}`, mem3, { tags: ["stage:3"] })),
      put(skillDoc(`D${uniq()}`, skill4, 1, "active", ["stage:4"], body4)),
    ] })).body.results.map((r: { status: string }) => r.status)).toEqual(["accepted", "accepted", "accepted", "accepted"]);
    const limited = (await mint(`lim9-${freshKey()}`, ["read"], [w])).token;
    const withGlobal = (await mint(`glob9-${freshKey()}`, ["read"], ["global", w])).token;
    for (const client of ["mcp", "rest"]) {
      const text = (await get(`/v1/instructions?client=${client}&stage=9`, limited)).body.text as string;
      expect(text, client).toContain(s0);
      for (const above of [rule5, mem3, skill4, body4]) expect(text, `${client} ${above}`).not.toContain(above);
      // the threshold still works for a device that may read the global wiki
      const wide = (await get(`/v1/instructions?client=${client}&stage=9`, withGlobal)).body.text as string;
      for (const above of [rule5, mem3, skill4, body4]) expect(wide, `${client} wide ${above}`).toContain(above);
    }
    const mcp = (await tool(limited, "session_start", { stage: 9 })).text;
    expect(mcp).toContain(s0);
    for (const above of [rule5, mem3, skill4, body4]) expect(mcp, `session_start ${above}`).not.toContain(above);
  });

  it("tells a baseline device nothing of tools it may not use, even when no focus is set", async () => {
    const op = await operator();
    const w = freshKey();
    await wiki(op, w);
    const baseline = await baselineToken([w]);
    const full = (await mint(`full-${freshKey()}`, ["read", "write"], [w])).token;
    for (const client of ["mcp", "rest"]) {
      const text = (await get(`/v1/instructions?client=${client}&wiki=${w}`, baseline)).body.text as string;
      expect(text).toContain("NORTHSTAR AND FIRES\nNone are set.");
      for (const gone of ["focus_set", "/focus", "- rule:", "- memory:", "- issue:", "feature | job", "/sync"]) expect(text, `${client} ${gone}`).not.toContain(gone);
      const fullText = (await get(`/v1/instructions?client=${client}&wiki=${w}`, full)).body.text as string;
      expect(fullText).toMatch(/focus_set|PUT \/v1\/wikis\/\{wiki\}\/focus/);
    }
    expect((await tool(baseline, "session_start", { wiki: w })).text).not.toContain("focus_set");
  });
});

describe("listing by tag reads the relation index", () => {
  it("answers from record_tags, not a scan of every record's tags", async () => {
    const stub = env.STORE.getByName("main");
    const statements = await runInDurableObject(stub, (_i, state) => {
      const seen: string[] = [];
      const sql = new Proxy(state.storage.sql, {
        get(target, prop) {
          if (prop === "exec") return (q: string, ...args: unknown[]) => { seen.push(q); return (target.exec as (q: string, ...a: unknown[]) => unknown)(q, ...args); };
          const v = (target as unknown as Record<string | symbol, unknown>)[prop];
          return typeof v === "function" ? (v as (...a: unknown[]) => unknown).bind(target) : v;
        },
      }) as unknown as SqlStorage;
      new Repo(sql).list("global", { tag: "anything", after: 0, limit: 5 });
      new Repo(sql, "baseline").list("global", { tag: "anything", kind: "M", after: 0, limit: 5 });
      return seen;
    });
    const all = statements.join("\n");
    expect(all).toContain("record_tags");
    expect(all).not.toContain("json_each");
  });

  it("pages, filters and hides exactly as the unindexed listing did", async () => {
    const op = await operator();
    const base = await baselineToken();
    const w = freshKey();
    await wiki(op, w);
    const tag = `listed-${uniq()}`;
    const ids: string[] = [];
    for (let i = 0; i < 5; i++) ids.push((await create(op, w, { type: "idea", summary: `listed idea ${i}`, tags: [tag] })).body.id);
    const rule = (await create(op, w, { type: "rule", summary: "listed hidden rule", tags: [tag] })).body.id;
    await create(op, w, { type: "idea", summary: "listed other", tags: ["not-this-one"] });
    const first = (await get(rec(w, `?tag=${tag}&limit=3`), op)).body;
    expect(first.records.map((r: { id: string }) => r.id)).toEqual(ids.slice(0, 3));
    const second = (await get(rec(w, `?tag=${tag}&limit=3&after=${first.next}`), op)).body;
    expect(second.records.map((r: { id: string }) => r.id)).toEqual([...ids.slice(3), rule]);
    expect((await get(rec(w, `?tag=${tag}&kind=R`), op)).body.records.map((r: { id: string }) => r.id)).toEqual([rule]);
    const seen = (await get(rec(w, `?tag=${tag}`), base)).body.records.map((r: { id: string }) => r.id);
    expect(seen).toEqual(ids); // the rule is hidden from a baseline device
    expect((await get(rec(w, "?tag=no-such-tag-anywhere"), op)).body.records).toEqual([]);
    // a record that loses the tag leaves the listing
    await send("PATCH", rec(w, `/${ids[0]}`), { token: op, body: { tags: ["idea"] } });
    expect((await get(rec(w, `?tag=${tag}`), op)).body.records.map((r: { id: string }) => r.id)).toEqual([...ids.slice(1), rule]);
  });
});
