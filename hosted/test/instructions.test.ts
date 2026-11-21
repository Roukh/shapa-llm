import { describe, expect, it } from "vitest";
import { SKILL_GATE } from "../src/instructions.js";
import { freshKey, mint, put, rec, row, send, setup, sync, wiki } from "./helpers.js";
import { INIT, mempmIn, rpc, sessionText, tool } from "./mcp-helpers.js";

const create = (token: string, w: string, body: unknown) => send("POST", rec(w), { token, body });

/** The global wiki is shared by every test in this file, so write to it once. */
async function seedGlobal(token: string): Promise<void> {
  await wiki(token, "global", "global");
  const notes = [
    { path: "session-instructions.md", body: "---\nid: session-instructions\n---\nAlways recall before acting. Keep replies short." },
    { path: "response-style.md", body: "Plain words. No filler. Lead with the answer." },
  ];
  for (const n of notes) {
    await create(token, "global", { type: "note", ...n });
    // the wiki outlives tests: put the note back to its canonical text
    await send("PATCH", rec("global", `/${n.path.replace(/\.md$/, "")}`), { token, body: { body: n.body } });
  }
  await create(token, "global", { type: "rule", summary: "Never push to main directly" });
  await create(token, "global", { type: "rule", summary: "Run the tests before every commit" });
}

