// The personal-data gate, a line-for-line twin of shapa/pii.py: personal and sensitive information never
// enters stage 0, and no AI app (a REST or MCP write, a sync push from a device that is not the operator's
// own machine) puts it into any wiki. redact.ts only MASKS secrets; this REFUSES.
//
// `check` throws `Rejected` naming the categories found (never the matched value) and returns the warnings
// of a write it lets through. The pattern table is hosted/src/pii-patterns.json, a byte copy of
// shapa/assets/pii-patterns.json (tests/test_parity.py holds them equal); test/pii-cases.json is the fixture
// both test suites replay. The patterns run with the `u` flag, whose \d \w \s \b are ASCII like Python's
// re.ASCII; the text is normalised first (invisible characters, NFKC, non-ASCII digits, Unicode spaces) and
// its decoded forms (entities, %XX and \uXXXX escapes, "[at]" spelling, base64 text) are scanned too.

import { scrub } from "./redact.js";
import table from "./pii-patterns.json";

export const FULL = "full";
export const OPERATOR = "operator";
export type PiiLevel = typeof FULL | typeof OPERATOR;
export const STAGE0_TAGS: readonly string[] = ["stage:0", "stage-proposed:0"];

interface PatSpec { re: string; flags?: string; check?: string; allow?: string[]; allow_flags?: string }
interface PairSpec { marker: string; terms: string; flags?: string }
interface CatSpec { name: string; label: string; operator?: boolean; patterns?: PatSpec[]; pairs?: PairSpec[] }
interface Table {
  normalize: { invisible: string; spaces: string; dashes: string; dots: string; entities: Record<string, string>; limits: { base64_runs: number; base64_min: number; decode_rounds: number } };
  street_stop: string[];
  categories: CatSpec[];
  warn: CatSpec[];
}
const TABLE = table as unknown as Table;
const N = TABLE.normalize;

/** An ASCII-class pattern: the `u` flag, a global clone to search from an offset. */
const compile = (source: string, flags = ""): RegExp => new RegExp(source.replaceAll("(?P<", "(?<"), `${flags.includes("i") ? "i" : ""}gu`);

const INVISIBLE = new RegExp(`[${N.invisible}]`, "gu");
const SPACES = new RegExp(`[${N.spaces}]`, "gu");
const DASHES = new RegExp(`[${N.dashes}]`, "gu");
const DOTS = new RegExp(`[${N.dots}]`, "gu");
// eslint-disable-next-line no-control-regex
const CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]/g;
const NON_ASCII = /[^\x00-\x7f]+/gu;
const ENTITY = /&(?:#(\d{1,7})|#[xX]([0-9A-Fa-f]{1,6})|([A-Za-z]{2,8}));?/g;
const PERCENT = /%([0-9A-Fa-f]{2})/g;
const ESCAPE = /\\u([0-9A-Fa-f]{4})|\\x([0-9A-Fa-f]{2})/g;
const AT = /\s*[[({]\s*at\s*[\])}]\s*/gi;
const DOT = /\s*[[({]\s*dot\s*[\])}]\s*/gi;
const B64_RUN = new RegExp(`[A-Za-z0-9+/_-]{${N.limits.base64_min},}={0,2}`, "g");
const HEX_RUN = /(?:[0-9A-Fa-f]{2}){8,}/g;
const STOP = new Set(TABLE.street_stop);
const isAscii = (s: string): boolean => /^[\x00-\x7f]*$/.test(s);
const isNd = (cp: number): boolean => cp >= 0 && /^\p{Nd}$/u.test(String.fromCodePoint(cp));

function digitValue(cp: number): number {
  let zero = cp;
  while (isNd(zero - 1)) zero--;
  return (cp - zero) % 10;
}

function foldNonAscii(chunk: string): string {
  let out = "";
  for (const ch of chunk) {
    if (/^\p{Mn}$/u.test(ch)) continue;
    out += /^\p{Nd}$/u.test(ch) ? String(digitValue(ch.codePointAt(0)!)) : ch;
  }
  return out;
}

/** The text as the patterns see it. Pure ASCII skips the Unicode steps. */
export function normalize(input: string): string {
  let text = input;
  if (!isAscii(text)) {
    text = text.replace(INVISIBLE, "");
    text = text.normalize("NFKC");
    text = text.replace(INVISIBLE, "");
    text = text.replace(SPACES, " ");
    text = text.replace(DASHES, "-");
    text = text.replace(DOTS, ".");
    text = text.replace(NON_ASCII, (m) => foldNonAscii(m));
  }
  return text.replace(CONTROL, "");
}

const chr = (n: number): string | null => {
  try { return String.fromCodePoint(n); } catch { return null; }
};

function decodeLayer(input: string): string {
  let text = input;
  if (text.includes("&")) {
    text = text.replace(ENTITY, (whole, dec: string | undefined, hex: string | undefined, name: string | undefined) => {
      if (dec !== undefined) return chr(parseInt(dec, 10)) ?? whole;
      if (hex !== undefined) return chr(parseInt(hex, 16)) ?? whole;
      return N.entities[(name ?? "").toLowerCase()] ?? whole;
    });
  }
  if (text.includes("%")) {
    text = text.replace(PERCENT, (whole, h: string) => {
      const n = parseInt(h, 16);
      return n >= 0x20 && n < 0x7f ? String.fromCharCode(n) : whole;
    });
  }
  if (text.includes("\\")) text = text.replace(ESCAPE, (_w, u: string | undefined, x: string | undefined) => String.fromCharCode(parseInt((u ?? x)!, 16)));
  return text;
}

function printableText(bytes: Uint8Array): string | null {
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes);
  } catch {
    return null;
  }
  const chars = Array.from(text);
  if (chars.length < 6) return null;
  const good = chars.filter((c) => c === " " || "\n\t\r".includes(c) || !/^[\p{C}\p{Z}]$/u.test(c)).length;
  return good / chars.length >= 0.95 ? text : null;
}

