import { describe, expect, it } from "vitest";
import { freshKey, get, put, rec, row, send, setup, sync } from "./helpers.js";

// The learning loop's tags (`shapa learn`) are system tags like stage:* and project:*: never a topic, listed only with
// `system=1`, and a principle row written on another machine still syncs in from a full device.

const PRINCIPLE_TAGS = [
  "principle", "evidence:3", "serves:my-repo/F1", "violations:2", "revisions:1", "last-seen:2026-11-13T10:00:00Z",
  "repeats:1", "learned", "learned-at:2026-11-13T10:00:00Z", "generalized-into:global:R1", "repeat-of:R7", "churn",
];

const tagsOf = async (token: string, q: string) => (await get(`/v1/tags${q}`, token)).body.tags as { tag: string }[];

describe("the learning loop's tags are system tags", () => {
  it("accepts a principle from a full device on sync, and keeps its tags out of the topics and the tag index", async () => {
    const { token, w } = await setup();
    const topic = `topic-${freshKey()}`;
    const r = await sync(token, w, { ops: [put(row("R1", "Install with npm in a worktree", { tags: [...PRINCIPLE_TAGS, topic] }))] });
    expect(r.body.results.map((x: { status: string }) => x.status)).toEqual(["accepted"]);
    expect((await get(rec(w, "/R1"), token)).body.tags).toEqual(expect.arrayContaining(PRINCIPLE_TAGS));

    expect((await tagsOf(token, `?wiki=${w}`)).map((x) => x.tag)).toEqual([topic]);
    expect((await tagsOf(token, `?wiki=${w}&system=1`)).map((x) => x.tag)).toEqual(expect.arrayContaining([topic, ...PRINCIPLE_TAGS]));
    const session = (await get(`/v1/instructions?wiki=${w}`, token)).body.text as string;
    for (const t of PRINCIPLE_TAGS) expect(session).not.toContain(`${t} (rules`);
  });

  it("refuses a lookalike of a learning tag, and still takes the exact words as ordinary topics where they are not the tag", async () => {
    const { token, w } = await setup();
    for (const tag of ["Principle", "ＰRINCIPLE", "Serves:x", "violations​:2"]) {
      const r = await send("POST", rec(w), { token, body: { type: "rule", summary: `look ${freshKey()}`, tags: [tag] } });
      expect(r.status, tag).toBe(400);
    }
    const ok = await send("POST", rec(w), { token, body: { type: "rule", summary: `plain ${freshKey()}`, tags: ["principles", "servers"] } });
    expect(ok.status).toBe(201);
  });
});
