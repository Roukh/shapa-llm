import { describe, expect, it } from "vitest";
import { MEMPM_HEAD, MEMPM_KEYS, MEMPM_MAX, mempmBlock, mempmJson, type Mempm } from "../src/instructions.js";

// The same entries and the same expected text as tests/test_parity.py, so the Worker's and the local engine's blocks
// stay one shape. The escapes are built from a backslash and hex digits, never typed.
const B = String.fromCharCode(92);
const LS = String.fromCharCode(0x2028);
const SAMPLE: Mempm = { P: [["R1", "2026-11-20", `say "hi" <b>&</b>${LS}x`]], X: [], M: [["M2", "2026-11-19", 'a]],["b']] };
const EXPECTED =
  `{"P":[["R1","2026-11-20","say ${B}"hi${B}" ${B}u003cb${B}u003e${B}u0026${B}u003c/b${B}u003e${B}u2028x"]],"X":[],` +
  `"M":[["M2","2026-11-19","a]],[${B}"b"]]}`;

describe("the mempm block", () => {
  it("is compact JSON with the keys P, X, M in that order and the markup characters escaped", () => {
    expect(mempmJson(SAMPLE)).toBe(EXPECTED);
    expect(JSON.parse(EXPECTED)).toEqual(SAMPLE);
    expect([...MEMPM_KEYS]).toEqual(["P", "X", "M"]);
    expect(Object.keys(JSON.parse(mempmJson({ M: [], X: [], P: [] })))).toEqual(["P", "X", "M"]); // the key order is the block's, not the object's
  });

  it("cuts the longest list first, from its end, and prints nothing when nothing fits", () => {
    const entries = (n: number): [string, string, string][] => Array.from({ length: n }, (_, i) => [`R${i}`, "2026-11-20", `text number ${i}`]);
    const full: Mempm = { P: entries(MEMPM_MAX.P), X: entries(3), M: entries(MEMPM_MAX.M) };
    const whole = mempmBlock(full, 100_000);
    expect(whole.startsWith(`${MEMPM_HEAD}\n{"P":`)).toBe(true);
    const cut = mempmBlock(full, whole.length - 200);
    const got = JSON.parse(cut.split("\n")[1]!) as Mempm;
    expect(cut.length).toBeLessThanOrEqual(whole.length - 200);
    expect(got.X).toEqual(full.X); // the short list is left alone while a longer one can give way
    expect(got.P).toEqual(full.P.slice(0, got.P.length));
    expect(got.P.length + got.M.length).toBeLessThan(full.P.length + full.M.length);
    expect(mempmBlock(full, MEMPM_HEAD.length + 10)).toBe("");
  });
});
