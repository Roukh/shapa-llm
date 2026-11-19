// Review break-it probes as a baseline token (not part of the change under review).

import { describe, expect, it } from "vitest";
import { OWNER, freshKey, get, rec, send, setup } from "./helpers.js";

const create = (token: string, w: string, body: Record<string, unknown>) => send("POST", rec(w), { token, body });
const read = (token: string, w: string, ref: string) => get(rec(w, `/${encodeURIComponent(ref)}`), token);

async function world() {
  const { token: op, w } = await setup();
  const r = await send("POST", "/admin/devices", { token: OWNER, body: { name: `outside-${freshKey()}`, scopes: ["read", "write"], reach: "baseline" } });
  expect(r.status).toBe(201);
  return { op, w, base: r.body.token as string };
}

describe("review: oracles a baseline token still has over hidden notes", () => {
  it("learns that a hidden note's frontmatter alias exists from the alias echoed in its own create response", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    const secret = `acquisition-${k}`;
    expect((await create(op, w, { type: "note", path: `research/hid-${k}.md`, body: `---\nid: ${secret}\n---\nSECRET` })).status).toBe(201);
    const probe = async (alias: string, i: number) =>
      (await create(base, w, { type: "note", path: `research/probe-${k}-${i}.md`, body: `---\nid: ${alias}\n---\nx` })).body;
    const taken = await probe(secret, 1);
    const free = await probe(`nothing-${k}`, 2);
    // the oracle: a free alias is echoed back, a taken one (held by a note hidden from this device) comes back null
    expect(free.data.alias ?? free.alias).toBeTruthy();
    expect(taken.data.alias ?? taken.alias ?? null).toBeNull();
    expect((await read(base, w, secret)).status).toBe(404); // and it is not a visible record
  });

  it("learns that a hidden note path exists (409) versus a free one (201)", async () => {
    const { op, w, base } = await world();
    const k = freshKey();
    await create(op, w, { type: "note", path: `research/layoffs-${k}.md`, body: "SECRET" });
    expect((await create(base, w, { type: "note", path: `research/layoffs-${k}.md`, body: "x" })).status).toBe(409);
    expect((await read(base, w, `research/layoffs-${k}.md`)).status).toBe(404);
  });
});
