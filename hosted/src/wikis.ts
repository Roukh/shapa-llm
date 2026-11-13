// Wiki registry: list and idempotent registration.

import { type Device, needScope, needWiki, wikiAllowed } from "./devices.js";
import { fail } from "./errors.js";
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
    repo.insertWiki({ key, name: body.name, kind: body.kind, origin: body.origin });
    return { wiki: info(repo, key), created: true };
  }
  if (cur.kind !== body.kind) fail("conflict", `wiki ${key} is a ${cur.kind} wiki`);
  if (cur.origin && body.origin && cur.origin !== body.origin) {
    fail("conflict", `wiki ${key} belongs to ${cur.origin}, not ${body.origin}; choose another key with --key`);
  }
  repo.updateWiki(key, body.name, cur.kind, cur.origin || body.origin);
  return { wiki: info(repo, key), created: false };
}
