// The project file: the one document of a project (`project:KEY`), written from one template.
// Text only: the template, the section keys, reading the numbered items of a section and
// appending one. The same template text is held by shapa/projectfile.py; a test on each side
// compares it with the contract's.

import { DOC_BODY_MAX, Invalid } from "./records.js";
import { redact } from "./redact.js";

export const SECTIONS = [
  "issues", "questions", "implementations", "problem", "solution", "diff", "advantages", "villains", "loop_market", "loop_product",
] as const;
export type Section = (typeof SECTIONS)[number];

/** The heading line of each section, exactly as the template writes it. */
export const HEADINGS: Record<Section, string> = {
  issues: "Issues:", questions: "Questions:", implementations: "Implementations:",
  problem: "Problem:", solution: "Solution:", diff: "Diff:", advantages: "advantages:", villains: "villains:",
  loop_market: "loop_market:", loop_product: "loop_product:",
};

const RULE = "---";
const ARCH = "### Architecture:";

/** The template, byte for byte (each empty item is "1. " with its trailing space; the text ends with a newline). */
export const PROJECT_TEMPLATE = [
  "Issues:", "1. ", "", "Questions:", "1. ", "", "Implementations:", "1. ", "", RULE, "", ARCH, "",
  "Problem:", "1. ", "", "Solution:", "1. ", "", "Diff:", "1. ", "", "advantages:", "1. ", "", "villains:", "1. ", "",
  "loop_market:", "1. ", "", "loop_product:", "1. ", "",
].join("\n");

export const ITEM_MAX = 500;
export const isSection = (s: unknown): s is Section => typeof s === "string" && (SECTIONS as readonly string[]).includes(s);

const SECTION_BY_HEADING = new Map<string, Section>(SECTIONS.map((s) => [HEADINGS[s], s]));
const ITEM_RE = /^(\d{1,6})\. ?(.*)$/;
/** A line that ends the items of a section: any section heading, the rule, the architecture heading. */
const boundary = (line: string) => SECTION_BY_HEADING.has(line) || line === RULE || line === ARCH;

/** One item's text: a single masked line without control or invisible characters, at most ITEM_MAX characters. */
export function cleanItem(text: unknown): string {
  const folded = String(text ?? "").replace(/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu, " ").split(/\s+/).filter(Boolean).join(" ");
  if (!folded) throw new Invalid("text is required");
  if (Array.from(folded).length > ITEM_MAX) throw new Invalid(`an item is one line of at most ${ITEM_MAX} characters`);
  return redact(folded);
}

/** The non-empty items of each section of a project file. A heading counts only on a line of its own, unindented. */
export function parseSections(body: string): Record<Section, string[]> {
  const out = Object.fromEntries(SECTIONS.map((s) => [s, [] as string[]])) as Record<Section, string[]>;
  let current: Section | null = null;
  for (const line of body.split("\n")) {
    const heading = SECTION_BY_HEADING.get(line);
    if (heading) { current = heading; continue; }
    if (boundary(line)) { current = null; continue; }
    if (current === null) continue;
    const m = ITEM_RE.exec(line);
    if (m && m[2]!.trim()) out[current].push(m[2]!.trim());
  }
  return out;
}

function sectionRange(lines: string[], section: Section): { start: number; end: number } | null {
  const start = lines.indexOf(HEADINGS[section]);
  if (start < 0) return null;
  let end = start + 1;
  while (end < lines.length && !boundary(lines[end]!)) end += 1;
  return { start, end };
}

export interface Appended {
  body: string;
  added: boolean;
  /** The number of the item that now holds the text. */
  n: number;
}

/** Append `text` to a section: fill the empty `1. ` first, else the next number; nothing when the section holds
 *  the same text already; a missing heading is added at its template position. Throws Invalid / too_large text. */
export function appendItem(body: string, section: Section, rawText: string): Appended {
  const text = cleanItem(rawText);
  const lines = body.replace(/\r\n?/g, "\n").split("\n");
  let range = sectionRange(lines, section);
  if (!range) {
    // Insert before the first later section that exists in the file, else at the end.
    const at = SECTIONS.slice(SECTIONS.indexOf(section) + 1).map((s) => lines.indexOf(HEADINGS[s])).find((i) => i >= 0);
    if (at === undefined) {
      while (lines.length && lines[lines.length - 1] === "") lines.pop();
      lines.push("", HEADINGS[section]);
      range = { start: lines.length - 1, end: lines.length };
    } else {
      lines.splice(at, 0, HEADINGS[section], "");
      range = { start: at, end: at + 1 };
    }
  }
  const items: { line: number; n: number; text: string }[] = [];
  for (let i = range.start + 1; i < range.end; i++) {
    const m = ITEM_RE.exec(lines[i]!);
    if (m) items.push({ line: i, n: Number(m[1]), text: m[2]!.trim() });
  }
  const same = items.find((x) => x.text === text);
  if (same) return { body, added: false, n: same.n };
  const empty = items.find((x) => x.text === "");
  let n: number;
  if (empty) {
    lines[empty.line] = `${empty.n}. ${text}`;
    n = empty.n;
  } else {
    n = items.reduce((m, x) => Math.max(m, x.n), 0) + 1;
    const last = items.length ? items[items.length - 1]!.line : range.start;
    lines.splice(last + 1, 0, `${n}. ${text}`);
  }
  let out = lines.join("\n");
  if (!out.endsWith("\n")) out += "\n";
  if (new TextEncoder().encode(out).length > DOC_BODY_MAX) throw new Invalid(`the project file would pass ${DOC_BODY_MAX} bytes`);
  return { body: out, added: true, n };
}