function base64Texts(text: string): string[] {
  const out: string[] = [];
  for (const m of Array.from(text.matchAll(B64_RUN)).slice(0, N.limits.base64_runs)) {
    let run = m[0].replace(/=+$/, "").replaceAll("-", "+").replaceAll("_", "/");
    if (run.length % 4 === 1) continue;
    run += "=".repeat((4 - (run.length % 4)) % 4);
    let bin: string;
    try {
      bin = atob(run);
    } catch {
      continue;
    }
    const shown = printableText(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
    if (shown) out.push(shown);
  }
  for (const m of Array.from(text.matchAll(HEX_RUN)).slice(0, N.limits.base64_runs)) {
    const shown = printableText(Uint8Array.from(m[0].match(/../g)!, (h) => parseInt(h, 16)));
    if (shown) out.push(shown);
  }
  return out;
}

function views(text: string, depth = 0): string[] {
  const base = normalize(text);
  const seen: string[] = [base];
  const add = (v: string): void => { if (v && !seen.includes(v)) seen.push(v); };
  let cur = base;
  for (let i = 0; i < N.limits.decode_rounds; i++) {
    const next = normalize(decodeLayer(cur));
    if (next === cur) break;
    cur = next;
    add(cur);
  }
  for (const v of [...seen]) {
    if (v.includes("[") || v.includes("(") || v.includes("{")) {
      const spelled = v.replace(AT, "@").replace(DOT, ".");
      if (spelled !== v) add(spelled);
    }
  }
  if (depth < 2) for (const d of base64Texts(base)) for (const v of views(d, depth + 1)) add(v);
  return seen;
}

// --- validators ----------------------------------------------------------------------------------------

function luhnOk(digits: string): boolean {
  if (digits.length < 13 || digits.length > 19) return false;
  const first = digits[0]!;
  if (!("3456".includes(first) || (first === "2" && +digits.slice(0, 4) >= 2221 && +digits.slice(0, 4) <= 2720))) return false;
  let total = 0;
  for (let i = 0; i < digits.length; i++) {
    let d = digits.charCodeAt(digits.length - 1 - i) - 48;
    if (i % 2) { d *= 2; if (d > 9) d -= 9; }
    total += d;
  }
  return total % 10 === 0;
}

function ibanOk(text: string): boolean {
  const s = text.replace(/[ .-]/g, "");
  if (s.length < 15 || s.length > 34) return false;
  let r = 0;
  for (const ch of s.slice(4) + s.slice(0, 4)) {
    const v = ch >= "0" && ch <= "9" ? ch.charCodeAt(0) - 48 : ch.charCodeAt(0) - 55;
    r = v < 10 ? (r * 10 + v) % 97 : (r * 100 + v) % 97;
  }
  return r === 1;
}

const NOT_GLOBAL: ReadonlyArray<readonly [readonly [number, number, number, number], number]> = [
  [[0, 0, 0, 0], 8], [[10, 0, 0, 0], 8], [[100, 64, 0, 0], 10], [[127, 0, 0, 0], 8], [[169, 254, 0, 0], 16],
  [[172, 16, 0, 0], 12], [[192, 0, 0, 0], 24], [[192, 0, 2, 0], 24], [[192, 88, 99, 0], 24], [[192, 168, 0, 0], 16],
  [[198, 18, 0, 0], 15], [[198, 51, 100, 0], 24], [[203, 0, 113, 0], 24], [[224, 0, 0, 0], 4], [[240, 0, 0, 0], 4],
];

const ipInt = (o: readonly number[]): number => (((o[0]! * 256 + o[1]!) * 256 + o[2]!) * 256 + o[3]!);

function publicIpv4(text: string): boolean {
  const parts = text.split(".");
  if (parts.length !== 4 || parts.some((p) => p.length > 1 && p[0] === "0")) return false;
  const octets = parts.map(Number);
  if (octets.some((o) => o > 255) || Math.max(...octets) <= 20) return false;
  const n = ipInt(octets);
  for (const [base, bits] of NOT_GLOBAL) {
    const shift = 2 ** (32 - bits);
    if (Math.floor(n / shift) === Math.floor(ipInt(base) / shift)) return false;
  }
  return true;
}

function geoOk(text: string): boolean {
  const i = text.indexOf(",");
  return !(Math.abs(Math.trunc(parseFloat(text.slice(0, i)))) === 0 && Math.abs(Math.trunc(parseFloat(text.slice(i + 1)))) === 0);
}

function streetOk(text: string, start: number): boolean {
  const m = /([A-Za-z]+)\W*$/.exec(text.slice(Math.max(0, start - 24), start));
  return !(m && STOP.has(m[1]!.toLowerCase()));
}

function valid(check: string | undefined, m: RegExpExecArray, text: string): boolean {
  switch (check) {
    case undefined: return true;
    case "luhn": return luhnOk(m[0].replace(/\D/g, ""));
    case "iban": return ibanOk(m[0]);
    case "gov-digits": return (m[1] ?? "").replace(/\D/g, "").length >= 5;
    case "geo": return geoOk(m[0]);
    case "street": return streetOk(text, m.index);
    case "public-ipv4": return publicIpv4(m[0]);
    default: throw new Error(`unknown check ${check}`);
  }
}

// --- the compiled table --------------------------------------------------------------------------------

interface Pat { rx: RegExp; check: string | undefined; allow: RegExp[]; hasV: boolean }
interface Cat { name: string; label: string; operator: boolean; pats: Pat[]; pairs: Array<[RegExp, RegExp]> }

const build = (e: CatSpec): Cat => ({
  name: e.name,
  label: e.label,
  operator: e.operator === true,
  pats: (e.patterns ?? []).map((p) => ({
    rx: compile(p.re, p.flags),
    check: p.check,
    allow: (p.allow ?? []).map((a) => compile(a, p.allow_flags)),
    hasV: p.re.includes("(?P<v>"),
  })),
  pairs: (e.pairs ?? []).map((p): [RegExp, RegExp] => [compile(p.marker, p.flags), compile(p.terms, p.flags)]),
});

export const CATEGORIES: readonly Cat[] = TABLE.categories.map(build);
const WARNINGS: readonly Cat[] = TABLE.warn.map(build);
export const LABELS: Record<string, string> = Object.fromEntries([...CATEGORIES, ...WARNINGS].map((c) => [c.name, c.label]));

/** A text with more candidate matches than this for one pattern is a dump of card-shaped digit groups (or of
 *  allowed addresses), not a note: refused outright, which also keeps the gate linear on hostile input. */
export const MAX_CANDIDATES = 4000;

function hit(p: Pat, text: string): boolean {
  let pos = 0;
  let tried = 0;
  while (pos <= text.length) {
    p.rx.lastIndex = pos;
    const m = p.rx.exec(text);
    if (m === null) return false;
    tried += 1;
    if (tried > MAX_CANDIDATES && (p.check !== undefined || p.allow.length > 0)) return true;
    if (p.allow.length) {
      const value = p.hasV ? (m.groups?.["v"] ?? "") : m[0];
      if (p.allow.some((a) => { a.lastIndex = 0; return a.test(value); })) { pos = m.index + m[0].length; continue; }
    }
    if (valid(p.check, m, text)) return true;
    pos = m.index + 1;
  }
  return false;
}

function categoryHit(cat: Cat, view: string): boolean {
  if (cat.pats.some((p) => hit(p, view))) return true;
  for (const [marker, terms] of cat.pairs) {
    for (const line of view.split("\n")) {
      marker.lastIndex = 0;
      terms.lastIndex = 0;
      if (marker.test(line) && terms.test(line)) return true;
    }
  }
  return false;
}

// --- the gate ------------------------------------------------------------------------------------------

export interface Scan { blocked: string[]; warned: Record<string, number> }

function fieldViews(texts: readonly unknown[]): { all: string[]; lines: string[] } {
  const fields = texts.filter((t) => t !== null && t !== undefined && t !== "").map(String);
  if (!fields.length) return { all: [], lines: [] };
  const lines = views(fields.join("\n"));
  const all = [...lines];
  // a value split across two fields: joined by a space, and run together
  if (fields.length > 1) for (const j of [fields.join(" "), fields.join("")]) for (const v of views(j)) if (!all.includes(v)) all.push(v);
  return { all, lines };
}

/** What the gate finds in `texts` (the fields of one write) at `level`. Never returns a value. */
export function scan(texts: readonly unknown[], level: PiiLevel = FULL): Scan {
  const { all, lines } = fieldViews(texts);
  const out: Scan = { blocked: [], warned: {} };
  for (const cat of CATEGORIES) {
    if (level === OPERATOR && !cat.operator) continue;
    const pool = cat.pairs.length ? lines : all;
    if (cat.name === "secret") {
      if (pool.some((v) => scrub(v)[1] > 0)) out.blocked.push(cat.name);
      continue;
    }
    if (pool.some((v) => categoryHit(cat, v))) out.blocked.push(cat.name);
  }
  if (!out.blocked.length) {
    for (const cat of WARNINGS) {
      const n = lines.filter((v) => categoryHit(cat, v)).length;
      if (n) out.warned[cat.name] = n;
    }
  }
  return out;
}

/** A write refused for personal or sensitive data. `categories` are names, never values. */
const REFUSAL_TAIL = "Personal and sensitive data never goes into the wiki: remove it, or say what you mean without the value.";
export class Rejected extends Error {
  constructor(readonly categories: string[]) {
    super(`refused: the text looks like it holds ${categories.map((c) => LABELS[c] ?? c).join(", ")}. ${REFUSAL_TAIL}`);
  }
}

export const warningsOf = (found: Scan): string[] =>
  Object.keys(found.warned).map((name) => `possible personal data (${LABELS[name] ?? name}): allowed, but check it is not about a person`);

/** Throw `Rejected` when `texts` hold blocked data at `level`; else the warnings (possibly none), logged as a count. */
export function check(texts: readonly unknown[], level: PiiLevel = FULL): string[] {
  const found = scan(texts, level);
  if (found.blocked.length) throw new Rejected(found.blocked);
  const n = Object.values(found.warned).reduce((a, b) => a + b, 0);
  if (n) console.log(JSON.stringify({ event: "pii-warn", count: n }));
  return warningsOf(found);
}

export const isStage0 = (tags: readonly unknown[] | undefined): boolean =>
  (tags ?? []).some((t) => STAGE0_TAGS.includes(String(t ?? "").split(/\s+/).filter(Boolean).join(" ").toLowerCase()));

/** FULL for every writer but the operator's own device, and for ANY write that goes to stage 0. */
export const levelFor = (tags: readonly unknown[] | undefined, privileged: boolean): PiiLevel => (privileged && !isStage0(tags) ? OPERATOR : FULL);
