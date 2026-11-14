// Wiki registry: list and idempotent registration.

import { type Device, needScope, needWiki, wikiAllowed } from "./devices.js";
import { fail } from "./errors.js";
import { denyBaseline } from "./reach.js";
import { Repo } from "./repo.js";

export interface WikiInfo {
  key: string;
  name: string;
  kind: "global" | "repo";
  origin: string;
  created: string;
  counts: Record<string, number>;
}

function info(repo: Repo, key: string): WikiInfo {
  const w = repo.wiki(key)!;
  return { key: w.key, name: w.name, kind: w.kind, origin: w.origin, created: w.created, counts: repo.wikiCounts(key) };
}

/** The keys of the wikis a device may use (every registered wiki when it has no list). */
export function readableWikis(repo: Repo, d: Device): string[] {
  return repo.wikis().filter((w) => wikiAllowed(d, w.key)).map((w) => w.key);
}

export function listWikis(repo: Repo, d: Device): WikiInfo[] {
  needScope(d, "read");
  return repo.wikis().filter((w) => wikiAllowed(d, w.key)).map((w) => info(repo, w.key));
}

export function putWiki(
  repo: Repo, d: Device, key: string, body: { name: string; kind: "global" | "repo"; origin: string },
): { wiki: WikiInfo; created: boolean } {
  needScope(d, "write");
  needWiki(d, key);
  if ((key === "global") !== (body.kind === "global")) fail("invalid", 'the wiki key "global" is the global wiki, and only it');
  const cur = repo.wiki(key);
  if (!cur) {
    denyBaseline(d, "this device uses the wikis that exist and registers no new one");
    repo.insertWiki({ key, name: body.name, kind: body.kind, origin: body.origin });
    return { wiki: info(repo, key), created: true };
  }
  if (cur.kind !== body.kind) fail("conflict", `wiki ${key} is a ${cur.kind} wiki`);
  // An empty origin is a value, not a wildcard: two repos that share a folder name must not share a wiki.
  if (cur.origin !== body.origin) {
    const was = cur.origin ? `belongs to ${cur.origin}` : "has no origin";
    fail("conflict", `wiki ${key} ${was}, not ${body.origin || "an empty origin"}; choose another key with --key`);
  }
  repo.updateWiki(key, body.name, cur.kind, cur.origin);
  return { wiki: info(repo, key), created: false };
}
