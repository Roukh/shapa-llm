// Tags are the relations: the tag index, and the records related to one record (linked to it in
// either direction, then sharing its tags). Both walk the relation index (relations.ts), so what
// they cost does not grow with the number of records, and both read through the caller's view
// (`repo`): a device limited to some wikis, or a baseline device, sees nothing outside its reach.

import { type Device, needScope, needWiki } from "./devices.js";
import { fail } from "./errors.js";
import { TYPE_NAMES, cmp, type Kind } from "./records.js";
import { linkedIn, tagIndex, tagNeighbours, type TagRow } from "./relations.js";
import type { Repo } from "./repo.js";
import { isSystemTag } from "./tags.js";
import { readableWikis } from "./wikis.js";

export const TAGS_LIMIT = { default: 200, max: 1000 } as const;
export const RELATED_LIMIT = { default: 10, max: 50 } as const;
/** Per tag, how many neighbours the related walk looks at: the walk costs this, however large the tag grows. */
const NEIGHBOURS_PER_TAG = 400;
const LINKED_CAP = 200;
/** How many of a record's own tags the related walk follows (it may carry a hundred). */
const TAGS_WALKED = 12;

export function getTags(repo: Repo, d: Device, q: { wiki?: string; system?: boolean; prefix?: string; limit?: number }): { tags: TagRow[] } {
  needScope(d, "read");
  if (q.wiki !== undefined) {
    needWiki(d, q.wiki);
    if (!repo.wiki(q.wiki)) fail("not_found", `no wiki ${q.wiki}`);
  }
  const wikis = q.wiki !== undefined ? [q.wiki] : readableWikis(repo, d);
  const limit = Math.max(1, Math.min(q.limit ?? TAGS_LIMIT.default, TAGS_LIMIT.max));
  return { tags: tagIndex(repo.sql, repo.baseline, wikis, { limit, system: q.system === true, ...(q.prefix ? { prefix: q.prefix } : {}) }) };
}

export interface RelatedHit {
  id: string;
  type: string;
  summary: string;
  updated: string;
  /** How it is related: linked (either direction) or only by shared tags. */
  via: "link" | "tags";
  shared: number;
}

export function related(repo: Repo, d: Device, wiki: string, ref: string, limit: number = RELATED_LIMIT.default): { id: string; wiki: string; related: RelatedHit[] } {
  needScope(d, "read");
  needWiki(d, wiki);
  if (!repo.wiki(wiki)) fail("not_found", `no wiki ${wiki}`);
  const rec = repo.find(wiki, ref);
  if (!rec) fail("not_found", `no record ${ref} in ${wiki}`);
  const n = Math.max(1, Math.min(limit, RELATED_LIMIT.max));

  const cand = new Map<string, { linked: boolean; shared: number; rev: number }>();
  const note = (id: string, rev: number, linked: boolean, shared = 0) => {
    if (id === rec.id) return;
    const c = cand.get(id) ?? { linked: false, shared: 0, rev };
    cand.set(id, { linked: c.linked || linked, shared: c.shared + shared, rev: Math.max(c.rev, rev) });
  };
  // Linked records, either direction (the ones this reach may see).
  for (const [dst] of rec.links.slice(0, LINKED_CAP)) {
    const s = repo.briefRef(wiki, dst);
    if (s) note(s.id, s.rev, true);
  }
  for (const src of linkedIn(repo.sql, repo.baseline, wiki, [rec.id, ...(rec.alias ? [rec.alias] : [])], LINKED_CAP)) {
    const s = repo.brief(wiki, src);
    if (s) note(s.id, s.rev, true);
  }
  // Shared non-system tags: how many each neighbour shares with this record.
  for (const tag of rec.tags.filter((t) => !isSystemTag(t)).slice(0, TAGS_WALKED)) {
    for (const nb of tagNeighbours(repo.sql, repo.baseline, wiki, tag, rec.id, NEIGHBOURS_PER_TAG)) note(nb.id, nb.rev, false, 1);
  }
  const ranked = [...cand.entries()]
    .sort(([ia, a], [ib, b]) => Number(b.linked) - Number(a.linked) || b.shared - a.shared || b.rev - a.rev || cmp(ia, ib))
    .slice(0, n);
  const out: RelatedHit[] = [];
  for (const [id, c] of ranked) {
    const s = repo.brief(wiki, id); // headlines only: a neighbour's body may be 1 MB
    if (!s) continue;
    out.push({ id: s.id, type: TYPE_NAMES[s.kind as Kind], summary: s.text, updated: s.updated, via: c.linked ? "link" : "tags", shared: c.shared });
  }
  return { id: rec.id, wiki, related: out };
}
