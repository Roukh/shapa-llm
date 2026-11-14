// The operator's universal task list: one read answers "what do I need to do", across every wiki
// the caller may read. Tasks are tickets tagged `human_task` or `agent_task`; the read walks the
// owner-tag index (rank:N first, then oldest), so it does not slow down as other records grow.

import { type Device, needScope, needWiki } from "./devices.js";
import { fail } from "./errors.js";
import { projectKeyProblem } from "./projectapi.js";
import { taskRefs } from "./relations.js";
import type { Repo } from "./repo.js";
import { rankOf, projectKeyOf, ownerOf, type Owner } from "./tags.js";
import { readableWikis } from "./wikis.js";

export const TASKS_LIMIT = 100;

export interface TaskRow {
  wiki: string;
  id: string;
  title: string;
  project: string;
  owner: Owner;
  status: string;
  created: string;
  rank: number | null;
}

export interface TaskQueryIn {
  owner?: "human" | "agent";
  status?: "open" | "closed" | "all";
  project?: string;
  wiki?: string;
  limit?: number;
}

export function taskRows(repo: Repo, wikis: string[], q: { owner: "human" | "agent"; status: "open" | "closed" | "all"; project: string; limit: number }): { tasks: TaskRow[]; total: number } {
  const { refs, total } = taskRefs(repo.sql, repo.baseline, { wikis, owner: q.owner, status: q.status, project: q.project, limit: q.limit });
  const tasks: TaskRow[] = [];
  for (const r of refs) {
    const s = repo.brief(r.wiki, r.id); // the headline only: a task's body may be 1 MB
    if (!s || s.kind !== "T") continue;
    tasks.push({
      wiki: r.wiki, id: s.id, title: s.text, project: projectKeyOf(s.tags), owner: ownerOf(s.tags),
      status: s.status || "open", created: s.created, rank: rankOf(s.tags),
    });
  }
  return { tasks, total };
}

/** The operator's open tasks (default `human`) in every wiki the device may read: rank first, then oldest; at most 100, with the total. */
export function listTasks(repo: Repo, d: Device, q: TaskQueryIn): { tasks: TaskRow[]; total: number } {
  needScope(d, "read");
  if (q.wiki !== undefined) {
    needWiki(d, q.wiki);
    if (!repo.wiki(q.wiki)) fail("not_found", `no wiki ${q.wiki}`);
  }
  const project = q.project ?? "";
  if (project) {
    const bad = projectKeyProblem(project);
    if (bad) fail("invalid", bad);
  }
  const wikis = q.wiki !== undefined ? [q.wiki] : readableWikis(repo, d);
  return taskRows(repo, wikis, {
    owner: q.owner ?? "human", status: q.status ?? "open", project, limit: Math.max(1, Math.min(q.limit ?? TASKS_LIMIT, TASKS_LIMIT)),
  });
}
