import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, activityEnabled, activityRunning, type ActivityJob } from "./api";
import { useActivity } from "./activityStore";
import { InboxActionDialog } from "./InboxImportActions";

function workCount(job: ActivityJob) {
  if (["matching", "import"].includes(job.mode || "")) return `${job.processed} / ${job.total} albums`;
  if (job.files_found != null) return `${job.files_found} tracks found`;
  if (job.stage_done == null) return "";
  return `${job.stage_done}${job.stage_total != null ? ` / ${job.stage_total}` : ""} ${job.stage_unit || "files"}${job.stage_total == null ? " found" : " processed"}`;
}
function Progress({ job }: { job: ActivityJob }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(timer); }, []);
  const batch = ["matching", "import"].includes(job.mode || "");
  const total = batch ? job.total : job.stage_total;
  const value = batch ? job.processed : job.stage_done;
  return <div className="activity-progress"><span aria-live="polite">{job.status === "waiting" && job.resume_at ? `MusicBrainz cooldown · resumes ${new Date(job.resume_at * 1000).toLocaleTimeString()}` : job.stage}</span>
    {activityRunning(job) && <progress aria-label={`${job.label} progress`} {...(total != null && total > 0 ? { value: value || 0, max: total } : {})}/>}
    <small>{[job.created_at ? `${Math.floor(((job.finished_at || now) - job.created_at) / 60)}m ${Math.floor(((job.finished_at || now) - job.created_at) % 60)}s elapsed` : "", workCount(job), job.current].filter(Boolean).join(" · ")}</small>
  </div>;
}
function Details({ job, onClose }: { job: ActivityJob; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [error, setError] = useState("");
  const [clearing, setClearing] = useState(false);
  const stop = async () => { try { await api(`/api/activity/${job.id}/stop`, { method: "POST", body: "{}" }); } catch (reason) { setError((reason as Error).message); } };
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; const node = dialog.current!; node.showModal(); return () => { node.close(); previous?.focus(); }; }, []);
  const receipts = job.rows.filter(row => row.status === "imported" && row.jobId).map(row => row.jobId);
  return createPortal(<><dialog className="dialog activity-dialog" ref={dialog} aria-labelledby="activity-details-title" onCancel={onClose}>
    <header><div><h2 id="activity-details-title">{job.label}</h2><p>{job.scope} · {job.status}</p></div><button type="button" aria-label="Close activity details" onClick={onClose}>×</button></header>
    <div className="dialog-body"><Progress job={job}/>{(error || job.error) && <p role="alert">{error || job.error}</p>}
      <ul className="activity-results">{job.rows.map((row, index) => <li key={index}><strong>{row.album || job.label}</strong><span>{row.status.replaceAll("-", " ")}</span>{row.reason && <p>{row.reason}</p>}</li>)}</ul>
      {job.result?.errors?.map((row: any, index: number) => <p key={index}>{row.error}</p>)}
    </div><footer><a href={job.href}>Open {job.scope}</a>{activityRunning(job) && ["matching", "import"].includes(job.mode || "") && <button type="button" className="secondary" disabled={job.status === "stopping"} onClick={() => void stop()}>Stop after current album</button>}
    {!activityRunning(job) && job.mode === "matching" && job.rows.some(row => ["failed", "needs-retry", "not-started"].includes(row.status)) && <button type="button" onClick={() => void api(`/api/activity/${job.id}/resume`, { method: "POST", body: "{}" }).then(onClose).catch(reason => setError(reason.message))}>Resume unfinished</button>}
    {job.inbox_id && receipts.length > 0 && <button type="button" className="danger-action" onClick={() => setClearing(true)}>Clear imported from Inbox</button>}</footer>
  </dialog>{clearing && <InboxActionDialog inboxId={job.inbox_id!} kind="delete-imported" jobs={receipts} onClose={() => setClearing(false)} onDone={() => { setClearing(false); onClose(); }}/>}</>, document.body);
}
export function ActivityMonitor() {
  const { jobs, error } = useActivity();
  const [open, setOpen] = useState(false), [selected, setSelected] = useState<string>();
  const [hidden, setHidden] = useState<string[]>(() => { try { return JSON.parse(localStorage.getItem("cratekeep-dismissed-activity") || "[]"); } catch { return []; } });
  useEffect(() => { const show = (event: Event) => { setSelected((event as CustomEvent<string>).detail); }; window.addEventListener("cratekeep:show-activity", show); return () => window.removeEventListener("cratekeep:show-activity", show); }, []);
  if (!activityEnabled) return null;
  const active = jobs.filter(activityRunning), recent = jobs.filter(job => !activityRunning(job) && !hidden.includes(job.id)).slice(0, 5);
  const chosen = jobs.find(job => job.id === selected);
  return <div className="activity-monitor"><button type="button" className="text-button" aria-expanded={open} aria-controls="activity-panel" onClick={() => setOpen(value => !value)}>Activity{active.length ? ` (${active.length})` : ""}</button>
    {open && <section id="activity-panel" className="activity-panel" aria-label="Activity monitor"><header><h2>Activity</h2><button type="button" className="text-button" onClick={() => setOpen(false)}>Close</button></header>
      {error && <p role="status">{error}</p>}{!active.length && !recent.length && <p>No recent activity.</p>}
      {[...active, ...recent].map(job => <article key={job.id} data-activity-id={job.id}><button type="button" className="text-button activity-open" onClick={() => { setSelected(job.id); setOpen(false); }}><strong>{job.label}<span>{job.scope}</span></strong><small>{[activityRunning(job) ? job.stage : job.status, workCount(job)].filter(Boolean).join(" · ")}</small></button>{!activityRunning(job) && <button type="button" className="text-button activity-dismiss" aria-label={`Dismiss ${job.label}`} title="Dismiss" onClick={() => { const next = [...hidden, job.id].slice(-100); setHidden(next); localStorage.setItem("cratekeep-dismissed-activity", JSON.stringify(next)); }}>×</button>}</article>)}
    </section>}{chosen && <Details job={chosen} onClose={() => setSelected(undefined)}/>}</div>;
}
export function ActivityInline({ scope }: { scope: "library" | string }) {
  const { jobs, error } = useActivity();
  if (!activityEnabled) return null;
  const relevant = jobs.filter(job => scope === "library" ? job.href.startsWith("/library") : job.href === `/inbox?source=${scope}`);
  const shown = relevant.filter(activityRunning);
  return <div className="page-activity" aria-label="Page activity">{error && shown.length > 0 && <p role="status">{error}</p>}{shown.map(job => <div key={job.id}><strong>{job.label} · {job.scope}</strong><Progress job={job}/><button type="button" className="text-button" onClick={() => window.dispatchEvent(new CustomEvent("cratekeep:show-activity", { detail: job.id }))}>View activity</button></div>)}</div>;
}
