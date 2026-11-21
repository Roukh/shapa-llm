// Matching a task to skills: BM25 over each skill's name (counted twice) and description. The
// tokenizer, the stop words and the formula are the engine's (shapa/bm25.py); tests/test_skill_match.py
// holds the constants equal. Pure functions: no store, no clock.

export const MATCH_MAX = 6;          // skills one match returns, whatever the caller asks
export const MATCH_TEXT_MAX = 2000;  // characters of a task read

const K1 = 1.5;
const B = 0.75;
const STOP = new Set([
  "the", "and", "for", "with", "that", "this", "are", "but", "not", "you",
  "its", "from", "into", "then", "they", "have", "has", "was", "will", "can",
  "use", "uses", "used", "when", "where", "which", "what", "how", "any", "all",
  "should", "would", "could", "must", "may", "might", "shall",
]);

export function words(text: string): string[] {
  return (text.toLowerCase().match(/[a-z][a-z0-9]{2,}/g) ?? []).filter((w) => !STOP.has(w));
}

export interface Candidate { name: string; description: string; wiki: string }
export interface SkillMatch extends Candidate { score: number; reason: string }

/** The skills that best fit a task, best first, at most MATCH_MAX. A task sharing no word with any skill
 *  returns nothing: never a guess. Each match names the task words it matched. */
export function matchSkills(candidates: readonly Candidate[], text: string, limit: number = MATCH_MAX): SkillMatch[] {
  const task = text.slice(0, MATCH_TEXT_MAX);
  const query = words(task);
  if (query.length === 0 || candidates.length === 0) return [];
  const q = new Set(query);
  const docs = candidates.map((c) => ({ c, toks: words(`${c.name} ${c.name} ${c.description}`) }));
  const n = docs.length;
  const avgdl = docs.reduce((s, d) => s + d.toks.length, 0) / n || 1;
  const df = new Map<string, number>();
  const tfs = docs.map((d) => {
    const tf = new Map<string, number>();
    for (const t of d.toks) tf.set(t, (tf.get(t) ?? 0) + 1);
    for (const t of tf.keys()) if (q.has(t)) df.set(t, (df.get(t) ?? 0) + 1);
    return tf;
  });
  const out: SkillMatch[] = [];
  docs.forEach((d, i) => {
    const tf = tfs[i]!;
    const dl = d.toks.length || 1;
    let score = 0;
    for (const t of q) {
      const f = tf.get(t) ?? 0;
      const dfT = df.get(t) ?? 0;
      if (f === 0 || dfT === 0) continue;
      const idf = Math.log((n - dfT + 0.5) / (dfT + 0.5) + 1);
      score += (idf * (f * (K1 + 1))) / (f + K1 * (1 - B + (B * dl) / avgdl));
    }
    if (score <= 0) return;
    const have = new Set(d.toks);
    const terms = [...new Set(query)].filter((w) => have.has(w)).slice(0, 6);
    out.push({ ...d.c, score: Math.round(score * 1e4) / 1e4, reason: `matches: ${terms.join(", ")}` });
  });
  out.sort((a, b) => b.score - a.score || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  return out.slice(0, Math.max(1, Math.min(Math.trunc(limit) || MATCH_MAX, MATCH_MAX)));
}
