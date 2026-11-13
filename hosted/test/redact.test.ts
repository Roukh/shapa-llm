import { describe, expect, it } from "vitest";
import { PATTERNS, PLACEHOLDER, redact, scrub } from "../src/redact.js";
import vectors from "./redact-vectors.json";

interface Vector {
  name: string;
  parts: string[];
  expected: string;
  hits: number;
}

// The fixture was produced by running shapa/redact.py over the same inputs, so every case below
// is a parity check against the Python original. Inputs are joined from parts at run time.
describe("redact: parity with shapa/redact.py", () => {
  for (const v of vectors as Vector[]) {
    it(v.name, () => {
      const [out, hits] = scrub(v.parts.join(""));
      expect(out).toBe(v.expected);
      expect(hits).toBe(v.hits);
    });
  }

  it("covers every pattern of the original set", () => {
    expect(PATTERNS.map((p) => p.name)).toEqual([
      "pem-private-key", "anthropic-key", "openai-key", "stripe-key", "stripe-webhook", "github-token",
      "gitlab-token", "slack-token", "aws-access-key", "google-api-key", "huggingface-token", "npm-token",
      "supabase-key", "resend-key", "jwt", "url-password", "bearer", "basic-auth", "credential-assignment",
      "conn-string-password",
    ]);
  });

  it("is idempotent and never matches its own placeholder", () => {
    const once = redact(["Bearer abcdefghij.klm", "n-opq and AKIA", "ABCDEFGHIJKLMNOP"].join(""));
    expect(redact(once)).toBe(once);
    expect(scrub(PLACEHOLDER)).toEqual([PLACEHOLDER, 0]);
  });

  it("masks only the secret group, keeping the context", () => {
    const [out] = scrub(["postgres://user:", "hunter2secret", "@host/db"].join(""));
    expect(out).toBe("postgres://user:[REDACTED]@host/db");
  });
});
