import { describe, expect, it } from "vitest";
import { PROJECT_TEMPLATE } from "../src/projectfile.js";
import { freshKey, get, mint, put, rec, row, send, setup, sync, wiki } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const projectsPath = (key: string, tail = "") => `/v1/projects/${encodeURIComponent(key)}${tail}`;
const key = () => `p-${freshKey()}`.toLowerCase();

// The contract's template, typed out once more here (each empty item is "1. " with its trailing space).
const TEMPLATE =
  "Issues:\n1. \n\nQuestions:\n1. \n\nImplementations:\n1. \n\n---\n\n### Architecture:\n\nProblem:\n1. \n\nSolution:\n1. \n\nDiff:\n1. \n\n" +
  "advantages:\n1. \n\nvillains:\n1. \n\nloop_market:\n1. \n\nloop_product:\n1. \n";

describe("the global wiki holds tasks only", () => {
  it("refuses features and jobs, a plain task, a task with a parent and a task with both owners", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    expect((await create(token, "global", { type: "feature", title: "bound to a branch" })).status).toBe(400);
    expect((await create(token, "global", { type: "job", title: "bound to a commit" })).status).toBe(400);
    expect((await create(token, "global", { type: "task", title: "who owns it" })).status).toBe(400);
    expect((await create(token, "global", { type: "task", title: "x", tags: ["human_task", "agent_task"] })).status).toBe(400);
    const ok = await create(token, "global", { type: "human_task", title: "standalone human task" });
    expect(ok.status).toBe(201);
    expect(ok.body.tags).toEqual(["human_task"]);
    expect(ok.body.kind).toBe("T");
    expect((await create(token, "global", { type: "agent_task", title: "standalone agent task" })).body.tags).toEqual(["agent_task"]);
    expect((await create(token, "global", { type: "task", title: "tagged by hand", tags: ["agent_task"] })).status).toBe(201);
    expect((await tool(token, "ledger_add", { wiki: "global", kind: "feature", title: "f" })).isError).toBe(true);
    expect((await tool(token, "ledger_add", { wiki: "global", kind: "human_task", title: "via ledger_add" })).isError).toBe(false);
    expect((await tool(token, "save", { wiki: "global", type: "agent_task", summary: "via save" })).isError).toBe(false);
    // sync obeys the same rules
    const r = await sync(token, "global", { ops: [put({ ...row("F1", "x"), kind: "F", data: { title: "f", body: "", parent: null, status: "open", closed_reason: null, git_ref: null, alias: null, claimed_by: null, closed: null } }), put({ ...row("T1", "x"), kind: "T", data: { title: "no owner", body: "", parent: null, status: "open", closed_reason: null, git_ref: null, alias: null, claimed_by: null, closed: null } })] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["rejected", "rejected"]);
  });

  it("lets a repo wiki use the aliases too, and keeps the owner tags to tasks", async () => {
    const { token, w } = await setup();
    const t = await create(token, w, { type: "human_task", title: "repo human task" });
    expect(t.body.tags).toEqual(["human_task"]);
    expect((await create(token, w, { type: "memory", summary: "m", tags: ["human_task"] })).status).toBe(400);
    expect((await create(token, w, { type: "feature", title: "still fine in a repo wiki" })).status).toBe(201);
  });

  it("stores identical open task text once, and a new one after it is closed", async () => {
    const { token, w } = await setup();
    const a = await create(token, w, { type: "human_task", title: "Renew the licence", body: "before March" });
    expect(a.status).toBe(201);
    const again = await create(token, w, { type: "human_task", title: "Renew the licence", body: "before March" });
    expect(again.status).toBe(200);
    expect(again.body.id).toBe(a.body.id);
    expect((await create(token, w, { type: "agent_task", title: "Renew the licence", body: "before March" })).status).toBe(201); // another owner
    expect((await create(token, w, { type: "human_task", title: "Renew the licence", body: "different" })).status).toBe(201);
    await send("POST", rec(w, `/${a.body.id}/close`), { token, body: { reason: "agent" } });
    expect((await create(token, w, { type: "human_task", title: "Renew the licence", body: "before March" })).status).toBe(201);
    // through MCP too
    const x = await tool(token, "save", { wiki: w, type: "human_task", summary: "Pay the invoice" });
    const y = await tool(token, "save", { wiki: w, type: "human_task", summary: "Pay the invoice" });
    expect(x.text).toContain("Saved");
    expect(y.text).toContain("Already saved");
  });
});

