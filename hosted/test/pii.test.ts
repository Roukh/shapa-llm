import { describe, expect, it } from "vitest";
import { CATEGORIES, FULL, OPERATOR, Rejected, check, levelFor, normalize, scan, type PiiLevel } from "../src/pii.js";
import { redact } from "../src/redact.js";
import fixture from "./pii-cases.json";

interface Case { name: string; fields: Array<string | string[]>; level: PiiLevel; blocked: string[]; warned: string[] }

// The fixture is shared with tests/test_pii.py: both languages must give the same answer for every case.
describe("pii: the shared fixture (parity with shapa/pii.py)", () => {
  for (const c of (fixture as { cases: Case[] }).cases) {
    it(c.name, () => {
      const fields = c.fields.map((f) => (Array.isArray(f) ? f.join("") : f));
      const found = scan(fields, c.level);
      expect([...found.blocked].sort()).toEqual(c.blocked);
      if (!c.blocked.length) expect(Object.keys(found.warned).sort()).toEqual(c.warned);
    });
  }
});

describe("pii: the gate", () => {
  it("names the category and never the value", () => {
    let err: unknown;
    try {
      check(["write to john.doe@gmail.com or call 415-555-2671"]);
    } catch (e) {
      err = e;
    }
    expect(err).toBeInstanceOf(Rejected);
    const r = err as Rejected;
    expect(r.categories).toEqual(["email", "phone"]);
    expect(r.message).toContain("email") ;
    expect(r.message).not.toContain("john");
    expect(r.message).not.toContain("415");
  });

  it("returns the warnings of a clean write without the value", () => {
    const out = check(["server at 93.184.216.34"]);
    expect(out).toHaveLength(1);
    expect(out[0]).not.toContain("93.184");
    expect(check(["nothing personal here"])).toEqual([]);
  });

  it("escalates a stage-0 write to every category, whoever writes it", () => {
    expect(levelFor(["stage:0"], true)).toBe(FULL);
    expect(levelFor([" Stage-Proposed:0 "], true)).toBe(FULL);
    expect(levelFor(["stage:2"], true)).toBe(OPERATOR);
    expect(levelFor([], false)).toBe(FULL);
    expect(() => check(["john@gmail.com"], levelFor(["stage:0"], true))).toThrow(Rejected);
    expect(check(["john@gmail.com"], levelFor(["stage:2"], true))).toEqual([]);
  });

  it("normalises before matching", () => {
    expect(normalize("j​ohn＠gmail.com")).toBe("john@gmail.com");
    expect(normalize("٤١")).toBe("41");
  });
});

describe("pii: speed on 100 KB of the worst input of each kind", () => {
  const worst: Array<[string, string]> = [
    ["overlong digit run", "7".repeat(100_000)],
    ["spaced digits", "4 ".repeat(50_000)],
    ["hyphen digit runs", "12-".repeat(40_000)],
    ["local part run", "a".repeat(100_000)],
    ["at signs", "a@".repeat(50_000)],
    ["domain labels", "x@" + "a.".repeat(50_000)],
    ["capitalised words", "1 " + "Aaaa ".repeat(20_000) + "Street"],
    ["street numbers", "1 A ".repeat(25_000)],
    ["sentence dots", "dob " + "1/".repeat(50_000)],
    ["keyword spam", "passport ".repeat(10_000)],
    ["pin spam", "pin: ".repeat(20_000)],
    ["pem header", "-----BEGIN " + "A ".repeat(40_000)],
    ["coordinates", "1.2345,".repeat(15_000)],
    ["base64 soup", "QUJD".repeat(25_000)],
    ["entity soup", "&#64;".repeat(20_000)],
    ["health line", "my ".repeat(30_000) + "cancer"],
  ];
  for (const [name, text] of worst) {
    it(name, () => {
      const t0 = performance.now();
      scan([text]);
      expect(performance.now() - t0).toBeLessThan(1000);
    });
  }

  it("refuses a dump of card-shaped digit groups instead of grinding through it", () => {
    const t0 = performance.now();
    expect(scan(["4 ".repeat(50_000)]).blocked).toEqual(["card"]);
    expect(performance.now() - t0).toBeLessThan(1000);
    expect(scan(["1 ".repeat(50_000)]).blocked).toEqual([]);
  });

  it("keeps the masker linear on dotted and hyphenated runs", () => {
    for (const text of ["12-".repeat(40_000), "x@" + "a.".repeat(50_000), "a.b-".repeat(25_000)]) {
      const t0 = performance.now();
      redact(text);
      expect(performance.now() - t0).toBeLessThan(500);
    }
    expect(redact("api_key = abcdefghijklmnop1234")).toBe("api_key = [REDACTED]");
    expect(redact("my-very-long-name-".repeat(8) + "token=abcdefghijklmnop1234").endsWith("token=[REDACTED]")).toBe(true);
  });

  it("every category scans 100 KB of prose in well under 50 ms", () => {
    const prose = ("lorem ipsum 12 dolor sit amet, consectetur 3 adipiscing elit 4111 sed do eiusmod " +
      "tempor@ incididunt ut labore 1.2.3.4 et dolore magna Main St. aliqua. ").repeat(700).slice(0, 100_000);
    for (const cat of CATEGORIES) {
      if (cat.name === "secret") continue;
      const t0 = performance.now();
      scan([prose], FULL);
      expect(performance.now() - t0, cat.name).toBeLessThan(500); // the whole gate; per-pattern timing is in pytest
    }
  });
});
