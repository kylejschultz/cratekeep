export class ApiError extends Error {
  status: number;
  code?: string;
  retryable: boolean;
  retryAfterSeconds: number;
  constructor(message: string, status: number, code?: string, retryable = [429, 502, 503, 504].includes(status), retryAfterSeconds = 0) {
    super(message); this.status = status; this.code = code; this.retryable = retryable; this.retryAfterSeconds = retryAfterSeconds;
  }
}

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { ...(init?.body ? { "Content-Type": "application/json" } : {}), ...init?.headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const retryAfter = response.headers?.get("Retry-After") || "";
    const headerSeconds = /^\d+$/.test(retryAfter) ? Number(retryAfter) : Math.max(0, (Date.parse(retryAfter) - Date.now()) / 1000) || 0;
    throw new ApiError(body.error || `Request failed (${response.status})`, response.status, body.code,
      body.retryable ?? [429, 502, 503, 504].includes(response.status), Math.max(Number(body.retry_after_seconds) || 0, headerSeconds));
  }
  return body as T;
}

export function formatBytes(value: number | null | undefined) {
  if (!Number.isFinite(value)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Number(value), index = 0;
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}