describe("the operator's universal task list", () => {
  it("lists open human tasks of every readable wiki: rank first, then oldest, with a total and a limit", async () => {
    const { token, w } = await setup();
    const other = freshKey();
    await wiki(token, other);
    const lim = await mint("scoped", ["read", "write"], [w, other]);
    await create(token, w, { type: "human_task", title: "first" });
    await create(token, w, { type: "human_task", title: "second ranked 2", tags: ["rank:2"] });
    await create(token, other, { type: "human_task", title: "third in another wiki" });
    await create(token, w, { type: "human_task", title: "fourth ranked 1", tags: ["rank:1"] });
    await create(token, w, { type: "agent_task", title: "an agent's" });
    await create(token, w, { type: "human_task", title: "done", });
    await send("POST", rec(w, "/T5/close"), { token, body: { reason: "agent" } });
    const r = await get("/v1/tasks", lim.token);
    expect(r.status).toBe(200);
    expect(r.body.tasks.map((t: { title: string }) => t.title)).toEqual(["fourth ranked 1", "second ranked 2", "first", "third in another wiki"]);
    expect(r.body.total).toBe(4);
    expect(r.body.tasks[0]).toMatchObject({ wiki: w, id: "T3", owner: "human", status: "open", rank: 1, project: "" });
    expect(r.body.tasks[3]).toMatchObject({ wiki: other, rank: null });
    expect((await get(`/v1/tasks?wiki=${other}`, lim.token)).body.tasks).toHaveLength(1);
    expect((await get("/v1/tasks?limit=2", lim.token)).body).toMatchObject({ total: 4 });
    expect((await get("/v1/tasks?limit=2", lim.token)).body.tasks).toHaveLength(2);
    expect((await get("/v1/tasks?owner=agent", lim.token)).body.tasks.map((t: { title: string }) => t.title)).toEqual(["an agent's"]);
    expect((await get("/v1/tasks?status=closed", lim.token)).body.tasks.map((t: { title: string }) => t.title)).toEqual(["done"]);
    expect((await get("/v1/tasks?status=all", lim.token)).body.total).toBe(5);
    expect((await get("/v1/tasks?limit=101", lim.token)).status).toBe(400);
    expect((await get("/v1/tasks?owner=nobody", lim.token)).status).toBe(400);
    expect((await get("/v1/tasks?project=Bad%20Key", lim.token)).status).toBe(400);
    expect((await get("/v1/tasks")).status).toBe(401);
    const viaMcp = await tool(lim.token, "tasks", {});
    expect(viaMcp.text).toContain("4 open human tasks");
    expect(viaMcp.text).toContain(`${w}/T3 fourth ranked 1`);
  });

  it("narrows to a project and counts a claimed task as open", async () => {
    const { token, w } = await setup();
    const p = key();
    await create(token, w, { type: "human_task", title: "in project", project: p });
    await create(token, w, { type: "human_task", title: "not in project" });
    await send("POST", rec(w, "/T1/claim"), { token });
    const r = await get(`/v1/tasks?project=${encodeURIComponent(p)}&wiki=${w}`, token);
    expect(r.body.tasks.map((t: { title: string }) => t.title)).toEqual(["in project"]);
    expect(r.body.tasks[0]).toMatchObject({ project: p, status: "claimed" });
    expect(r.body.total).toBe(1);
  });

  it("puts the operator's open tasks in the session text with the fixed sentence", async () => {
    const { token, w } = await setup();
    const marker = freshKey();
    await create(token, w, { type: "human_task", title: `Call the notary ${marker}` });
    const t = (await get(`/v1/instructions?wiki=${w}`, token)).body.text as string;
    expect(t).toMatch(/The operator's open tasks: \d+\n(?: {2}.*\n)*? {2}[\w./-]*T1 Call the notary/);
    expect(t).toContain("When the operator asks what they need to do, answer from `tasks` (GET /v1/tasks?owner=human). When they mention something they must do, save it as a human_task.");
  });
});

describe("project files", () => {
  it("holds the template byte for byte", () => {
    expect(PROJECT_TEMPLATE).toBe(TEMPLATE);
  });

  it("creates one from the template, idempotently, in the global wiki by default", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const k = key();
    const made = await send("PUT", projectsPath(k), { token, body: { title: "Alpha project" } });
    expect(made.status).toBe(201);
    expect(made.body).toMatchObject({ key: k, wiki: "global", title: "Alpha project" });
    expect(Object.keys(made.body.document.sections)).toEqual(["issues", "questions", "implementations", "problem", "solution", "diff", "advantages", "villains", "loop_market", "loop_product"]);
    const doc = (await get(rec("global", `/${made.body.document.id}`), token)).body;
    expect(doc.data.body).toBe(TEMPLATE);
    expect(doc.tags).toEqual([`project:${k}`]);
    const again = await send("PUT", projectsPath(k), { token, body: { title: "Renamed" } });
    expect(again.status).toBe(200);
    expect(again.body.document.id).toBe(made.body.document.id);
    expect(again.body.title).toBe("Alpha project");
    expect((await send("PUT", projectsPath(k), { token })).status).toBe(200); // the body is optional
    // another wiki has its own
    const w = freshKey();
    await wiki(token, w);
    expect((await send("PUT", projectsPath(k), { token, body: { wiki: w } })).status).toBe(201);
    expect((await send("PUT", projectsPath(k), { token, body: { wiki: "no-such-wiki" } })).status).toBe(404);
  });

  it("refuses malformed keys, with a slash allowed", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    for (const bad of ["Upper", "has space", "../up", "-lead", "x".repeat(73), "a;b", "a%00b", "\u0000"]) {
      const r = await send("PUT", projectsPath(bad), { token });
      expect(r.status, JSON.stringify(bad)).toBe(400);
    }
    expect((await send("PUT", projectsPath(`group/${key()}`), { token })).status).toBe(201);
    expect((await get(projectsPath("x".repeat(200)), token)).status).toBe(400);
    expect((await tool(token, "project_new", { key: "Bad Key" })).isError).toBe(true);
  });

  it("appends numbered items: fills the empty one first, numbers on, is a no-op for the same text", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const k = key();
    await send("PUT", projectsPath(k), { token });
    const add = (section: string, text: string) => send("POST", projectsPath(k, "/items"), { token, body: { section, text } });
    const first = await add("questions", "Which store holds it?");
    expect(first.status).toBe(201);
    expect(first.body).toMatchObject({ section: "questions", n: 1, added: true });
    expect((await add("questions", "Who signs off?")).body.n).toBe(2);
    const same = await add("questions", "Which store holds it?");
    expect(same.status).toBe(200);
    expect(same.body).toMatchObject({ n: 1, added: false });
    expect((await add("villains", "A weak spot")).body.n).toBe(1);
    const shown = (await get(projectsPath(k), token)).body;
    expect(shown.document.sections.questions).toEqual(["Which store holds it?", "Who signs off?"]);
    expect(shown.document.sections.villains).toEqual(["A weak spot"]);
    expect(shown.document.sections.issues).toEqual([]);
    const body = (await get(rec("global", `/${shown.document.id}`), token)).body.data.body as string;
    expect(body).toContain("Questions:\n1. Which store holds it?\n2. Who signs off?\n\nImplementations:");
    expect(body.endsWith("\n")).toBe(true);
  });

  it("creates a missing section heading at its template position", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const k = key();
    const made = await send("PUT", projectsPath(k), { token });
    const id = made.body.document.id as string;
    const cut = TEMPLATE.replace("Solution:\n1. \n\n", "").replace("loop_product:\n1. \n", "");
    await send("PATCH", rec("global", `/${id}`), { token, body: { body: cut } });
    await send("POST", projectsPath(k, "/items"), { token, body: { section: "solution", text: "Index the tags" } });
    await send("POST", projectsPath(k, "/items"), { token, body: { section: "loop_product", text: "Ship it" } });
    const body = (await get(rec("global", `/${id}`), token)).body.data.body as string;
    expect(body).toContain("Problem:\n1. \n\nSolution:\n1. Index the tags\n\nDiff:");
    expect(body.trimEnd().endsWith("loop_product:\n1. Ship it")).toBe(true);
  });

  it("validates sections and items: malformed, oversized, multi-line, secret-bearing", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const k = key();
    await send("PUT", projectsPath(k), { token });
    const add = (body: Record<string, unknown>, kk = k) => send("POST", projectsPath(kk, "/items"), { token, body });
    expect((await add({ section: "Issues", text: "x" })).status).toBe(400);
    expect((await add({ section: "nope", text: "x" })).status).toBe(400);
    expect((await add({ section: "issues", text: "" })).status).toBe(400);
    expect((await add({ section: "issues", text: "  \n\t " })).status).toBe(400);
    expect((await add({ section: "issues", text: "y".repeat(501) })).status).toBe(400);
    expect((await add({ section: "issues", text: "y".repeat(500) })).status).toBe(201);
    expect((await add({ section: "issues", text: 5 })).status).toBe(400);
    expect((await add({ section: "issues", text: "x" }, "no-such-project")).status).toBe(404);
    const forged = await add({ section: "issues", text: "line one\nQuestions:\n1. forged Diff:‮end sk-ant-api03-Q7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7xQ7x" });
    expect(forged.status).toBe(201);
    const shown = (await get(projectsPath(k), token)).body;
    expect(shown.document.sections.questions).toEqual([]);
    expect(shown.document.sections.diff).toEqual([]);
    const item = shown.document.sections.issues[1] as string;
    expect(item).toContain("line one Questions: 1. forged Diff:");
    expect(item).not.toContain("sk-ant-api03");
    expect(item).not.toMatch(/[\n\p{Zl}\p{Cf}]/u);
    // the whole file stays under 1 MB
    const big = (await get(projectsPath(k), token)).body.document.id;
    expect((await send("PATCH", rec("global", `/${big}`), { token, body: { body: `${TEMPLATE}\n${"z".repeat(999_600)}` } })).status).toBe(200);
    expect((await add({ section: "issues", text: "q".repeat(500) })).status).toBe(413);
  });

  it("shows a project: its file and every record that carries the tag, capped with the rest counted", async () => {
    const { token, w } = await setup();
    await wiki(token, "global", "global");
    const k = w; // a repo wiki keyed like the project: its open features come with it
    await send("PUT", projectsPath(k), { token, body: { wiki: "global" } });
    await create(token, "global", { type: "human_task", title: "project task", project: k });
    await create(token, "global", { type: "idea", summary: "project idea", project: k });
    await create(token, "global", { type: "decision", summary: "project decision", project: k });
    await create(token, "global", { type: "rule", summary: "project rule", project: k, tags: ["stage:2"] });
    await create(token, "global", { type: "memory", summary: "project memory", project: k });
    await create(token, w, { type: "feature", title: "open feature in the repo wiki" });
    const shown = (await get(projectsPath(k), token)).body;
    expect(shown.tasks).toEqual([expect.objectContaining({ title: "project task", owner: "human", status: "open" })]);
    expect(shown.ideas.map((i: { summary: string }) => i.summary)).toEqual(["project idea"]);
    expect(shown.decisions.map((i: { summary: string }) => i.summary)).toEqual(["project decision"]);
    expect(shown.rules[0]).toMatchObject({ summary: "project rule", stage: 2 });
    expect(shown.memories.map((i: { summary: string }) => i.summary)).toEqual(["project memory"]);
    expect(shown.features).toEqual([{ id: "F1", title: "open feature in the repo wiki" }]);
    expect(shown.more).toMatchObject({ tasks: 0, features: 0 });
    // more than 50 of a kind: 50 shown, the rest counted
    const ops = Array.from({ length: 60 }, (_, i) => put(row(`M${i + 100}`, `bulk memory ${i}`, { tags: [`project:${k}`] })));
    for (let i = 0; i < 60; i += 30) await sync(token, "global", { ops: ops.slice(i, i + 30) });
    const big = (await get(projectsPath(k), token)).body;
    expect(big.memories).toHaveLength(50);
    expect(big.more.memories).toBe(11);
    expect((await get(projectsPath(key()), token)).status).toBe(404);
    expect((await tool(token, "project", { key: k })).text).toContain("project task");
  });

  it("lists projects with their open tasks, the busiest first", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const a = key();
    const b = key();
    await send("PUT", projectsPath(a), { token, body: { title: "Quiet" } });
    await send("PUT", projectsPath(b), { token, body: { title: "Busy" } });
    await create(token, "global", { type: "human_task", title: "t1", project: b });
    await create(token, "global", { type: "human_task", title: "t2", project: b });
    const r = (await get("/v1/projects", token)).body;
    const rows = r.projects as { key: string; title: string; open_tasks: number }[];
    expect(rows.find((p) => p.key === b)).toEqual({ key: b, title: "Busy", open_tasks: 2 });
    expect(rows.find((p) => p.key === a)).toEqual({ key: a, title: "Quiet", open_tasks: 0 });
    expect(rows.findIndex((p) => p.key === b)).toBeLessThan(rows.findIndex((p) => p.key === a));
    expect(r.total).toBeGreaterThanOrEqual(2);
    expect((await tool(token, "projects", {})).text).toContain(b);
    // closing a task lowers the count
    await send("POST", rec("global", "/T1/close"), { token, body: { reason: "agent" } });
  });
});

