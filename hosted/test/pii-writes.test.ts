import { describe, expect, it } from "vitest";
import { get, job, mint, put, rec, row, send, setup, sync, wiki } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

// The personal-data gate on every write path of the Worker: REST create and PATCH, MCP save and update,
// sync puts, the focus, project files. An app is refused every category, a stage-0 write is refused every
// category whoever writes it, the operator's own device (a delete-scope token, over sync) only the severe ones.

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const EMAIL = "write to jane.roe@gmail.com";
const CARD = "card 4111 1111 1111 1111";
const SECRET = ["sk-an", "t-api03-", "A1b2C3d4E5f6G7h8I9j0"].join("");

describe("pii gate: REST", () => {
  it("refuses an email in any field, names the category and never echoes the value", async () => {
    const { token, w } = await setup();
    for (const body of [{ summary: EMAIL, body: "x" }, { summary: "x", body: EMAIL }, { summary: "x", body: "y", alias: "jane.roe@gmail.com" }, { summary: "x", body: "y", tags: ["jane.roe@gmail.com"] }]) {
      const r = await create(token, w, { type: "memory", ...body });
      expect(r.status, JSON.stringify(body)).toBe(400);
      expect(r.body.error.message).toContain("an email address");
      expect(r.text).not.toContain("jane.roe");
    }
    for (const type of ["task", "feature", "document"]) {
      expect((await create(token, w, { type, title: EMAIL, body: "x" })).status, type).toBe(400);
    }
    expect((await create(token, w, { type: "note", path: "research/jane.roe@gmail.com.md", body: "x" })).status).toBe(400);
    expect((await get(rec(w, "/M1"), token)).status).toBe(404);
  });

  it("lets a clean write through and returns the warnings of an allowed hit without the value", async () => {
    const { token, w } = await setup();
    const r = await create(token, w, { type: "memory", summary: "the staging box is 93.184.216.34", body: "nothing personal" });
    expect(r.status).toBe(201);
    const warning = r.headers.get("x-shapa-warning") ?? "";
    expect(warning).toContain("public IPv4 address");
    expect(warning).not.toContain("93.184");
    expect(r.text).toContain("93.184.216.34"); // the record itself is stored as written; only the header is the warning
  });

  it("screens an edit, and screens the stored text when a record is moved toward stage 0", async () => {
    const { token, w } = await setup();
    const ok = await create(token, w, { type: "memory", summary: "plain", body: "text" });
    expect(ok.status).toBe(201);
    const id = ok.body.id as string;
    const bad = await send("PATCH", rec(w, `/${id}`), { token, body: { body: EMAIL } });
    expect(bad.status).toBe(400);
    expect(bad.text).not.toContain("jane.roe");
    expect((await send("PATCH", rec(w, `/${id}`), { token, body: { tags: ["jane.roe@gmail.com"] } })).status).toBe(400);
    expect((await send("PATCH", rec(w, `/${id}`), { token, body: { body: "still clean" } })).status).toBe(200);
  });

  it("refuses a secret instead of masking it, and a split value (summary + body) is one value", async () => {
    const { token, w } = await setup();
    const r = await create(token, w, { type: "memory", summary: `use ${SECRET} now`, body: "x" });
    expect(r.status).toBe(400);
    expect(r.body.error.message).toContain("a secret or credential");
    expect((await create(token, w, { type: "memory", summary: "card 4111 1111", body: "1111 1111" })).status).toBe(400);
  });

  it("refuses the project file items, the project title and the focus", async () => {
    const { token, w } = await setup();
    await wiki(token, "global", "global");
    const k = `pii-${w}`;
    expect((await send("PUT", `/v1/projects/${k}`, { token, body: { title: EMAIL } })).status).toBe(400);
    expect((await send("PUT", `/v1/projects/${k}`, { token })).status).toBeLessThan(300);
    expect((await send("POST", `/v1/projects/${k}/items`, { token, body: { section: "issues", text: EMAIL } })).status).toBe(400);
    expect((await send("PUT", `/v1/wikis/${w}/focus`, { token, body: { northstar: EMAIL } })).status).toBe(400);
    expect((await send("PUT", `/v1/wikis/${w}/focus`, { token, body: { fires: [{ text: CARD }] } })).status).toBe(400);
    expect((await send("PUT", `/v1/wikis/${w}/focus`, { token, body: { northstar: "ship it" } })).status).toBe(200);
  });
});

