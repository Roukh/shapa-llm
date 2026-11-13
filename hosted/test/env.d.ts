// Types the Worker's own exports for `import { exports } from "cloudflare:workers"` in tests.
declare namespace Cloudflare {
  interface GlobalProps {
    mainModule: typeof import("../src/index");
    durableNamespaces: "ShapaStore";
  }
}
