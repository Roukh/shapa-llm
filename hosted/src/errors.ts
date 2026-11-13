// Errors cross the Durable Object boundary as plain data: Durable Object RPC does not carry
// custom Error properties, so every store method returns `Out<T>` and the Worker side unwraps
// it back into an `ApiError`.

export type ErrorCode = "invalid" | "unauthorized" | "forbidden" | "not_found" | "conflict" | "too_large" | "rate_limited" | "unavailable";

const STATUS: Record<ErrorCode, number> = {
  invalid: 400, unauthorized: 401, forbidden: 403, not_found: 404,
  conflict: 409, too_large: 413, rate_limited: 429, unavailable: 503,
};

export class ApiError extends Error {
  readonly status: number;
  constructor(readonly code: ErrorCode, message: string, readonly extra?: Record<string, unknown>, readonly headers?: Record<string, string>) {
    super(message);
    this.status = STATUS[code];
  }
}

export type Out<T> =
  | { ok: true; value: T }
  | { ok: false; code: ErrorCode; message: string; extra?: Record<string, unknown>; headers?: Record<string, string> };

export function fail(code: ErrorCode, message: string, extra?: Record<string, unknown>): never {
  throw new ApiError(code, message, extra);
}

export function run<T>(fn: () => T): Out<T> {
  try {
    return { ok: true, value: fn() };
  } catch (e) {
    if (e instanceof ApiError) {
      const out: Out<T> = { ok: false, code: e.code, message: e.message };
      if (e.extra) out.extra = e.extra;
      if (e.headers) out.headers = e.headers;
      return out;
    }
    throw e;
  }
}

/** Unwrap a store result on the Worker side. */
export async function unwrap<T>(p: Promise<Out<T>> | Out<T>): Promise<T> {
  const r = await p;
  if (r.ok) return r.value;
  throw new ApiError(r.code, r.message, r.extra, r.headers);
}
