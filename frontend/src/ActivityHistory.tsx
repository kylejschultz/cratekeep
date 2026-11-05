import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, activityRunning, type ActivityJob, workCount } from "./api";

type Group = { key: string; label: string; scope: string; count: number; active: number; attention: number; finished: number; timestamp: number; latest: ActivityJob };
type Page = { groups?: Group[]; jobs?: ActivityJob[]; clearable: number; snapshot: number; next_before: number | null; next_priority?: number | null };
type Cursor = { before?: number; snapshot?: number; priority?: number };

function Timestamp({ value }: { value?: number }) {
  if (!value) return null;
  const date = new Date(value * 1000);
  return <time dateTime={date.toISOString()} title={date.toLocaleString()}>{date.toLocaleString(undefined, { ...(date.toDateString() === new Date().toDateString() ? {} : { month: "short", day: "numeric" }), hour: "numeric", minute: "2-digit" })}</time>;
}
function stateText(job: ActivityJob) {
  if (activityRunning(job)) return [job.stage || "Working…", workCount(job)].filter(Boolean).join(" · ");
  if (["partial", "failed", "interrupted"].includes(job.status)) return "Needs attention";
  if (job.status === "stopped") return "Stopped";
  if (job.scan_added) return workCount(job);
  if (!job.intent && job.files_found != null) return `Finished · ${workCount(job)}`;
  if (job.is_check) return "Check complete";
  return job.outcome || ({ import: "Imported", metadata: "Saved", move: "Moved", artwork: "Saved", delete: "Deleted", undo: "Undone" }[job.intent || ""] || (job.status === "complete" ? "Finished" : job.status));
}
function feedTitle(item: Group) {
  if (item.latest.is_check || !item.latest.intent) return item.label;
  return ({ import: "Album import", match: "Album matching", metadata: "Metadata changes", move: "File moves", artwork: "Album artwork", delete: "Inbox deletion", undo: "Import undo", "inbox-files": "Inbox files" }[item.latest.intent] || item.label);
}
async function clearHistory(values: { group?: string; ids?: string[] }) {
  await api("/api/activity/history/clear", { method: "POST", body: JSON.stringify(values) });
  window.dispatchEvent(new Event("cratekeep:history-cleared"));
}
function useHistoryPage(enabled: boolean, group?: string) {
  const [data, setData] = useState<Page>();
  const [error, setError] = useState("");
  const [pages, setPages] = useState<Cursor[]>([{}]);
  const [refresh, setRefresh] = useState(0);
  const cursor = pages[pages.length - 1];
  useEffect(() => {
    const reset = () => { setPages([{}]); setRefresh(value => value + 1); };
    window.addEventListener("cratekeep:history-cleared", reset);
    return () => window.removeEventListener("cratekeep:history-cleared", reset);
  }, []);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    setData(undefined);
    const load = async () => {
      try {
        const query = new URLSearchParams();
        if (group) query.set("group", group);
        if (cursor.before != null) query.set("before", String(cursor.before));
        if (cursor.priority != null) query.set("priority", String(cursor.priority));
        if (cursor.snapshot != null) query.set("snapshot", String(cursor.snapshot));
        const next = await api<Page>(`/api/activity/history?${query}`);
        if (!cancelled) { setData(next); setError(""); }
      } catch (reason) { if (!cancelled) setError((reason as Error).message); }
      finally { if (!cancelled) timer = setTimeout(() => void load(), 1500); }
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [enabled, group, cursor, refresh]);
  return { data, error, retry: () => setRefresh(value => value + 1), older: data?.next_before != null ? () => setPages(previous => [...previous, { before: data.next_before!, snapshot: data.snapshot, priority: data.next_priority ?? undefined }]) : undefined,
    newer: pages.length > 1 ? () => setPages(previous => previous.slice(0, -1)) : undefined };
}
function Paging({ older, newer }: { older?: () => void; newer?: () => void }) {
  if (!older && !newer) return null;
  return <nav className="activity-paging" aria-label="Activity history pages"><button type="button" className="text-button" disabled={!newer} onClick={newer}>Newer</button><button type="button" className="text-button" disabled={!older} onClick={older}>Older</button></nav>;
}
function RunHistory({ group, onClose, onOpen }: { group: Group; onClose: () => void; onOpen: (job: ActivityJob) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const page = useHistoryPage(true, group.key);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; const node = dialog.current!; node.showModal(); return () => { node.close(); previous?.focus(); }; }, []);
  const clear = async (id: string) => { setBusy(true); try { await clearHistory({ ids: [id] }); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } };
  return createPortal(<dialog className="dialog activity-dialog" ref={dialog} aria-labelledby="activity-history-title" onCancel={onClose}>
    <header><h2 id="activity-history-title">{group.label} history</h2><button type="button" aria-label="Close activity history" onClick={onClose}>×</button></header>
    <div className="dialog-body">{(error || page.error) && <p role="alert">{error || page.error}</p>}{!page.data ? <p role="status">Loading history…</p> : !page.data.jobs?.length ? <p>No activity remaining.</p> : <ul className="activity-run-history">{page.data.jobs.map(job => <li key={job.id} data-activity-id={job.id}><button type="button" className="text-button activity-open" onClick={() => onOpen(job)}><strong>{job.label}<span>{job.scope}</span></strong><small>{stateText(job)}</small><small>Started <Timestamp value={job.created_at}/>{job.finished_at && <> · Finished <Timestamp value={job.finished_at}/></>}</small></button>{!activityRunning(job) && <button type="button" className="text-button" disabled={busy} aria-label={`Clear ${job.scope} run`} onClick={() => void clear(job.id)}>Clear</button>}</li>)}</ul>}</div>
    <footer><Paging older={page.older} newer={page.newer}/><button type="button" className="secondary" onClick={onClose}>Close</button></footer>
  </dialog>, document.body);
}
export function ActivityHistory({ open, onClose, onOpen }: { open: boolean; onClose: () => void; onOpen: (job: ActivityJob) => void }) {
  const page = useHistoryPage(open);
  const [group, setGroup] = useState<Group>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem("cratekeep-dismissed-activity") || "[]");
      if (Array.isArray(saved) && saved.length) void clearHistory({ ids: saved }).then(() => localStorage.removeItem("cratekeep-dismissed-activity")).catch(() => {});
    } catch { /* A malformed old local preference must not prevent loading history. */ }
  }, []);
  const clear = async (key?: string) => { setBusy(true); setError(""); try { await clearHistory(key ? { group: key } : {}); } catch (reason) { setError((reason as Error).message); } finally { setBusy(false); } };
  return <>{open && <section id="activity-panel" className="activity-panel" aria-label="Activity monitor"><header><h2>Activity</h2><div><button type="button" className="text-button" disabled={busy || !page.data?.clearable} onClick={() => void clear()}>Clear all</button><button type="button" className="text-button" onClick={onClose}>Close</button></div></header>
    {(error || page.error) && <p role="alert">{error || page.error} <button type="button" className="text-button" onClick={page.retry}>Retry</button></p>}{!page.data ? !page.error && <p role="status">Loading activity…</p> : !page.data.groups?.length ? <p>No recent activity.</p> : <div className="activity-feed" role="region" aria-label="Recent activity" tabIndex={0}>{page.data.groups.map(item => {
      const quietScan = item.key === "inbox-scans";
      const running = item.active > 0;
      const attention = item.attention > 0;
      const state = running ? "running" : attention ? "attention" : item.latest.status === "stopped" ? "stopped" : "success";
      const title = feedTitle(item);
      const status = quietScan ? (running ? "Scanning…" : "Success") : item.count === 1 ? stateText(item.latest) : [running ? `${item.active} running` : "", attention ? `${item.attention} need attention` : "", item.finished && !attention ? `${item.finished} finished` : ""].filter(Boolean).join(" · ");
      const content = <><span className={`activity-state ${state}`} aria-hidden="true"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"><path d={running ? "M8 2a6 6 0 1 1-6 6" : attention ? "M8 3v6m0 3v1" : state === "stopped" ? "M3 8h10" : "m3 8 3 3 7-7"}/></svg></span><span className="activity-item-copy"><strong title={title}>{title}{!quietScan && item.count > 1 && <b className="activity-repeat">×{item.count}</b>}</strong><small title={`${item.scope} · ${status}`}>{item.scope} · <span>{status}</span></small></span><Timestamp value={item.timestamp}/></>;
      return <article key={item.key} data-state={state} data-activity-id={item.latest.id} data-activity-group={item.key}>{quietScan && !running ? <div className="activity-open activity-status">{content}</div> : <button type="button" className="text-button activity-open" onClick={() => { if (quietScan || item.count === 1) { onOpen(item.latest); onClose(); } else { setGroup(item); onClose(); } }}>{content}</button>}<button type="button" className="text-button activity-clear" title={`Clear ${title}`} aria-label={`Clear ${title}`} disabled={busy || !item.finished} onClick={() => void clear(item.key)}>×</button></article>;
    })}</div>}<Paging older={page.older} newer={page.newer}/>
  </section>}{group && <RunHistory group={group} onClose={() => setGroup(undefined)} onOpen={onOpen}/>}</>;
}
