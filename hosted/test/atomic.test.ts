// One rule per object on the Worker: REST create and edit, MCP save and update. A sync put is never refused for it (stored
// rows are grandfathered). The cases in atomic-cases.json are the local engine's (db.several_rules): both sides run them.

import { describe, expect, it } from "vitest";
import { severalRules } from "../src/atomic.js";
import fixture from "./atomic-cases.json";
import { put, rec, row, send, setup, sync } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

describe("atomic rules", () => {
  it("agrees with the local engine on every shared case", () => {
    for (const c of fixture.cases) expect(severalRules(c.summary, c.body), c.name).toBe(c.why);
  });

  it("refuses a rule that holds several on REST create and edit, and an ordinary one passes", async () => {
    const { token, w } = await setup();
    const make = (summary: string, body = "") => send("POST", rec(w), { token, body: { type: "rule", summary, body } });
    const bad = await make("1. Use npm in worktrees. 2. Never run pnpm there.");
    expect(bad.status).toBe(400);
    expect(bad.body.error.message).toContain("ONE rule");
    expect((await make("Use npm in worktrees.", "1. a\n2. b")).status).toBe(400);
    const ok = await make("Install packages with npm inside a worktree, never with pnpm.");
    expect(ok.status).toBe(201);
    const id = ok.body.id as string;
    expect((await send("PATCH", rec(w, `/${id}`), { token, body: { summary: "Use npm in worktrees. Never run pnpm there." } })).status).toBe(400);
    expect((await send("PATCH", rec(w, `/${id}`), { token, body: { body: "- Use the lockfile\n- Never edit it by hand" } })).status).toBe(400);
    expect((await send("PATCH", rec(w, `/${id}`), { token, body: { summary: "Keep lockfiles committed." } })).status).toBe(200);
    // other kinds are not rules: a memory may hold a list
    expect((await send("POST", rec(w), { token, body: { type: "memory", summary: "1. a thing 2. another thing" } })).status).toBe(201);
  });

  it("refuses it through the MCP save and update tools", async () => {
    const { token, w } = await setup();
    const saved = await tool(token, "save", { wiki: w, type: "rule", summary: "Use npm. Never pnpm." });
    expect(saved.isError).toBe(true);
    expect(saved.text).toContain("ONE rule");
    const good = await tool(token, "save", { wiki: w, type: "rule", summary: "Keep lockfiles committed." });
    expect(good.isError).toBe(false);
  });

  it("never refuses a sync put (stored rows are grandfathered)", async () => {
    const { token, w } = await setup();
    const many = row("R700", "1. Use npm in worktrees. 2. Never run pnpm there.");
    expect((await sync(token, w, { ops: [put(many)] })).body.results[0].status).toBe("accepted");
  });
});