describe("project spam and the API description", () => {
  it("caps the projects of a wiki at 1,000 and still lets a known one be read", async () => {
    const { token, w } = await setup();
    for (let b = 0; b < 5; b++) {
      const ops = Array.from({ length: 200 }, (_, i) => put({ ...row(`M${b * 200 + i + 1}`, `spam ${b}-${i}`), tags: [`project:spam-${b * 200 + i}`] }));
      expect((await sync(token, w, { ops })).body.results.every((r: { status: string }) => r.status === "accepted")).toBe(true);
    }
    const full = await send("PUT", projectsPath("one-too-many"), { token, body: { wiki: w } });
    expect(full.status).toBe(413);
    expect(full.body.error.message).toContain("at most 1000 projects");
    expect((await get(projectsPath("spam-7") + `?wiki=${w}`, token)).status).toBe(200);
    expect((await get(`/v1/projects?wiki=${w}`, token)).body.projects.length).toBeLessThanOrEqual(200);
  });

  it("describes every new route in /openapi.json", async () => {
    const spec = (await send("GET", "/openapi.json")).body.paths as Record<string, Record<string, unknown>>;
    const has = (path: string, method: string) => Object.keys(spec[path] ?? {}).includes(method);
    for (const [path, method] of [
      ["/v1/tags", "get"], ["/v1/tasks", "get"], ["/v1/projects", "get"], ["/v1/projects/{key}", "get"], ["/v1/projects/{key}", "put"],
      ["/v1/projects/{key}/items", "post"], ["/v1/wikis/{wiki}/records/{id}/related", "get"], ["/v1/search", "get"], ["/v1/instructions", "get"],
    ] as const) expect(has(path, method), `${method} ${path}`).toBe(true);
    const params = (spec["/v1/search"]!["get"] as { parameters: { name: string }[] }).parameters.map((p) => p.name);
    expect(params).toEqual(expect.arrayContaining(["q", "tags", "k", "wiki", "types"]));
    const inst = (spec["/v1/instructions"]!["get"] as { parameters: { name: string }[] }).parameters.map((p) => p.name);
    expect(inst).toContain("stage");
  });
});

