import { api, activityEnabled, startActivity, watchActivity, type ActivityJob, type ActivityStep } from "./api";
import type { Album } from "./types";
import type { InboxActionSelection } from "./InboxImportActions";

export type InboxSource = { id: string; name: string };
export async function runInboxBatch(steps: ActivityStep[], update?: (job: ActivityJob) => void, checkId?: string) {
  if (activityEnabled) {
    const job = await startActivity(steps, "batch", checkId); update?.(job);
    const finished = await watchActivity(job.id, update);
    if (finished.rows.length !== steps.length || finished.rows.some(row => !row.result)) throw new Error("Some Inbox results are unavailable. Check Activity before retrying.");
    return finished.rows;
  }
  const rows = [];
  for (const step of steps) {
    try { rows.push({ id: step.id, result: await api<any>(step.path, { method: "POST", body: JSON.stringify(step.body) }), http_status: 200 }); }
    catch (reason) { rows.push({ id: step.id, result: { error: (reason as Error).message }, http_status: 500 }); }
  }
  return rows;
}
export async function refreshInboxes(inboxes: InboxSource[], cleanup: boolean) {
  const rows = await runInboxBatch(inboxes.map(inbox => ({ id: inbox.id, album: inbox.name, path: `/api/inbox/${encodeURIComponent(inbox.id)}/${cleanup ? "scopes" : "queue"}/refresh`, body: {} })));
  return { inventory: { files: rows.reduce((sum, row) => sum + (row.result.inventory?.files || 0), 0) },
    cleanup: { removed: rows.flatMap(row => row.result.cleanup?.removed || []), errors: rows.flatMap(row => (row.result.cleanup?.errors || []).map((error: any) => ({ ...error, error: `${inboxes.find(inbox => inbox.id === row.id)?.name}: ${error.error}` }))) },
    errors: rows.flatMap(row => row.result.error ? [{ error: `${inboxes.find(inbox => inbox.id === row.id)?.name}: ${row.result.error}` }] : []) };
}
export function groupInboxActions(selection: InboxActionSelection, albums: Album[]) {
  const groups = new Map<string, InboxActionSelection & { inboxId: string; inboxName: string }>();
  for (const album of albums) {
    const id = String(album.source_id || "");
    if (!id) throw new Error("Inbox source is unavailable. Reload before changing files.");
    const group = groups.get(id) || { kind: selection.kind, inboxId: id, inboxName: String(album.inbox_name), albumIds: [], jobs: [], names: [] };
    if (selection.kind === "delete-albums") group.albumIds!.push(album.id);
    else if (selection.kind === "delete-track") { group.albumId = selection.albumId; group.trackId = selection.trackId; }
    else if (selection.jobs?.includes(album.imported?.job_id)) group.jobs!.push(album.imported.job_id);
    group.names!.push(String(album.album)); groups.set(id, group);
  }
  return [...groups.values()].filter(group => selection.kind === "delete-track" || (selection.kind === "delete-albums" ? group.albumIds!.length : group.jobs!.length));
}
