import { runInboxBatch } from "./allInboxes";
import { useEffect, useRef, useState } from "react";
import { ActivityProgress } from "./ActivityProgress";
import { api, activityRunning, type ActivityJob } from "./api";
import { useToast } from "./toastContext";

type Preview = { preview_id: string; fingerprint: string; files: { path: string; location: string }[]; count: number; albums?: string[] };
function folderFor(path: string) {
  const parts = path.split("/").slice(0, -1);
  if (parts.length > 1 && /^(cd|disc|disk)[ ._-]*[0-9]+$/i.test(parts[parts.length - 1])) parts.pop();
  return parts.join("/");
}
function folderCounts(files: Preview["files"]) {
  const albumFolders = [...new Set(files.filter(file => file.location === "Inbox").map(file => folderFor(file.path)))].sort((a, b) => b.length - a.length);
  const counts = new Map<string, number>();
  for (const file of files) {
    const folder = albumFolders.find(folder => folder && file.path.startsWith(`${folder}/`)) ?? folderFor(file.path);
    counts.set(folder, (counts.get(folder) || 0) + 1);
  }
  return [...counts].sort(([a], [b]) => a.localeCompare(b));
}

type Action = { kind: "undo" | "delete-imported" | "delete-albums" | "delete-track"; jobs?: number[]; albumIds?: number[]; albumId?: number; trackId?: number; names?: string[] };
export type InboxActionSelection = Action & { groups?: Array<Action & { inboxId: string; inboxName: string }> };
type Plan = { inboxId: string; inboxName: string; preview: Preview };
export function InboxActionDialog({ inboxId, kind, groups, jobs, albumIds, albumId, trackId, names, inline = false, onActivityChange, onClose, onDone }: InboxActionSelection & { inboxId: string; inline?: boolean; onActivityChange?: (id: string | undefined) => void; onClose: () => void; onDone: (complete: boolean) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const checkId = useRef<string | undefined>(undefined);
  const [plans, setPlans] = useState<Plan[]>([]);
  const [preview, setPreview] = useState<Preview | null>(null), [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [activity, setActivity] = useState<ActivityJob | null>(null);
  const notify = useToast();
  const unimported = kind === "delete-albums" || kind === "delete-track";
  const grouped = kind === "delete-albums" || kind === "delete-imported";
  const folders = preview && grouped ? plans.length ? plans.flatMap(plan => folderCounts(plan.preview.files).map(([folder, count]) => [`${plan.inboxName} · ${folder || "Inbox root"}`, count] as [string, number])) : folderCounts(preview.files) : [];
  const [remaining, setRemaining] = useState<{path: string; location: string}[]>([]);
  const base = `/api/inbox/${encodeURIComponent(inboxId)}/actions`;
  useEffect(() => {
    const node = ref.current;
    node?.showModal();
    let current = true;
    const body = (selection: Action) => selection.kind === "delete-albums" ? { kind: selection.kind, album_ids: selection.albumIds } : selection.kind === "delete-track" ? { kind: selection.kind, album_id: selection.albumId, track_id: selection.trackId } : { kind: selection.kind, job_ids: selection.jobs };
    const progress = (job: ActivityJob) => { if (current) { setActivity(job); checkId.current = job.id; } };
    const load = async () => {
      if (!groups) return api<Preview>(`${base}/preview`, { method: "POST", body: JSON.stringify(body({ kind, albumIds, albumId, trackId, jobs })) }, progress);
      if (!groups.length) throw new Error("No Inbox files selected. Reload the queue.");
      const rows = await runInboxBatch(groups.map(group => ({ id: group.inboxId, album: group.inboxName, path: `/api/inbox/${encodeURIComponent(group.inboxId)}/actions/preview`, body: body(group) })), progress);
      const prepared = groups.map(group => {
        const row = rows.find(row => row.id === group.inboxId);
        if (!row?.result.preview_id || row.http_status >= 400) throw new Error(`${group.inboxName}: ${row?.result.error || "Preview unavailable"}`);
        return { inboxId: group.inboxId, inboxName: group.inboxName, preview: row.result as Preview };
      });
      if (current) setPlans(prepared);
      return { ...prepared[0].preview, count: prepared.reduce((sum, plan) => sum + plan.preview.count, 0),
        files: prepared.flatMap(plan => plan.preview.files.map(file => ({ ...file, path: `${plan.inboxName} · ${file.path}` }))) };
    };
    void load().then(value => { if (current) setPreview(value); }).catch(reason => { if (current) setError(reason.message); });
    return () => { current = false; node?.close(); };
  }, [base, kind, groups, jobs, albumIds, albumId, trackId]);
  useEffect(() => {
    onActivityChange?.(activity && activityRunning(activity) ? activity.id : undefined);
    return () => onActivityChange?.(undefined);
  }, [activity, onActivityChange]);
  function finish(result: any) {
    setRemaining(result.remaining || []);
    if (result.status !== "complete") { setError(result.message + " " + (result.errors || []).map((row: any) => row.error).join("; ")); setPreview(null); onDone(false); }
    else { notify(result.message); onDone(true); onClose(); }
  }
  async function applyGroups() {
    let results: any[] = [];
    try {
      const rows = await runInboxBatch(plans.map(plan => ({ id: plan.inboxId, album: plan.inboxName,
        path: `/api/inbox/${encodeURIComponent(plan.inboxId)}/actions/apply`,
        body: { preview_id: plan.preview.preview_id, expected_fingerprint: plan.preview.fingerprint, confirmed: true } })), setActivity, checkId.current);
      results = plans.map(plan => rows.find(row => row.id === plan.inboxId)?.result);
      if (results.some(result => !["complete", "partial"].includes(result?.status))) throw new Error("Read saved results");
    } catch {
      // Recover each real receipt, never repeat a destructive batch after a lost response.
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try { results = await Promise.all(plans.map(async plan => {
        try {
          const saved = await api<any>(`/api/inbox/${encodeURIComponent(plan.inboxId)}/actions/${encodeURIComponent(plan.preview.preview_id)}`, { signal: controller.signal });
          return { ...saved.result, status: saved.status };
        } catch { return { status: "unknown", message: "Result uncertain; check Activity and saved files before another action." }; }
      })); } finally { clearTimeout(timeout); }
    }
    const complete = results.every(result => result.status === "complete");
    finish({ status: complete ? "complete" : "partial", message: complete ? "Inbox actions completed." : "Some Inbox files need attention.",
      errors: results.flatMap((result, index) => result.status === "complete" ? [] : [{ error: `${plans[index].inboxName}: ${result.message || result.status}` }]),
      remaining: results.flatMap((result, index) => result.status === "complete" ? [] : (result.remaining || plans[index].preview.files).map((file: any) => ({ ...file, path: `${plans[index].inboxName} · ${file.path}` }))) });
  }
  async function apply() {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      if (plans.length) { await applyGroups(); return; }
      finish(await api<any>(`${base}/apply`, { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id, expected_fingerprint: preview.fingerprint, confirmed: true }) }, setActivity, checkId.current));
    } catch (reason) {
      // A lost response must never resubmit deletion. Read the durable result.
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const saved = await api<any>(`${base}/${encodeURIComponent(preview.preview_id)}`, { signal: controller.signal });
        if (["complete", "partial"].includes(saved.status)) finish({ ...saved.result, status: saved.status });
        else { setError(saved.status === "running" ? "This action is still running. Close this confirmation and refresh Inbox to check its result." : `${(reason as Error).message}. Refresh Inbox before trying another action.`); setPreview(null); }
      } catch { setError(`${(reason as Error).message}. The result is uncertain; refresh Inbox before trying another action.`); setPreview(null); }
      finally { clearTimeout(timeout); }
    } finally { setBusy(false); }
  }
  const title = kind === "undo" ? "Undo import" : unimported
    ? kind === "delete-track" ? "Delete track from Inbox" : "Delete from Inbox"
    : "Remove imported originals";
  const content = <>
    <div className="dialog-body">{inline && <h3>{title}</h3>}<p>{kind === "undo" ? "Remove only the Library copies created by this import and their Beets records. Inbox originals stay unchanged." : unimported ? kind === "delete-track" ? "Delete this track without importing? Other files and your Library stay unchanged. This is permanent." : "Delete these albums and accompanying files without importing? Library files stay unchanged. This is permanent." : "Delete these Inbox originals and accompanying files? Library copies stay unchanged. This is permanent."}</p>
      {!grouped && (names || preview?.albums)?.length ? <p>{(names || preview?.albums)!.join(" · ")}</p> : null}
      {activity && activityRunning(activity) ? <ActivityProgress job={activity}/> : !preview && !error ? <p role="status">Checking files…</p> : busy ? <p role="status">Working…</p> : null}
      {preview && <><p>{grouped ? `${folders.length} ${folders.length === 1 ? "folder" : "folders"} · ` : ""}{preview.count} {preview.count === 1 ? "file" : "files"}</p>{grouped ? <>
        <ul className="inbox-delete-folders" aria-label="Folders to clear">{folders.map(([folder, count]) => <li key={folder}><strong>{folder || "Inbox root"}</strong><small>{count} {count === 1 ? "file" : "files"}</small></li>)}</ul>
        <details className="inbox-delete-files"><summary>Show files</summary><ul>{preview.files.map(row => <li key={row.path}>{row.path}</li>)}</ul></details>
      </> : <ul>{preview.files.map(row => <li key={`${row.location}/${row.path}`}><strong>{row.location}</strong> · {row.path}</li>)}</ul>}</>}
      {error && <p role="alert" className="notice error">{error}</p>}
      {remaining.length > 0 && <><p>Files still to check. Close this confirmation, refresh Inbox and select the remaining files before trying again.</p><ul>{remaining.map(row => <li key={`${row.location}/${row.path}`}>{row.path}</li>)}</ul></>}
    </div><footer><button type="button" className="secondary" autoFocus={inline} disabled={busy} onClick={onClose}>{error ? "Close" : "Cancel"}</button><button type="button" className="danger-action" disabled={!preview || busy} onClick={() => void apply()}>{busy ? "Working…" : kind === "undo" ? "Remove Library copies and undo" : unimported ? "Delete permanently" : "Permanently remove Inbox originals"}</button></footer>
  </>;
  if (inline) return content;
  return <dialog ref={ref} className="dialog inbox-action-dialog" aria-labelledby="inbox-action-title" onCancel={event => { if (busy) event.preventDefault(); else onClose(); }}>
    <header><h2 id="inbox-action-title">{title}</h2></header>
    {content}
  </dialog>;
}
