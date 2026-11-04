import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, activityRunning, type ActivityJob, workCount } from "./api";

type Group = { key: string; label: string; scope: string; count: number; active: number; attention: number; finished: number; timestamp: number; latest: ActivityJob };
type Page = { groups?: Group[]; jobs?: ActivityJob[]; clearable: number; snapshot: number; next_before: number | null };
type Cursor = { before?: number; snapshot?: number };

function Timestamp({ value }: { value?: number }) {
  if (!value) return null;
  const date = new Date(value * 1000);
  return <time dateTime={date.toISOString()} title={date.toLocaleString()}>{date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</time>;
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
        if (cursor.snapshot != null) query.set("snapshot", String(cursor.snapshot));
        const next = await api<Page>(`/api/activity/history?${query}`);
        if (!cancelled) { setData(next); setError(""); }
      } catch (reason) { if (!cancelled) setError((reason as Error).message); }
      finally { if (!cancelled) timer = setTimeout(() => void load(), 1500); }
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [enabled, group, cursor, refresh]);
  return { data, error, older: data?.next_before != null ? () => setPages(previous => [...previous, { before: data.next_before!, snapshot: data.snapshot }]) : undefined,
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
    <div className="dialog-body">{(error || page.error) && <p role="alert">{error || page.error}</p>}{!page.data ? <p role="status">Loading history…</p> : !page.data.jobs?.length ? <p>No activity remaining.</p> : <ul className="activity-run-history">{page.data.jobs.map(job => <li key={job.id} data-activity-id={job.id}><button type="button" className="text-button activity-open" onClick={() => onOpen(job)}><strong>{job.label}<span>{job.scope}</span></strong><small>{[activityRunning(job) ? job.stage : job.status, workCount(job)].filter(Boolean).join(" · ")}</small><small>Started <Timestamp value={job.created_at}/>{job.finished_at && <> · Finished <Timestamp value={job.finished_at}/></>}</small></button>{!activityRunning(job) && <button type="button" className="text-button" disabled={busy} aria-label={`Clear ${job.scope} run`} onClick={() => void clear(job.id)}>Clear</button>}</li>)}</ul>}</div>
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
    {(error || page.error) && <p role="alert">{error || page.error}</p>}{!page.data ? <p role="status">Loading activity…</p> : !page.data.groups?.length ? <p>No recent activity.</p> : <div className="activity-feed" role="region" aria-label="Recent activity" tabIndex={0}>{page.data.groups.map(item => {
      const quietScan = item.key === "inbox-scans";
      const status = quietScan ? (item.active ? "Scanning…" : "Success") : item.count === 1 ? [activityRunning(item.latest) ? item.latest.stage : item.latest.status, workCount(item.latest)].filter(Boolean).join(" · ") : [item.active ? `${item.active} running` : "", item.attention ? `${item.attention} need attention` : "", item.finished ? `${item.finished} finished` : ""].filter(Boolean).join(" · ");
      const content = <><strong>{item.label}{!quietScan && item.count > 1 && <b className="activity-repeat">×{item.count}</b>}<span>{item.scope}</span></strong><span className="activity-row-meta"><small>{status}</small><Timestamp value={item.timestamp}/></span></>;
      return <article key={item.key} data-activity-id={item.latest.id} data-activity-group={item.key}>{quietScan && !item.active ? <div className="activity-open activity-status">{content}</div> : <button type="button" className="text-button activity-open" onClick={() => { if (quietScan || item.count === 1) { onOpen(item.latest); onClose(); } else { setGroup(item); onClose(); } }}>{content}</button>}<button type="button" className="text-button activity-clear" aria-label={`Clear ${item.label}`} disabled={busy || !item.finished} onClick={() => void clear(item.key)}>Clear</button></article>;
    })}</div>}<Paging older={page.older} newer={page.newer}/>
  </section>}{group && <RunHistory group={group} onClose={() => setGroup(undefined)} onOpen={onOpen}/>}</>;
}
