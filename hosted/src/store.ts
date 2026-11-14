// The one Durable Object (name "main"): the single writer for every wiki, record, device and
// failed-attempt counter. REST routes and MCP tools both call these methods, so each
// operation has one implementation. Every method re-resolves the calling device from its
// credential, so a revoked device is refused on its next request, and returns `Out<T>` because
// Durable Object RPC does not carry custom error classes.
//
// Every operation runs through `act`: it hands the work the caller's VIEW of the store (the
// unfiltered root only for the owner's admin methods), so what a baseline device may read is
// decided in one place and a new operation cannot forget it.

import { DurableObject } from "cloudflare:workers";
import { type Device, Devices, type Reach, needWiki, scopesCoherent, validDeviceName, validScopes, validWikis, wikiAllowed } from "./devices.js";
import { fail, run, type Out } from "./errors.js";
import * as focus from "./focus.js";
import { composeInstructions, type ClientKind } from "./instructions.js";
import * as skills from "./skills.js";
import * as ledger from "./ledger.js";
import * as projectApi from "./projectapi.js";
import * as tagApi from "./tagapi.js";
import * as tasks from "./tasks.js";
import { denyBaseline } from "./reach.js";
import type { CreateIn, Kind, PatchIn, RecordWire, Scope, SyncResponseOut } from "./records.js";
import { Repo } from "./repo.js";
import { migrate } from "./schema.js";
import { search, getAcross } from "./search.js";
import { sync } from "./sync.js";
import { listWikis, putWiki, type WikiInfo } from "./wikis.js";

/** What the Worker knows about a caller: a device id, plus the hashed token secret for device tokens. */
export interface Cred {
  device: string;
  secretHash?: string;
}

export const STORE_NAME = "main";

