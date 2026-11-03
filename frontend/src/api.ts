export class ApiError extends Error {
  status: number;
  code?: string;
  retryable: boolean;
  retryAfterSeconds: number;
  constructor(message: string, status: number, code?: string, retryable = [429, 502, 503, 504].includes(status), retryAfterSeconds = 0) {
    super(message); this.status = status; this.code = code; this.retryable = retryable; this.retryAfterSeconds = retryAfterSeconds;
  }
}

async function rawApi<T>(url: string, init?: RequestInit): Promise<T> {
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

export type ActivityStep = { path: string; method?: string; body: Record<string, unknown>; id?: number; album?: string };
export type ActivityJob = { created_at?: number; finished_at?: number; id: string; label: string; scope: string; href: string; status: string; stage: string; stage_done?: number; stage_total?: number; stage_unit?: string; current?: string; processed: number; total: number; rows: any[]; result?: any; http_status?: number; error?: string; resume_at?: number; mode?: string; inbox_id?: string; steps?: ActivityStep[] };
export let activityEnabled = false;
export function configureActivity(enabled: boolean) { activityEnabled = enabled; }
export const activityRunning = (job: ActivityJob) => ["queued", "running", "waiting", "stopping"].includes(job.status);
export async function startActivity(steps: ActivityStep[], mode = "operation") {
  return rawApi<ActivityJob>("/api/activity", { method: "POST", body: JSON.stringify({ request_id: crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, "0")).join(""), steps, mode }) });
}
export async function watchActivity(id: string, update?: (job: ActivityJob) => void): Promise<ActivityJob> {
  let missed = 0;
  while (true) {
    try {
      const job = await rawApi<ActivityJob>(`/api/activity/${id}`);
      missed = 0; update?.(job);
      if (!activityRunning(job)) return job;
    } catch (reason) {
      if (++missed > 20) throw new Error(`Status unavailable. Check Activity before retrying. ${(reason as Error).message}`);
    }
    await new Promise(resolve => setTimeout(resolve, 500));
  }
}
export function activityPath(path: string, init?: RequestInit) {
  return activityEnabled && ["POST", "PATCH", "DELETE"].includes(init?.method || "GET") && typeof init?.body === "string"
    && /^\/api\/(inbox|library-import|library|imports)\//.test(path)
    && !/\/candidates$/.test(path) && !/^\/api\/(?:inbox|library-import)\/albums\/\d+$/.test(path); // Review-choice saves and matching batches retain their caller contracts.
}

export async function api<T>(url: string, init?: RequestInit): Promise<T> {
  if (!activityPath(url, init)) return rawApi<T>(url, init);
  const job = await startActivity([{ path: url, method: init?.method, body: JSON.parse(String(init?.body)) }]);
  const result = await watchActivity(job.id);
  if ((result.http_status || 200) >= 400 || ["failed", "interrupted"].includes(result.status)) {
    throw new ApiError(result.error || result.result?.error || "Operation could not be confirmed. Check Activity before retrying.", result.http_status || 500, result.result?.code);
  }
  return result.result as T;
}
