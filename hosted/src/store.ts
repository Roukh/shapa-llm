// The one Durable Object (name "main"): the single writer for every wiki, record, device and
// failed-attempt counter. REST routes and MCP tools both call these methods, so each
// operation has one implementation. Every method re-resolves the calling device from its
// credential, so a revoked device is refused on its next request, and returns `Out<T>` because
// Durable Object RPC does not carry custom error classes.

import { DurableObject } from "cloudflare:workers";
import { type Device, Devices, needWiki, scopesCoherent, validDeviceName, validScopes, validWikis, wikiAllowed } from "./devices.js";
import { fail, run, type Out } from "./errors.js";
import { composeInstructions, type ClientKind } from "./instructions.js";
import * as ledger from "./ledger.js";
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
  private readonly repo: Repo;
  private readonly devices: Devices;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    this.repo = new Repo(ctx.storage.sql);
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

  // --- identity and devices -----------------------------------------------------------------

  whoami(cred: Cred): Out<Device> {
    return this.tx(() => this.as(cred));
  }

  createDevice(a: { name: string; scopes: Scope[]; wikis: string[] | null; source: "token" | "oauth"; tokenHash: string }): Out<Device> {
    return this.tx(() => {
      if (!validDeviceName(a.name)) fail("invalid", "name must be 1-64 characters, no control characters, no surrounding spaces");
      if (!validScopes(a.scopes)) fail("invalid", "scopes must be a non-empty list of read, write, delete");
      if (!scopesCoherent(a.scopes)) fail("invalid", "write needs read, and delete needs read and write");
      if (!validWikis(a.wikis)) fail("invalid", "wikis must be null or a list of wiki keys");
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
    return this.tx(() => listWikis(this.repo, this.as(cred)));
  }

  putWiki(cred: Cred, key: string, body: { name: string; kind: "global" | "repo"; origin: string }): Out<{ wiki: WikiInfo; created: boolean }> {
    return this.tx(() => putWiki(this.repo, this.as(cred), key, body));
  }

  // --- read -----------------------------------------------------------------------------------

  instructions(cred: Cred, opts: { client: ClientKind; wiki?: string; budget: number }): Out<{ text: string; version: string }> {
    return this.tx(() => composeInstructions(this.repo, this.as(cred), opts));
  }

  search(cred: Cred, q: { query: string; k: number; wiki?: string; types?: string[] }): Out<ReturnType<typeof search>> {
    return this.tx(() => search(this.repo, this.as(cred), q));
  }

  get(cred: Cred, ref: string, wiki?: string): Out<ReturnType<typeof getAcross>> {
    return this.tx(() => getAcross(this.repo, this.as(cred), ref, wiki));
  }

  list(cred: Cred, wiki: string, f: { kind?: Kind; status?: string; tag?: string; after: number; limit: number }): Out<{ records: RecordWire[]; next: number | null }> {
    return this.tx(() => ledger.listRecords(this.repo, this.as(cred), wiki, f));
  }

  record(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.tx(() => ledger.getRecord(this.repo, this.as(cred), wiki, ref));
  }

  /** Live features with their jobs and tasks, for one wiki or every wiki the device may use. */
  ledger(cred: Cred, wiki?: string): Out<{ wiki: string; features: ledger.LedgerNode[] }[]> {
    return this.tx(() => {
      const d = this.as(cred);
      if (!d.scopes.includes("read")) fail("forbidden", "this device lacks the read scope");
      if (wiki !== undefined) {
        needWiki(d, wiki);
        if (!this.repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
      }
      return this.repo
        .wikis()
        .filter((w) => w.kind === "repo" && wikiAllowed(d, w.key) && (wiki === undefined || w.key === wiki))
        .map((w) => ({ wiki: w.key, features: ledger.ledgerTree(this.repo, w.key) }));
    });
  }

  // --- write ----------------------------------------------------------------------------------

  create(cred: Cred, wiki: string, body: CreateIn): Out<{ record: RecordWire; created: boolean }> {
    return this.tx(() => ledger.createRecord(this.repo, this.as(cred), wiki, body));
  }

  patch(cred: Cred, wiki: string, ref: string, patch: PatchIn): Out<RecordWire> {
    return this.tx(() => ledger.patchRecord(this.repo, this.as(cred), wiki, ref, patch));
  }

  claim(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.tx(() => ledger.claim(this.repo, this.as(cred), wiki, ref));
  }

  release(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.tx(() => ledger.release(this.repo, this.as(cred), wiki, ref));
  }

  close(cred: Cred, wiki: string, ref: string, reason: string, gitRef?: string): Out<{ record: RecordWire; closed: string[] }> {
    return this.tx(() => ledger.close(this.repo, this.as(cred), wiki, ref, reason, gitRef));
  }

  remove(cred: Cred, wiki: string, ref: string): Out<RecordWire> {
    return this.tx(() => ledger.deleteRecord(this.repo, this.as(cred), wiki, ref));
  }

  sync(cred: Cred, wiki: string, req: Parameters<typeof sync>[3]): Out<SyncResponseOut> {
    return this.tx(() => sync(this.repo, this.as(cred), wiki, req));
  }
}