describe("a task, idea or rule is linked to its project's file by an about link", () => {
  const linksOf = async (token: string, w: string, id: string) => (await get(rec(w, `/${id}`), token)).body.links as [string, string][];

  it("links on create, moves on retag, and links the records made before the file", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const a = key();
    const b = key();
    const early = await create(token, "global", { type: "human_task", title: "made before the file", project: a });
    expect(await linksOf(token, "global", early.body.id)).toEqual([]);
    const fa = (await send("PUT", projectsPath(a), { token })).body.document.id;
    const fb = (await send("PUT", projectsPath(b), { token })).body.document.id;
    expect(await linksOf(token, "global", early.body.id)).toEqual([[fa, "about"]]); // linked when the file was created
    const later = await create(token, "global", { type: "idea", summary: `made after ${a}`, project: a });
    expect(await linksOf(token, "global", later.body.id)).toEqual([[fa, "about"]]);
    const moved = await send("PATCH", rec("global", `/${later.body.id}`), { token, body: { tags: [`project:${b}`, "idea"] } });
    expect(moved.body.links).toEqual([[fb, "about"]]);
    expect((await get(rec("global", `/${later.body.id}/related`), token)).body.related.map((h: { id: string }) => h.id)).toContain(fb);
    const unlinked = await send("PATCH", rec("global", `/${later.body.id}`), { token, body: { tags: ["idea"] } });
    expect(unlinked.body.links).toEqual([]); // no project tag now: the project's link goes with it
  });

  it("keeps the link in step on a sync put and refuses two project tags or a lookalike", async () => {
    const { token } = await setup();
    await wiki(token, "global", "global");
    const k = key();
    const f = (await send("PUT", projectsPath(k), { token })).body.document.id;
    const r = await sync(token, "global", { ops: [put({ ...row("R40", "synced rule"), tags: [`project:${k}`] })] });
    expect(r.body.results[0].status).toBe("accepted");
    expect((await get(rec("global", "/R40"), token)).body.links).toEqual([[f, "about"]]);
    const two = await sync(token, "global", { ops: [put({ ...row("R41", "two projects"), tags: [`project:${k}`, "project:other"] })] });
    expect(two.body.results[0].status).toBe("rejected");
    for (const bad of ["project:Foo", "project:", `project:${"a".repeat(80)}`]) {
      expect((await create(token, "global", { type: "idea", summary: "bad project", tags: [bad] })).status, bad).toBe(400);
    }
    // a document made through sync with the tag links the waiting records too
    const k2 = key();
    await create(token, "global", { type: "agent_task", title: "waits for its file", project: k2 });
    const docId = "D90";
    const d = await sync(token, "global", { ops: [put({ ...row(docId, "x"), kind: "D", data: { title: "File", body: "", alias: null, mime: "text/markdown", source: "" }, tags: [`project:${k2}`] })] });
    expect(d.body.results[0].status).toBe("accepted");
    const waiting = (await get(`/v1/tasks?owner=agent&project=${k2}&wiki=global`, token)).body.tasks[0].id as string;
    expect(await linksOf(token, "global", waiting)).toEqual([[docId, "about"]]);
  });
});
