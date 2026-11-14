import { runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { OWNER, TS, env, get, mintAll, put, rec, row, send, sync, uidOf, wiki } from "./helpers.js";
import { tool } from "./mcp-helpers.js";

// "Same speed or faster as the data grows": the read paths an agent uses at session start and while
// it works are measured at 1,000 and at 10,000 records (the same store, grown by 9,000 records of mixed
// kinds, about 200 tags and 50 projects), each as a full and as a baseline device. Two measures:
//  - the rows the store reads to answer one call (SQLite counts them): deterministic, so a read that
//    starts to scan the store is caught however fast the machine is;
//  - the time of a call, as the median of batches of calls (workerd's clock only moves on I/O, and one
//    call costs a few milliseconds whatever it does, so a single call tells nothing).
// None may grow more than 3x (search: 10x) for the 10x data, or pass a floor that a scan would cross.

const KINDS = ["R", "M", "I", "D", "N", "T", "E"] as const;
const TAGS = 200;
const PROJECTS = 50;
/** Per-call time below which a read is flat whatever its ratio (a call through the Worker costs a few milliseconds). */
const FLOOR_MS = 6;
const CALLS = 6;
const BATCHES = 9;
/** Rows read per call below which a read is flat whatever its ratio: a page of fifty records and their index rows,
 *  the caps of a project or a neighbour walk (a list stops at its cap, so it grows until 1,000 records and then not). A
 *  scan of 10,000 records is far above it. A search reads and ranks every record that matches, which is the match
 *  count itself, so its floor is a page of candidates. */
const FLOOR_ROWS = 400;
const FLOOR_ROWS_SEARCH = 1000;

function record(n: number): unknown {
  const kind = KINDS[n % KINDS.length]!;
  const topic = [`topic-${n % TAGS}`, `topic-${(n * 7 + 3) % TAGS}`, `topic-${(n * 13 + 5) % TAGS}`];
  const project = `project:scale-${n % PROJECTS}`;
  const id = (k: string) => `${k}${n + 1}`;
  const base = (k: string) => ({ id: id(k), uid: uidOf(id(k), TS), created: TS, updated: TS, deleted: false, links: [] });
  switch (kind) {
    case "R": return { ...base("R"), kind: "R", data: { summary: `scale rule ${n} about ${topic[0]}`, body: "rule body ".repeat(8), alias: null, source: "", session: "" }, tags: topic };
    case "M": return { ...base("M"), kind: "M", data: { summary: `scale memory ${n} about ${topic[1]}`, body: "memory body ".repeat(8), alias: null, source: "", session: "" }, tags: topic };
    case "E": return { ...base("M"), kind: "M", data: { summary: `scale idea ${n}`, body: "", alias: null, source: "", session: "" }, tags: [...topic, "idea", project] };
    case "I": return { ...base("I"), kind: "I", data: { summary: `scale issue ${n}`, body: "", alias: null, source: "", session: "" }, tags: topic };
    case "D": return { ...base("D"), kind: "D", data: { title: `scale document ${n}`, body: "document body ".repeat(20), alias: null, mime: "text/markdown", source: "" }, tags: topic };
    case "N": return { id: `research/scale-${n}.md`, uid: "", kind: "N", created: "", updated: TS, deleted: false, links: [], data: { body: `scale note ${n} about ${topic[2]}` }, tags: topic };
    default: return {
      ...base("T"), kind: "T",
      data: { title: `scale task ${n}`, body: "", parent: null, status: "open", closed_reason: null, git_ref: null, alias: null, claimed_by: null, closed: null },
      tags: [...topic.slice(0, 2), n % 3 === 0 ? "agent_task" : "human_task", project, ...(n % 50 === 0 ? [`rank:${(n % 9) + 1}`] : [])],
    };
  }
}

async function grow(token: string, from: number, to: number): Promise<void> {
  for (let n = from; n < to; n += 200) {
    const ops = Array.from({ length: Math.min(200, to - n) }, (_, i) => put(record(n + i)));
    const r = await sync(token, "global", { ops });
    const bad = r.body.results.find((x: { status: string }) => x.status !== "accepted" && x.status !== "renamed"); // a label held by a project file is relabeled
    if (bad) throw new Error(`seed failed at ${n}: ${JSON.stringify(bad)}`);
  }
}

const median = (xs: number[]) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)]!;
/** The median over BATCHES of the mean time of CALLS calls: a batch lasts tens of milliseconds, so the clock's
 *  1 ms step (and one slow call) cannot decide the outcome. */
async function time(run: () => Promise<unknown>, calls = CALLS, batches = BATCHES): Promise<number> {
  await run(); // warm
  const t: number[] = [];
  for (let i = 0; i < batches; i++) {
    const s = performance.now();
    for (let j = 0; j < calls; j++) await run();
    t.push((performance.now() - s) / calls);
  }
  return median(t);
}

/** Rows the store reads to answer one call: the work itself, counted by SQLite, so it does not depend on a clock that
 *  only moves when I/O happens (workerd) or on the few milliseconds every call through the Worker costs. A read that
 *  scans the store reads ten times the rows at 10,000 records, and is caught however fast the machine is. */