describe("pii gate: MCP", () => {
  it("save and update refuse personal data with the category named", async () => {
    const { token, w } = await setup();
    const saved = await tool(token, "save", { wiki: w, type: "memory", summary: "Contact", body: EMAIL });
    expect(saved.isError).toBe(true);
    expect(saved.text).toContain("an email address");
    expect(saved.text).not.toContain("jane.roe");
    const ok = await tool(token, "save", { wiki: w, type: "memory", summary: "Server", body: "runs at 93.184.216.34" });
    expect(ok.isError).toBe(false);
    expect(ok.text).toContain("Warning:");
    expect(ok.text).not.toContain("93.184");
    const upd = await tool(token, "update", { wiki: w, id: "M1", body: CARD });
    expect(upd.isError).toBe(true);
    expect(upd.text).toContain("a payment card number");
    const task = await tool(token, "ledger_add", { wiki: w, kind: "task", title: "Call 415-555-2671" });
    expect(task.isError).toBe(true);
  });
});

describe("pii gate: the whole record", () => {
  it("scans git_ref, claimed_by and the link destinations of a sync put, from the operator's device too (severe categories)", async () => {
    const { token: op, w } = await setup();
    const status = async (r: unknown) => (await sync(op, w, { ops: [put(r)] })).body.results[0];
    const f = job("F650", "feature");
    const viaRef = await status({ ...f, data: { ...f.data, git_ref: CARD } });
    expect(viaRef.status).toBe("rejected");
    expect(viaRef.error).toContain("a payment card number");
    const g = job("F651", "feature");
    expect((await status({ ...g, data: { ...g.data, claimed_by: CARD } })).status).toBe("rejected");
    const h = row("M652", "linked");
    expect((await status({ ...h, links: [["4111 1111 1111 1111", "about"]] })).status).toBe("rejected");
    expect((await status(row("M653", "clean"))).status).toBe("accepted");
  });

  it("scans the merged record on an edit: text the edit leaves alone is held to the same gate", async () => {
    const { token, w } = await setup();
    const op = token; // every scope: the operator's machine, which may keep an email over sync
    const kept = row("M660", "plain", { data: { summary: "plain", body: EMAIL, alias: null, source: "", session: "" } });
    expect((await sync(op, w, { ops: [put(kept)] })).body.results[0].status).toBe("accepted");
    const app = await mint("editor-app", ["read", "write"]);
    const e = await send("PATCH", rec(w, "/M660"), { token: app.token, body: { summary: "retitled only" } });
    expect(e.status).toBe(400);
    expect(e.text).not.toContain("jane.roe");
  });
});

