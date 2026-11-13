import { call } from "./helpers.js";

let nextId = 1;

/** One JSON-RPC request to /mcp (the server is stateless, so no session handshake is carried over). */
export async function rpc(token: string | undefined, method: string, params: unknown = {}): Promise<{ status: number; json: any; headers: Headers; text: string }> {
  const headers: Record<string, string> = {
    "content-type": "application/json",
    accept: "application/json, text/event-stream",
    "mcp-protocol-version": "2025-06-18",
    // A real request always carries Host; a request built in a test does not.
    host: "shapa.test",
  };
  if (token) headers["authorization"] = `Bearer ${token}`;
  const res = await call("/mcp", { method: "POST", headers, body: JSON.stringify({ jsonrpc: "2.0", id: nextId++, method, params }) });
  const text = await res.text();
  let json: any = null;
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("text/event-stream")) {
    const data = text.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).filter(Boolean);
    json = data.length ? JSON.parse(data[data.length - 1]!) : null;
  } else {
    try {
      json = JSON.parse(text);
    } catch {
      json = null;
    }
  }
  return { status: res.status, json, headers: res.headers, text };
}

export const INIT = { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "test", version: "1" } };

export async function tool(token: string, name: string, args: Record<string, unknown> = {}): Promise<{ text: string; isError: boolean; raw: any }> {
  const r = await rpc(token, "tools/call", { name, arguments: args });
  if (r.status !== 200 || !r.json) throw new Error(`tools/call ${name}: ${r.status} ${r.text}`);
  const result = r.json.result;
  if (!result) return { text: JSON.stringify(r.json.error), isError: true, raw: r.json };
  return { text: (result.content ?? []).map((c: { text?: string }) => c.text ?? "").join("\n"), isError: !!result.isError, raw: result };
}