async function rowsRead(run: () => Promise<unknown>): Promise<number> {
  const stub = env.STORE.getByName("main");
  type Cursor = SqlStorageCursor<Record<string, SqlStorageValue>>;
  const meter = (instance: unknown) => {
    const root = (instance as { root: { sql: SqlStorage; __meter?: Cursor[] } }).root;
    if (!root.__meter) {
      const cursors: Cursor[] = [];
      const real = root.sql;
      root.__meter = cursors;
      root.sql = new Proxy(real, {
        get(target, prop) {
          if (prop === "exec") return (q: string, ...args: unknown[]) => { const c = (target.exec as (q: string, ...a: unknown[]) => Cursor)(q, ...args); cursors.push(c); return c; };
          const v = (target as unknown as Record<string | symbol, unknown>)[prop];
          return typeof v === "function" ? (v as (...a: unknown[]) => unknown).bind(target) : v;
        },
      });
    }
    return root.__meter;
  };
  await run(); // warm
  await runInDurableObject(stub, (instance) => { meter(instance).length = 0; });
  await run();
  return runInDurableObject(stub, (instance) => meter(instance).reduce((n, c) => n + c.rowsRead, 0));
}

describe("scale: reads stay flat as the data grows", () => {
  it("instructions, tags, project, related, tasks, get, search and the tag listing at 1,000 and 10,000 records, as a full and as a baseline device", async () => {
    const token = (await mintAll("scale")).token;
    const baseline = (await send("POST", "/admin/devices", { token: OWNER, body: { name: "scale-baseline", scopes: ["read", "write"], reach: "baseline" } })).body.token as string;
    await wiki(token, "global", "global");
    // a few features in a repo wiki: the northstar/fires path stays constant in the data it reads
    await wiki(token, "scale-repo", "repo");
    for (let i = 0; i < 20; i++) await send("POST", rec("scale-repo"), { token, body: { type: "feature", title: `scale feature ${i}` } });
    // the operator's baseline stays small
    await sync(token, "global", { ops: [put(row("R9000", "scale baseline rule", { tags: ["stage:0"] }))] });
    for (let p = 0; p < PROJECTS; p++) await send("PUT", `/v1/projects/scale-${p}`, { token });
    expect((await send("POST", rec("global"), { token, body: { type: "idea", summary: "the one rare idea", tags: ["scale-rare"] } })).status).toBe(201);

    // Each read as the operator's full device and as a baseline device (a different path: the reach filter).
    const reads = (t: string): Record<string, () => Promise<unknown>> => ({
      "session start": () => get("/v1/instructions?client=mcp", t),
      "tag index": () => get("/v1/tags?limit=200", t),
      "project show": () => get("/v1/projects/scale-7", t),
      "related": () => get(rec("global", "/T6/related"), t),
      "tasks": () => get("/v1/tasks?owner=human", t),
      "get by id": () => get(`/v1/get?id=T6&wiki=global`, t),
      "search k=8": () => get("/v1/search?q=scale+task&k=8", t),
      "search by tag": () => get("/v1/search?tags=topic-5&k=8", t),
      "list by tag": () => get(rec("global", "?tag=topic-5&limit=20"), t),
      // a tag fewer records hold than a page: without an index the listing reads every record to find them
      "list by rare tag": () => get(rec("global", "?tag=scale-rare&limit=20"), t),
      "mcp session_start": () => tool(t, "session_start", {}),
    });
    const ops: Record<string, () => Promise<unknown>> = {};
    for (const [who, t] of [["full", token], ["baseline", baseline]] as const) {
      for (const [name, run] of Object.entries(reads(t))) ops[`${name} (${who})`] = run;
    }
    const table: Record<string, { small: number; large: number; rowsSmall: number; rowsLarge: number }> = {};
    await grow(token, 0, 1000);
    for (const [name, run] of Object.entries(ops)) table[name] = { small: await time(run), large: 0, rowsSmall: await rowsRead(run), rowsLarge: 0 };
    const small = (await get("/v1/tags?limit=1", token)).body.tags.length;
    expect(small).toBe(1);
    await grow(token, 1000, 10_000);
    for (const [name, run] of Object.entries(ops)) { table[name]!.large = await time(run); table[name]!.rowsLarge = await rowsRead(run); }

    const grows = (name: string) => (name.startsWith("search") ? 10 : 3);
    const limitOf = (name: string, smallMs: number) => Math.max(FLOOR_MS, smallMs * grows(name));
    const rowsLimit = (name: string, rows: number) => Math.max(name.startsWith("search") ? FLOOR_ROWS_SEARCH : FLOOR_ROWS, Math.ceil(rows * grows(name)));
    const lines = Object.entries(table).map(([k, v]) =>
      `${k.padEnd(30)} ${v.small.toFixed(1).padStart(6)} ms ${v.large.toFixed(1).padStart(6)} ms (limit ${limitOf(k, v.small).toFixed(0).padStart(3)})   rows read ${String(v.rowsSmall).padStart(5)} -> ${String(v.rowsLarge).padStart(5)} (limit ${rowsLimit(k, v.rowsSmall)})`);
    console.log(`\nWorker scale (median of ${BATCHES} batches of ${CALLS} calls, per call; rows the store read for one call; 1,000 -> 10,000 records)\n${lines.join("\n")}\n`);

    for (const [name, v] of Object.entries(table)) {
      expect(v.rowsLarge, `${name}: rows read ${v.rowsSmall} -> ${v.rowsLarge}`).toBeLessThanOrEqual(rowsLimit(name, v.rowsSmall));
      expect(v.large, `${name}: ${v.small.toFixed(1)} -> ${v.large.toFixed(1)} ms`).toBeLessThanOrEqual(limitOf(name, v.small));
    }
    // the answers are still right at the larger size
    const text = (await get("/v1/instructions?client=mcp", token)).body.text as string;
    expect(text).toContain("R9000 scale baseline rule");
    expect(text).toMatch(/The operator's open tasks: [1-9]\d{2,}/);
    expect((await get("/v1/projects/scale-7", token)).body.tasks.length).toBeGreaterThan(20);
  }, 600_000);
});
