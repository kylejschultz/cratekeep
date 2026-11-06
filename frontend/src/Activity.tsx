import { useEffect, useRef, useState } from "react";
import { ActivityProgress } from "./ActivityProgress";
import { HeaderIcon } from "./HeaderIcon";
import { createPortal } from "react-dom";
import { api, activityEnabled, activityRunning, type ActivityJob } from "./api";
import { useActivity } from "./activityStore";
import { ActivityHistory } from "./ActivityHistory";
import { groupInboxActions } from "./allInboxes";
import { InboxActionDialog, type InboxActionSelection } from "./InboxImportActions";

function Details({ job: initial, onClose, onOpen }: { job: ActivityJob; onClose: () => void; onOpen: (job: ActivityJob) => void }) {
  const [job, setJob] = useState(initial);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const next = await api<ActivityJob>(`/api/activity/${initial.id}`);
        if (!cancelled) { setJob(next); if (activityRunning(next)) timer = setTimeout(() => void load(), 1500); }
      } catch (reason) { if (!cancelled) { setError((reason as Error).message); timer = setTimeout(() => void load(), 1500); } }
    };
    void load();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [initial.id]);
  const dialog = useRef<HTMLDialogElement>(null);
  const [clearing, setClearing] = useState<InboxActionSelection | null>(null);
  const stop = async () => { try { await api(`/api/activity/${job.id}/stop`, { method: "POST", body: "{}" }); } catch (reason) { setError((reason as Error).message); } };
  useEffect(() => { const previous = document.activeElement as HTMLElement | null; const node = dialog.current!; node.showModal(); return () => { node.close(); previous?.focus(); }; }, []);
  const receipts = job.rows.filter(row => row.status === "imported" && row.jobId).map(row => row.jobId);
  return createPortal(<><dialog className="dialog activity-dialog" ref={dialog} aria-labelledby="activity-details-title" onCancel={onClose}>
    <header><div><h2 id="activity-details-title">{job.label}</h2><p>{job.scope} · {job.status}</p></div><button type="button" aria-label="Close activity details" onClick={onClose}>×</button></header>
    <div className="dialog-body"><ActivityProgress job={job}/>{(error || job.error) && <p role="alert">{error || job.error}</p>}
      <ul className="activity-results">{job.rows.map((row, index) => <li key={index}><strong>{row.album || job.label}</strong>{job.inbox_ids && job.inbox_ids.length > 1 && <small>{row.inbox_name}</small>}<span>{row.status.replaceAll("-", " ")}</span>{row.reason && <p>{row.reason}</p>}</li>)}</ul>
      {job.result?.errors?.map((row: any, index: number) => <p key={index}>{row.error}</p>)}
    </div><footer>{(job.check_ids || (job.check_id ? [job.check_id] : [])).map((checkId, index, checks) => <button key={checkId} type="button" className="text-button" onClick={() => void api<ActivityJob>(`/api/activity/${checkId}`).then(onOpen).catch(reason => setError(reason.message))}>View preflight checks{checks.length > 1 ? ` (${index + 1})` : ""}</button>)}<a href={job.href}>Open {job.scope}</a>{activityRunning(job) && ["matching", "import"].includes(job.mode || "") && <button type="button" className="secondary" disabled={job.status === "stopping"} onClick={() => void stop()}>Stop after current album</button>}
    {!activityRunning(job) && job.mode === "matching" && job.rows.some(row => ["failed", "needs-retry", "not-started"].includes(row.status)) && <button type="button" onClick={() => void api(`/api/activity/${job.id}/resume`, { method: "POST", body: "{}" }).then(onClose).catch(reason => setError(reason.message))}>Resume unfinished</button>}
    {(job.inbox_id || job.inbox_ids?.length) && receipts.length > 0 && <button type="button" className="danger-action" onClick={() => {
      const selection: InboxActionSelection = { kind: "delete-imported", jobs: receipts };
      if (!job.inbox_id) selection.groups = groupInboxActions(selection, job.rows.filter(row => row.jobId).map(row => ({ id: row.id, album: row.album, source_id: row.inbox_id, inbox_name: row.inbox_name, imported: { job_id: row.jobId } })));
      setClearing(selection);
    }}>Clear imported from Inbox</button>}</footer>
  </dialog>{clearing && <InboxActionDialog inboxId={job.inbox_id || "all"} {...clearing} onClose={() => setClearing(null)} onDone={complete => { if (complete) { setClearing(null); onClose(); } }}/>}</>, document.body);
}
export function ActivityMonitor() {
  const { jobs, error } = useActivity();
  const [open, setOpen] = useState(false), [selected, setSelected] = useState<ActivityJob>();
  const [detailError, setDetailError] = useState("");
  const monitor = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !monitor.current?.contains(event.target)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setOpen(false); };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", dismiss); document.removeEventListener("keydown", escape); };
  }, [open]);
  useEffect(() => {
    const show = (event: Event) => { const id = (event as CustomEvent<string>).detail; void api<ActivityJob>(`/api/activity/${id}`).then(job => { setSelected(job); setDetailError(""); }).catch(reason => { setDetailError(reason.message); setOpen(true); }); };
    window.addEventListener("cratekeep:show-activity", show);
    return () => window.removeEventListener("cratekeep:show-activity", show);
  }, []);
  if (!activityEnabled) return null;
  const active = jobs.filter(activityRunning);
  return <div className="activity-monitor" ref={monitor}><button type="button" className={`header-icon${active.length ? " is-running" : ""}`} aria-label={`Activity${active.length ? ` (${active.length})` : ""}`} title={active.length ? `Activity: ${active.length} running` : "Activity"} aria-expanded={open} aria-controls="activity-panel" onClick={() => setOpen(value => !value)}><HeaderIcon name="activity"/></button>
    {(error || detailError) && open && <p role="status">{error || detailError}</p>}<ActivityHistory open={open} onClose={() => setOpen(false)} onOpen={setSelected}/>
    {selected && <Details key={selected.id} job={selected} onOpen={setSelected} onClose={() => setSelected(undefined)}/>}</div>;
}
export function ActivityInline({ scope, excludeId }: { scope: "library" | string; excludeId?: string }) {
  const { jobs, error } = useActivity();
  if (!activityEnabled) return null;
  const relevant = jobs.filter(job => scope === "library" ? job.href.startsWith("/library") : scope === "all" ? job.href.startsWith("/inbox") : job.href === `/inbox?source=${scope}` || job.inbox_ids?.includes(scope));
  const shown = relevant.filter(job => activityRunning(job) && job.id !== excludeId);
  return <div className="page-activity" aria-label="Page activity">{error && shown.length > 0 && <p role="status">{error}</p>}{shown.map(job => <div key={job.id}><strong>{job.label} · {job.scope}</strong><ActivityProgress job={job}/><button type="button" className="text-button" onClick={() => window.dispatchEvent(new CustomEvent("cratekeep:show-activity", { detail: job.id }))}>View activity</button></div>)}</div>;
}