describe("pii gate: sync", () => {
  it("refuses every category from an app's device and the severe ones from the operator's own", async () => {
    const op = (await setup()).token; // a device token with every scope: the operator's machine
    await wiki(op, "global", "global");
    const app = await mint("chat-app", ["read", "write"]);
    const emailRow = row("M600", "Contact", { data: { summary: "Contact", body: EMAIL, alias: null, source: "", session: "" } });
    const cardRow = row("M601", "Pay", { data: { summary: "Pay", body: CARD, alias: null, source: "", session: "" } });
    const status = async (token: string, r: unknown) => (await sync(token, "global", { ops: [put(r)] })).body.results[0];
    expect((await status(app.token, emailRow)).status).toBe("rejected");
    expect((await status(app.token, emailRow)).error).toContain("an email address");
    expect((await status(op, emailRow)).status).toBe("accepted"); // the operator's own device may keep an email
    const severe = await status(op, cardRow);
    expect(severe.status).toBe("rejected");
    expect(severe.error).toContain("a payment card number");
    expect(JSON.stringify(severe)).not.toContain("4111");
  });

  it("refuses every category for a stage-0 put, even from the operator, and a value in a note path or a tag", async () => {
    const op = (await setup()).token;
    await wiki(op, "global", "global");
    const staged = row("R610", "Baseline rule", { tags: ["stage:0"], data: { summary: "Baseline rule", body: EMAIL, alias: null, source: "", session: "" } });
    const r = (await sync(op, "global", { ops: [put(staged)] })).body.results[0];
    expect(r.status).toBe("rejected");
    expect(r.error).toContain("an email address");
    const tagged = row("M611", "Tagged", { tags: ["jane.roe@gmail.com"] });
    expect((await sync(op, "global", { ops: [put(tagged)] })).body.results[0].status).toBe("accepted"); // the operator's level: an email is allowed
    const app = await mint("chat-app-2", ["read", "write"]);
    expect((await sync(app.token, "global", { ops: [put(tagged)] })).body.results[0].status).toBe("rejected");
    const note = { id: "research/jane.roe@gmail.com.md", uid: "", kind: "N", created: "", updated: "2026-01-01T00:00:00Z", deleted: false, data: { body: "x" }, tags: [], links: [] };
    expect((await sync(app.token, "global", { ops: [put(note)] })).body.results[0].status).toBe("rejected");
  });

  it("refuses a ledger item's text from an app", async () => {
    const op = (await setup()).token;
    await wiki(op, "global", "global");
    const app = await mint("chat-app-3", ["read", "write"]);
    const t = job("T620", EMAIL, null, { tags: ["human_task"] });
    expect((await sync(app.token, "global", { ops: [put(t)] })).body.results[0].status).toBe("rejected");
  });
});

describe("pii gate: evasion", () => {
  it("normalises zero-width characters, full-width forms, entities, escapes and base64 before matching", async () => {
    const { token, w } = await setup();
    const attempts = [
      "j​ane.roe@gm​ail.com",
      "jane.roe＠gmail.com",
      "jane.roe&#64;gmail.com",
      "jane.roe%40gmail.com",
      "jane.roe (at) gmail (dot) com",
      "amFuZS5yb2VAZ21haWwuY29t",
      "4111​ 1111 ١١١١ 1111",
    ];
    for (const text of attempts) {
      const r = await create(token, w, { type: "memory", summary: "x", body: text });
      expect(r.status, JSON.stringify(text)).toBe(400);
    }
  });

  it("stays fast on a 100 KB body of the worst input", async () => {
    const { token, w } = await setup();
    const t0 = Date.now();
    const r = await create(token, w, { type: "memory", summary: "big", body: "7 ".repeat(50_000) });
    expect(r.status).toBe(201);
    expect(Date.now() - t0).toBeLessThan(3000);
  });
});

describe("pii gate: the paths around a write", () => {
  it("screens a close's git_ref and a wiki's name", async () => {
    const { token, w } = await setup();
    const f = await create(token, w, { type: "feature", title: "Ship it" });
    expect(f.status).toBe(201);
    const closed = await send("POST", rec(w, `/${f.body.id}/close`), { token, body: { reason: "agent", git_ref: "jane.roe@gmail.com" } });
    expect(closed.status).toBe(400);
    expect(closed.text).not.toContain("jane.roe");
    const named = await send("PUT", `/v1/wikis/${w}-named`, { token, body: { name: "jane.roe@gmail.com", kind: "repo", origin: "" } });
    expect(named.status).toBe(400);
  });

  it("will not move a record that already holds an email toward stage 0", async () => {
    const op = (await setup()).token;
    await wiki(op, "global", "global");
    const stored = row("R630", "Contact rule", { data: { summary: "Contact rule", body: EMAIL, alias: null, source: "", session: "" } });
    expect((await sync(op, "global", { ops: [put(stored)] })).body.results[0].status).toBe("accepted"); // the operator's level allows an email
    const moved = await send("PATCH", rec("global", "/R630"), { token: op, body: { tags: ["stage:0"] } });
    expect(moved.status).toBe(400);
    expect(moved.text).not.toContain("jane.roe");
    expect((await send("PATCH", rec("global", "/R630"), { token: op, body: { tags: ["stage:3"] } })).status).toBe(200); // any other stage is not the baseline
  });
});