export class ShapaStore extends DurableObject<Env> {
  /** The unfiltered store. Operations never read it directly: they take the view of the device they serve (`act`). */
  private readonly root: Repo;
  private readonly devices: Devices;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.root = new Repo(ctx.storage.sql);
    this.devices = new Devices(ctx.storage.sql);
    ctx.blockConcurrencyWhile(async () => migrate(ctx.storage.sql));
  }

  /** One atomic unit: an ApiError anywhere inside rolls the whole request back. */
  private tx<T>(fn: () => T): Out<T> {
    return run(() => this.ctx.storage.transactionSync(fn));
  }

  private as(cred: Cred): Device {
    const d = cred.secretHash !== undefined ? this.devices.verify(cred.device, cred.secretHash) : this.devices.active(cred.device);
    if (!d) fail("unauthorized", "unknown or revoked credential");
    return d;
  }

  /** One operation as one device, in one transaction, through that device's view of the store. */
  private act<T>(cred: Cred, fn: (repo: Repo, d: Device) => T): Out<T> {
    return this.tx(() => {
      const d = this.as(cred);
      return fn(this.root.view(d.reach), d);
    });
  }

  // --- identity and devices -----------------------------------------------------------------

  whoami(cred: Cred): Out<Device> {
    return this.tx(() => this.as(cred));
  }

  createDevice(a: { name: string; scopes: Scope[]; wikis: string[] | null; source: "token" | "oauth"; tokenHash: string; reach?: Reach }): Out<Device> {
    return this.tx(() => {
      if (!validDeviceName(a.name)) fail("invalid", "name must be 1-64 characters, no control characters, no surrounding spaces");
      if (!validScopes(a.scopes)) fail("invalid", "scopes must be a non-empty list of read, write, delete");
      if (!scopesCoherent(a.scopes)) fail("invalid", "write needs read, and delete needs read and write");
      if (!validWikis(a.wikis)) fail("invalid", "wikis must be null or a list of wiki keys");
      if (a.reach !== undefined && a.reach !== "full" && a.reach !== "baseline") fail("invalid", "reach must be full or baseline");
      return this.devices.create(a);
    });
  }

  listDevices(): Out<Device[]> {
    return this.tx(() => this.devices.list());
  }

  revokeDevice(id: string): Out<Device> {
    return this.tx(() => {
      if (!this.devices.revoke(id)) fail("not_found", `no device ${id}`);
      return this.devices.get(id)!;
    });
  }

  /** Owner-key throttle: seconds to wait, or 0. */
  ownerBlocked(ip: string): Out<number> {
    return this.tx(() => this.devices.blockedFor(ip));
  }

  ownerFailed(ip: string): Out<number> {
    return this.tx(() => {
      this.devices.recordFailure(ip);
      return this.devices.blockedFor(ip);
    });
  }

  // --- wikis ----------------------------------------------------------------------------------

  wikis(cred: Cred): Out<WikiInfo[]> {
    return this.act(cred, (repo, d) => listWikis(repo, d));
  }

  putWiki(cred: Cred, key: string, body: { name: string; kind: "global" | "repo"; origin: string }): Out<{ wiki: WikiInfo; created: boolean }> {
    return this.act(cred, (repo, d) => putWiki(repo, d, key, body));
  }

  // --- read -----------------------------------------------------------------------------------

  instructions(cred: Cred, opts: { client: ClientKind; wiki?: string; budget: number; full?: boolean; stage?: number }): Out<{ text: string; version: string }> {
    return this.act(cred, (repo, d) => composeInstructions(repo, d, opts));
  }

  // --- focus and skills -----------------------------------------------------------------------

  /** The northstar, the fires with their tree, and the candidates for the next free slot. */
  focus(cred: Cred, wiki: string): Out<focus.FocusView> {
    return this.act(cred, (repo, d) => focus.getFocus(repo, d, wiki));
  }

  setFocus(cred: Cred, wiki: string, body: Parameters<typeof focus.setFocus>[3]): Out<focus.FocusView> {
    return this.act(cred, (repo, d) => {
      denyBaseline(d, "this device does not set the focus");
      return focus.setFocus(repo, d, wiki, body);
    });
  }

  promoteFocus(cred: Cred, wiki: string, body: Parameters<typeof focus.promote>[3]): Out<focus.FocusView> {
    return this.act(cred, (repo, d) => {
      denyBaseline(d, "this device does not set the focus");
      return focus.promote(repo, d, wiki, body);
    });
  }

  skills(cred: Cred, wiki: string): Out<skills.SkillSummary[]> {
    return this.act(cred, (repo, d) => skills.listSkills(repo, d, wiki));
  }

  skill(cred: Cred, wiki: string, name: string, version?: number): Out<skills.SkillVersion> {
    return this.act(cred, (repo, d) => skills.getSkill(repo, d, wiki, name, version));
  }

  search(cred: Cred, q: { query: string; k: number; wiki?: string; types?: string[]; tags?: string[] }): Out<ReturnType<typeof search>> {
    return this.act(cred, (repo, d) => search(repo, d, q));
  }

  get(cred: Cred, ref: string, wiki?: string): Out<ReturnType<typeof getAcross>> {
    return this.act(cred, (repo, d) => getAcross(repo, d, ref, wiki));
  }

  list(cred: Cred, wiki: string, f: { kind?: Kind; status?: string; tag?: string; after: number; limit: number }): Out<{ records: RecordWire[]; next: number | null }> {
    return this.act(cred, (repo, d) => ledger.listRecords(repo, d, wiki, f));
  }

  record(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.act(cred, (repo, d) => ledger.getRecord(repo, d, wiki, ref));
  }

  /** Live features with their jobs and tasks, for one wiki or every wiki the device may use. */
  ledger(cred: Cred, wiki?: string): Out<{ wiki: string; features: ledger.LedgerNode[] }[]> {
    return this.act(cred, (repo, d) => {
      if (!d.scopes.includes("read")) fail("forbidden", "this device lacks the read scope");
      if (wiki !== undefined) {
        needWiki(d, wiki);
        if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
      }
      return repo
        .wikis()
        .filter((w) => w.kind === "repo" && wikiAllowed(d, w.key) && (wiki === undefined || w.key === wiki))
        .map((w) => ({ wiki: w.key, features: ledger.ledgerTree(repo, w.key) }));
    });
  }

  // --- tags, relations, tasks, projects ---------------------------------------------------------

  tags(cred: Cred, q: { wiki?: string; system?: boolean; prefix?: string; limit?: number }): Out<ReturnType<typeof tagApi.getTags>> {
    return this.act(cred, (repo, d) => tagApi.getTags(repo, d, q));
  }

  related(cred: Cred, wiki: string, ref: string, limit?: number): Out<ReturnType<typeof tagApi.related>> {
    return this.act(cred, (repo, d) => tagApi.related(repo, d, wiki, ref, limit));
  }

  tasks(cred: Cred, q: tasks.TaskQueryIn): Out<ReturnType<typeof tasks.listTasks>> {
    return this.act(cred, (repo, d) => tasks.listTasks(repo, d, q));
  }

  projects(cred: Cred, wiki: string): Out<ReturnType<typeof projectApi.listProjects>> {
    return this.act(cred, (repo, d) => projectApi.listProjects(repo, d, wiki));
  }

  project(cred: Cred, key: string, wiki: string): Out<projectApi.ProjectOut> {
    return this.act(cred, (repo, d) => projectApi.showProject(repo, d, key, wiki));
  }

  projectNew(cred: Cred, key: string, wiki: string, title?: string): Out<ReturnType<typeof projectApi.newProject>> {
    return this.act(cred, (repo, d) => projectApi.newProject(repo, d, key, wiki, title));
  }

  projectAdd(cred: Cred, key: string, wiki: string, section: string, text: string): Out<ReturnType<typeof projectApi.addItem>> {
    return this.act(cred, (repo, d) => projectApi.addItem(repo, d, key, wiki, section, text));
  }

  // --- write ----------------------------------------------------------------------------------

  create(cred: Cred, wiki: string, body: CreateIn): Out<ledger.Created> {
    return this.act(cred, (repo, d) => ledger.createRecord(repo, d, wiki, body));
  }

  patch(cred: Cred, wiki: string, ref: string, patch: PatchIn): Out<ledger.Patched> {
    return this.act(cred, (repo, d) => ledger.patchRecord(repo, d, wiki, ref, patch));
  }

  claim(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.act(cred, (repo, d) => ledger.claim(repo, d, wiki, ref));
  }

  release(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.act(cred, (repo, d) => ledger.release(repo, d, wiki, ref));
  }

  close(cred: Cred, wiki: string, ref: string, reason: string, gitRef?: string): Out<{ record: RecordWire; closed: string[] }> {
    return this.act(cred, (repo, d) => ledger.close(repo, d, wiki, ref, reason, gitRef));
  }

  remove(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.act(cred, (repo, d) => ledger.deleteRecord(repo, d, wiki, ref));
  }

  sync(cred: Cred, wiki: string, req: Parameters<typeof sync>[3]): Out<SyncResponseOut> {
    return this.act(cred, (repo, d) => sync(repo, d, wiki, req));
  }
}
