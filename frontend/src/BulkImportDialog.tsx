import { useEffect, useRef, useState } from "react";
import { startActivity, watchActivity, api } from "./api";
import { ApiError } from "./api";

export type ImportRow = { id: number; album: string; status: "queued" | "blocked" | "importing" | "imported" | "failed" | "not-started" | "unknown"; reason?: string; warnings?: string[]; jobId?: number };
type Props = { activity?: { path: string; body: Record<string, unknown> }; rows: ImportRow[]; source: "inbox" | "library-import"; execute: (id: number) => Promise<any>; onRunning: (running: boolean) => void; onFinish: (rows: ImportRow[]) => void; onClose: () => void; onRemoveImported?: (jobs: number[]) => void };
const labels = { queued: "Queued", blocked: "Blocked", importing: "Importing…", imported: "Imported", failed: "Failed", "not-started": "Not started", unknown: "Check result" };

export function BulkImportDialog({ activity, rows: initialRows, source, execute, onRunning, onFinish, onClose, onRemoveImported }: Props) {
  const [rows, setRows] = useState(initialRows);
  const [phase, setPhase] = useState<"confirm" | "running" | "done">("confirm");
  const [jobId, setJobId] = useState<string>();
  const [stopping, setStopping] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const stop = useRef(false), started = useRef(false), dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { const node = dialog.current!; node.showModal(); node.focus(); return () => node.close(); }, []);
  useEffect(() => {
    if (phase !== "running" || activity) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    addEventListener("beforeunload", warn);
    return () => removeEventListener("beforeunload", warn);
  }, [phase, activity]);
  const run = async () => {
    if (started.current) return;
    started.current = true;
    setPhase("running"); onRunning(true);
    if (activity) {
      try {
        const job = await startActivity(rows.filter(row => row.status === "queued").map(row => ({ path: activity.path, body: { ...activity.body, album_ids: [row.id], confirmed: true }, id: row.id, album: row.album })), "import");
        setJobId(job.id);
        const finished = await watchActivity(job.id, update => { if (mounted.current) setRows(rows.map(row => ({ ...row, ...update.rows.find(current => current.id === row.id) }))); });
        if (!mounted.current) return;
        const final = rows.map(row => ({ ...row, ...finished.rows.find(current => current.id === row.id) }));
        setRows(final); setPhase("done"); onRunning(false); onFinish(final);
      } catch (reason) { setRows(rows.map(row => row.status === "queued" ? { ...row, status: "unknown", reason: `${(reason as Error).message} Check Activity before retrying.` } : row)); setPhase("done"); onRunning(false); }
      return;
    }
    const results = [...rows];
    const update = (index: number, status: ImportRow["status"], reason?: string) => { results[index] = { ...results[index], status, reason }; setRows([...results]); };
    for (let index = 0; index < results.length; index++) {
      if (results[index].status !== "queued") continue;
      if (stop.current) { update(index, "not-started"); continue; }
      update(index, "importing");
      try {
        const result = await execute(results[index].id);
        const receipt = result.imported?.find((row: any) => row.album_review_id === results[index].id);
        if (receipt) {
          results[index] = { ...results[index], jobId: receipt.job_id };
          update(index, "imported");
        }
        else if (result.failed?.length) update(index, "failed", result.failed[0].error);
        else update(index, "blocked", result.skipped?.[0]?.reason || "Album is no longer eligible. Review its current files and match.");
      } catch (reason) {
        // A transport/server failure may happen after files changed. Never retry
        // automatically or claim rollback; leave remaining work untouched.
        const uncertain = !(reason instanceof ApiError) || reason.status >= 500;
        update(index, uncertain ? "unknown" : "failed", `${(reason as Error).message}${uncertain ? ". Check the Library and queue before retrying." : ""}`);
        if (uncertain) stop.current = true;
      }
    }
    onRunning(false); setPhase("done"); onFinish(results);
  };
  const queued = rows.filter(row => row.status === "queued").length;
  const imported = rows.filter(row => row.status === "imported").length;
  const removableJobs = rows.filter(row => row.status === "imported" && Number.isInteger(row.jobId) && row.jobId! > 0).map(row => row.jobId!);
  const settled = rows.filter(row => !["queued", "importing"].includes(row.status)).length;
  return <dialog className="dialog bulk-import-dialog" ref={dialog} tabIndex={-1} aria-labelledby="bulk-import-title" onCancel={event => { if (phase === "running" && !activity) event.preventDefault(); else onClose(); }}>
    <header><div><p className="eyebrow">{source === "inbox" ? "Inbox" : "Library Import"}</p><h2 id="bulk-import-title">{phase === "confirm" ? "Import selected albums" : phase === "running" ? "Importing your albums" : "Import results"}</h2></div></header>
    <div className="dialog-body">
      <p>{phase === "confirm" ? `${queued} ${queued === 1 ? "album" : "albums"} queued · ${rows.length - queued} blocked. ${source === "inbox" ? "Audio is copied into your Library. Inbox originals are kept." : "Saved matches will be applied; audio files stay in place."}` : `${imported} of ${rows.length} albums imported.`}</p>
      {phase === "confirm" && rows.some(row => row.warnings?.length) && <p role="note">Metadata warnings do not block import. Unpaired tracks keep their existing track tags.</p>}
      {phase === "running" && <><progress aria-label="Albums processed" value={settled} max={rows.length}/><p role="status">{stopping ? "Stopping after the current album finishes…" : "Importing one album at a time. You can stop before the next album."}</p></>}
      <ul className="bulk-import-rows" aria-label="Album import progress">{rows.map(row => <li key={row.id}><div><strong>{row.album}</strong>{row.reason && <small>{row.reason}</small>}{row.warnings?.map(warning => <small key={warning}>Warning: {warning}</small>)}</div><span className={`bulk-result ${row.status}`}>{row.status === "importing" && <span className="spinner"/>}{labels[row.status]}</span></li>)}</ul>
    </div>
    <footer>{phase === "running" && activity && <button type="button" className="secondary" onClick={onClose}>Continue in background</button>}{phase === "running" ? <button type="button" className="secondary" disabled={stopping} onClick={() => { stop.current = true; setStopping(true); if (jobId) void api(`/api/activity/${jobId}/stop`, { method: "POST", body: "{}" }); }}>Stop after current album</button> : <><button type="button" className="secondary" onClick={onClose}>{phase === "confirm" ? "Cancel" : "Close results"}</button>{phase === "done" && source === "inbox" && removableJobs.length > 0 && onRemoveImported && <button type="button" className="danger-action" onClick={() => onRemoveImported(removableJobs)}>Clear imported from Inbox</button>}{phase === "confirm" && queued > 0 && <button type="button" onClick={() => void run()}>Start import</button>}</>}</footer>
  </dialog>;
}
