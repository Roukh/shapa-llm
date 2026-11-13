import { describe, expect, it } from "vitest";
import { freshKey, get, mint, rec, send, setup, wiki } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

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

describe("session instructions", () => {
  it("composes intro, operator notes, rules, live features and the write-back format", async () => {
    const { token, w } = await setup();
    await seedGlobal(token);
    await create(token, w, { type: "feature", title: "Ship hosted mode" });
    await create(token, w, { type: "feature", title: "Closed already" });
    await create(token, w, { type: "job", title: "Store", parent: "F1" });
    await send("POST", rec(w, "/F2/close"), { token, body: { reason: "agent" } });
    await send("POST", rec(w, "/F1/claim"), { token });

    const r = await get(`/v1/instructions?client=mcp&wiki=${w}`, token);
    expect(r.status).toBe(200);
    const text: string = r.body.text;
    expect(text).toContain("shapa is this operator's shared memory");
    expect(text).toContain("OPERATOR'S SESSION INSTRUCTIONS\nAlways recall before acting. Keep replies short.");
    expect(text).not.toContain("id: session-instructions");
    expect(text).toContain("OPERATOR'S STYLE\nPlain words. No filler. Lead with the answer.");
    expect(text).toMatch(/RULES\n(?:.*\n)*R\d+ Never push to main directly/);
    expect(text).toContain(`${w}/F1 Ship hosted mode [claimed by tester] (1 open job)`);
    expect(text).not.toContain("Closed already");
    expect(text).toContain("HOW TO WRITE BACK (MCP tools)");
    expect(r.body.version).toMatch(/^1\.\d+$/);
  });

  it("gives the mcp flavor the save call and the rest flavor the exact POST body", async () => {
    const { token } = await setup();
    const mcp = (await get("/v1/instructions?client=mcp", token)).body.text as string;
    expect(mcp).toContain("Call `save` with {wiki, type, summary, body?, tags?}");
    for (const type of ["memory", "rule", "issue", "idea", "document", "note"]) expect(mcp).toContain(`- ${type}:`);
    expect(mcp).toContain("`ledger_list`");
    expect(mcp).not.toContain("POST /v1/wikis/{wiki}/records");

    const rest = (await get("/v1/instructions?client=rest", token)).body.text as string;
    expect(rest).toContain("POST /v1/wikis/{wiki}/records");
    expect(rest).toContain('{"type":"memory","summary":"<one line, max 160>","body":"<optional detail>","tags":["<optional>"]}');
    expect(rest).toContain('Authorization: Bearer <token>');
    expect(rest).toContain("/close");
    expect(rest).not.toContain("Call `save`");
    // the default for the REST route is the rest flavor
    expect((await get("/v1/instructions", token)).body.text).toBe(rest);
  });

  it("stays within the budget and never cuts the write-back block", async () => {
    const { token, w } = await setup();
    await seedGlobal(token);
    await create(token, "global", { type: "note", path: "session-instructions.md", body: "x" }); // exists: 409, harmless
    for (let i = 0; i < 80; i++) await create(token, "global", { type: "rule", summary: `Rule number ${i} with a fair amount of words to take up room` });
    for (let i = 0; i < 40; i++) await create(token, w, { type: "feature", title: `Feature ${i} with a reasonably long descriptive title for the budget` });
    for (const budget of [2000, 2500, 6000, 12000, 40000]) {
      for (const client of ["mcp", "rest"]) {
        const r = await get(`/v1/instructions?client=${client}&budget=${budget}`, token);
        expect(r.status).toBe(200);
        expect(r.body.text.length, `${client} ${budget}`).toBeLessThanOrEqual(budget);
        expect(r.body.text, `${client} ${budget}`).toContain(client === "mcp" ? "HOW TO WRITE BACK (MCP tools)" : "HOW TO WRITE BACK (HTTP");
        expect(r.body.text).toContain("shapa is this operator's");
      }
    }
    const tight = (await get("/v1/instructions?client=mcp&budget=2500", token)).body.text as string;
    expect(tight).toMatch(/…and \d+ more/);
    // a larger budget carries more
    const roomy = (await get("/v1/instructions?client=mcp&budget=30000", token)).body.text as string;
    expect(roomy.length).toBeGreaterThan(tight.length);
    expect((await get("/v1/instructions?budget=1999", token)).status).toBe(400);
    expect((await get("/v1/instructions?budget=40001", token)).status).toBe(400);
    expect((await get("/v1/instructions?budget=abc", token)).status).toBe(400);
    expect((await get("/v1/instructions?client=other", token)).status).toBe(400);
  });

  it("clips a very long operator note instead of overflowing", async () => {
    const { token } = await setup();
    await seedGlobal(token);
    await send("PATCH", rec("global", "/session-instructions"), { token, body: { body: "word ".repeat(20_000) } });
    const r = await get("/v1/instructions?client=mcp&budget=3000", token);
    expect(r.body.text.length).toBeLessThanOrEqual(3000);
    expect(r.body.text).toContain("HOW TO WRITE BACK");
    expect(r.body.text).toContain("…");
  });

  it("returns plain text on request and a version that moves with the data", async () => {
    const { token, w } = await setup();
    const plain = await send("GET", "/v1/instructions?client=mcp", { token, headers: { accept: "text/plain" } });
    expect(plain.status).toBe(200);
    expect(plain.headers.get("content-type")).toContain("text/plain");
    expect(plain.text).toContain("HOW TO WRITE BACK (MCP tools)");
    const v1 = (await get("/v1/instructions", token)).body.version;
    await create(token, w, { type: "memory", summary: "something new" });
    const v2 = (await get("/v1/instructions", token)).body.version;
    expect(v2).not.toBe(v1);
    expect((await get("/v1/instructions", token)).body.version).toBe(v2);
  });

  it("serves the same text through MCP session_start", async () => {
    const { token, w } = await setup();
    await seedGlobal(token);
    await create(token, w, { type: "feature", title: "Visible to MCP" });
    const viaMcp = await tool(token, "session_start", { wiki: w });
    expect(viaMcp.isError).toBe(false);
    const viaRest = (await get(`/v1/instructions?client=mcp&wiki=${w}&budget=6000`, token)).body.text as string;
    expect(viaMcp.text).toBe(viaRest);
    expect(viaMcp.text).toContain("Visible to MCP");
    const restFlavor = await tool(token, "session_start", { client: "rest" });
    expect(restFlavor.text).toContain("POST /v1/wikis/{wiki}/records");
  });

  it("needs a token, is never cached, and cannot be sniffed as HTML", async () => {
    const { token } = await setup();
    const plain = await send("GET", "/v1/instructions", { token, headers: { accept: "text/plain" } });
    expect(plain.headers.get("x-content-type-options")).toBe("nosniff");
    expect(plain.headers.get("cache-control")).toBe("no-store");
    expect((await get("/v1/instructions")).status).toBe(401);
  });

  it("does not reveal another wiki to a device outside it", async () => {
    const a = await setup();
    const b = await setup();
    await create(a.token, b.w, { type: "feature", title: "Private feature of b" });
    const lim = await mint("a-only", ["read"], [a.w]);
    expect((await get(`/v1/instructions?wiki=${b.w}`, lim.token)).status).toBe(403);
    expect(JSON.stringify((await get("/v1/instructions", lim.token)).body)).not.toContain(b.w);
  });

  it("shows a restricted device only what it may read", async () => {
    const a = await setup();
    const hidden = freshKey();
    await wiki(a.token, hidden);
    await create(a.token, a.w, { type: "feature", title: "Allowed feature" });
    await create(a.token, hidden, { type: "feature", title: "Hidden feature" });
    await create(a.token, hidden, { type: "rule", summary: "Hidden rule" });
    const lim = await mint("limited", ["read"], [a.w]);
    const text = (await get("/v1/instructions", lim.token)).body.text as string;
    expect(text).toContain("Allowed feature");
    expect(text).not.toContain("Hidden feature");
    expect(text).not.toContain("Hidden rule");
    expect((await get(`/v1/instructions?wiki=${hidden}`, lim.token)).status).toBe(403);
    expect((await get("/v1/instructions?wiki=no-such-wiki", a.token)).status).toBe(404);
  });
});
