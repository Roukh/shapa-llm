import { cloudflareTest } from "@cloudflare/vitest-plugin";
import { defineConfig } from "vitest/config";

// The tests run inside workerd (miniflare) against the real Worker and Durable Object.
// The owner key is a test-only value injected here; nothing reads a .dev.vars file.
export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: { SHAPA_OWNER_KEY: "test-owner-key-0123456789abcdef0123456789" },
      },
    }),
  ],
  test: {
    include: ["test/**/*.test.ts"],
    testTimeout: 20_000,
  },
});