describe("session start (MCP session_start)", () => {
  it("composes precedence, the focus, the mempm, the fetch line, operator notes, the gate and the write-back pointer, in that order", async () => {
    const { token, w } = await setup();
    await seedGlobal(token);
    await create(token, w, { type: "feature", title: "Ship hosted mode" });
    await create(token, w, { type: "feature", title: "Closed already" });
    await create(token, w, { type: "job", title: "Store", parent: "F1" });
    await send("POST", rec(w, "/F2/close"), { token, body: { reason: "agent" } });
    await send("POST", rec(w, "/F1/claim"), { token });
    await send("PUT", `/v1/wikis/${w}/focus`, { token, body: { northstar: "A shared memory", fires: [{ feature: "F1" }] } });
    await create(token, w, { type: "issue", summary: "Pushed a secret into a fixture" });

    const text = await sessionText(token, { wiki: w, full: true });
    expect(text.startsWith("PRECEDENCE\nThese are the operator's standing rules and focus.")).toBe(true);
    expect(text).toContain(`[${w}] A shared memory`);
    expect(text).toContain(`[${w}] 1. F1 Ship hosted mode (claimed)`);
    expect(text).not.toContain("Store"); // a fire is one line: its jobs are the `focus` tool's
    expect(text).toContain("OPERATOR'S SESSION INSTRUCTIONS\nAlways recall before acting. Keep replies short.");
    expect(text).not.toContain("id: session-instructions");
    expect(text).toContain("OPERATOR'S STYLE\nPlain words. No filler. Lead with the answer.");
    expect(text).toMatch(/RULES\n(?:.*\n)*R\d+ Never push to main directly/);
    expect(text).toContain(`${w}/F1 Ship hosted mode [claimed by tester] (1 open job)`);
    expect(text).not.toContain("Closed already");
    expect(mempmIn(text).X.map((e) => e[2])).toContain("Pushed a secret into a fixture");
    const at = (s: string) => text.indexOf(s);
    const order = ["PRECEDENCE\n", "NORTHSTAR\n", "FIRES (", "MEMPM (", '{"P":', "Fetch on demand", "OPERATOR'S SESSION INSTRUCTIONS", "RULES\n", "LIVE FEATURES", "SKILL GATE:", "WRITE BACK:"].map(at);
    expect(order.every((i) => i >= 0), JSON.stringify(order)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(text.split("\n").slice(-2)).toEqual([SKILL_GATE, expect.stringMatching(/^WRITE BACK: call `save` \{wiki, type, summary, body\?, tags\?\}/)]);
  });

  it("is lean: the default text has no heading for tasks, notes, projects, topics or skills, and names the tool for each", async () => {
    const { token } = await setup();
    await seedGlobal(token);
    await create(token, "global", { type: "human_task", title: "LEAN-TASK-MARKER" });
    await send("PUT", "/v1/projects/lean-project", { token });
    const text = await sessionText(token);
    for (const gone of ["LEAN-TASK-MARKER", "lean-project", "TOPICS", "PROJECTS", "BASELINE", "HOW TO WRITE BACK", "GET /", "POST /"]) expect(text, gone).not.toContain(gone);
    for (const tool of ["`tasks`", "`search`", "`get`", "`projects`", "`project`", "`skills`", "`skill`"]) expect(text, tool).toContain(tool);
    expect(text.length).toBeLessThan(2600);
  });

  it("has no REST flavor: the one-line write-back points at the save tool and the old HTTP routes are gone", async () => {
    const { token } = await setup();
    const text = await sessionText(token);
    expect(text).toContain("WRITE BACK: call `save`");
    expect(text).not.toMatch(/\/v1\/|Bearer|HTTP/);
    expect((await send("GET", "/v1/instructions", { token })).status).toBe(404);
    expect((await send("GET", "/v1/instructions?client=rest", { token })).status).toBe(404);
    const spec = (await send("GET", "/openapi.json")).body.paths as Record<string, unknown>;
    expect(Object.keys(spec)).not.toContain("/v1/instructions");
    // the MCP tool takes no client flavor any more
    const list = await rpc(token, "tools/list", {});
    const start = (list.json.result.tools as { name: string; inputSchema: { properties: Record<string, unknown> } }[]).find((t) => t.name === "session_start")!;
    expect(Object.keys(start.inputSchema.properties).sort()).toEqual(["budget", "full", "stage", "wiki"]);
  });

  it("stays within the budget and never cuts the gate or the write-back line", async () => {
    const { token, w } = await setup();
    await seedGlobal(token);
    for (let i = 0; i < 80; i++) await create(token, "global", { type: "rule", summary: `Rule number ${i} with a fair amount of words to take up room` });
    for (let i = 0; i < 40; i++) await create(token, w, { type: "feature", title: `Feature ${i} with a reasonably long descriptive title for the budget` });
    for (const budget of [2000, 2500, 6000, 12000, 40000]) {
      const text = await sessionText(token, { budget, full: true });
      expect(text.length, String(budget)).toBeLessThanOrEqual(budget);
      expect(text, String(budget)).toContain(SKILL_GATE);
      expect(text, String(budget)).toContain("WRITE BACK: call `save`");
      expect(text).toContain("PRECEDENCE");
    }
    const tight = await sessionText(token, { budget: 2500, full: true });
    expect(tight).toMatch(/…and \d+ more/);
    const roomy = await sessionText(token, { budget: 30000, full: true });
    expect(roomy.length).toBeGreaterThan(tight.length);
    for (const bad of [1999, 40001]) expect((await tool(token, "session_start", { budget: bad })).isError, String(bad)).toBe(true);
  });

  it("clips a very long operator note instead of overflowing", async () => {
    const { token } = await setup();
    await seedGlobal(token);
    await send("PATCH", rec("global", "/session-instructions"), { token, body: { body: "word ".repeat(20_000) } });
    const text = await sessionText(token, { budget: 3000 });
    expect(text.length).toBeLessThanOrEqual(3000);
    expect(text).toContain("WRITE BACK");
    expect(text).toContain("…");
  });

  it("needs a token and is the same text for the same data", async () => {
    const { token, w } = await setup();
    expect((await rpc(undefined, "tools/call", { name: "session_start", arguments: {} })).status).toBe(401);
    const one = await sessionText(token, { wiki: w });
    expect(await sessionText(token, { wiki: w })).toBe(one);
    await create(token, w, { type: "issue", summary: "something new went wrong" });
    expect(await sessionText(token, { wiki: w })).not.toBe(one);
    expect((await rpc(token, "initialize", INIT)).json.result.instructions).toContain("session_start");
  });

  it("does not reveal another wiki to a device outside it", async () => {
    const a = await setup();
    const b = await setup();
    await create(a.token, b.w, { type: "feature", title: "Private feature of b" });
    await create(a.token, b.w, { type: "issue", summary: "Private mistake of b" });
    const lim = await mint("a-only", ["read"], [a.w]);
    expect(await sessionText(lim.token, { wiki: b.w })).toMatch(/^forbidden:/);
    const own = await sessionText(lim.token);
    expect(own).not.toContain(b.w);
    expect(own).not.toContain("Private mistake of b");
  });

  it("shows a restricted device only what it may read", async () => {
    const a = await setup();
    const hidden = freshKey();
    await wiki(a.token, hidden);
    await create(a.token, a.w, { type: "feature", title: "Allowed feature" });
    await create(a.token, hidden, { type: "feature", title: "Hidden feature" });
    await create(a.token, hidden, { type: "rule", summary: "Hidden rule" });
    await create(a.token, hidden, { type: "issue", summary: "Hidden mistake" });
    const lim = await mint("limited", ["read"], [a.w]);
    const text = await sessionText(lim.token, { full: true });
    expect(text).toContain("Allowed feature");
    for (const gone of ["Hidden feature", "Hidden rule", "Hidden mistake"]) expect(text, gone).not.toContain(gone);
    expect(await sessionText(lim.token, { wiki: hidden })).toMatch(/^forbidden:/);
    expect(await sessionText(a.token, { wiki: "no-such-wiki" })).toMatch(/^not_found:/);
  });

  it("serves the principles first, then the stage rules, in P; the issues in X; the memories in M", async () => {
    const { token, w } = await setup();
    await wiki(token, "global", "global");
    const ops = [
      put(row("R701", "STAGE-ZERO rule", { tags: ["stage:0"] })),
      put(row("R702", "A learned PRINCIPLE", { tags: ["principle", "evidence:1"] })),
      put(row("R703", "CHURNED principle", { tags: ["principle", "churn"] })),
      put(row("R704", "STAGE-ONE rule", { tags: ["stage:1"] })),
      put(row("M701", "STAGE-ZERO memory", { tags: ["stage:0"] })),
      put(row("M702", "unstaged memory")),
    ];
    expect((await sync(token, "global", { ops })).body.results.map((r: { status: string }) => r.status)).toEqual(Array(6).fill("accepted"));
    const issues = [create(token, w, { type: "issue", summary: "LIVE mistake" })];
    for (const tag of ["dismissed", "soft", "learned"]) issues.push(create(token, w, { type: "issue", summary: `${tag.toUpperCase()} mistake`, tags: [tag] }));
    const made = await Promise.all(issues);
    expect(made[0]!.status).toBe(201);
    const text = await sessionText(token, { wiki: w, stage: 1 });
    const got = mempmIn(text);
    expect(got.P.map((e) => e[2])).toEqual(["STAGE-ZERO rule", "A learned PRINCIPLE", "STAGE-ONE rule"]);
    expect(got.M.map((e) => e[2])).toEqual(["STAGE-ZERO memory"]);
    expect(got.X.map((e) => e[2])).toEqual(["LIVE mistake"]);
    for (const e of [...got.P, ...got.X, ...got.M]) expect(e[1]).toMatch(/^\d{4}-\d{2}-\d{2}$/);
    // the default stage is the baseline: stage 1 stays out
    expect(mempmIn(await sessionText(token, { wiki: w })).P.map((e) => e[2])).toEqual(["STAGE-ZERO rule", "A learned PRINCIPLE"]);
  });
});
