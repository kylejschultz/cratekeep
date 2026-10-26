import { useEffect, useRef, useState } from "react";
import { ApiError } from "./api";
import type { Album } from "./types";

type MatchStatus = "queued" | "matching" | "cooldown" | "matched" | "cached" | "no-match" | "needs-retry" | "failed" | "not-started";
export type MatchRow = { id: number; album: string; status: MatchStatus; reason?: string; attempts: number };
type Props = { albums: Album[]; execute: (id: number, reuseCurrent: boolean) => Promise<any>; onRunning: (running: boolean) => void; onFinish: (rows: MatchRow[]) => void; onClose: () => void };
const labels: Record<MatchStatus, string> = { queued: "Queued", matching: "Matching…", cooldown: "Waiting", matched: "Matched", cached: "Saved match", "no-match": "No matches", "needs-retry": "Needs retry", failed: "Failed", "not-started": "Not started" };
const pending = (row: MatchRow) => ["queued", "needs-retry", "not-started"].includes(row.status);

export function MatchingDialog({ albums, execute, onRunning, onFinish, onClose }: Props) {
  const [rows, setRows] = useState<MatchRow[]>(() => albums.map(album => ({ id: album.id, album: album.album || album.name || "Album", status: "queued", attempts: 0 })));
  const [phase, setPhase] = useState<"confirm" | "running" | "paused" | "done">("confirm");
  const [refreshSaved, setRefreshSaved] = useState(false), [stopping, setStopping] = useState(false);
  const [readyAt, setReadyAt] = useState(0), [now, setNow] = useState(Date.now());
  const stop = useRef(false), running = useRef(false), mounted = useRef(false);
  const wake = useRef<(() => void) | null>(null), dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    mounted.current = true; const node = dialog.current!; node.showModal(); node.focus();
    return () => { mounted.current = false; stop.current = true; wake.current?.(); node.close(); };
  }, []);
  useEffect(() => {
    if (!readyAt) return;
    const tick = window.setInterval(() => { setNow(Date.now()); if (Date.now() >= readyAt) window.clearInterval(tick); }, 1000);
    return () => window.clearInterval(tick);
  }, [readyAt]);
  useEffect(() => {
    if (phase !== "running") return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    addEventListener("beforeunload", warn); return () => removeEventListener("beforeunload", warn);
  }, [phase]);
  const wait = (milliseconds: number) => new Promise<void>(resolve => {
    const timer = window.setTimeout(() => { wake.current = null; resolve(); }, milliseconds);
    wake.current = () => { window.clearTimeout(timer); wake.current = null; resolve(); };
  });
  const run = async () => {
    if (running.current || Date.now() < readyAt) return;
    running.current = true; stop.current = false; setStopping(false); setPhase("running"); onRunning(true);
    const deadline = Date.now() + 15 * 60 * 1000;
    const results = rows.map(row => pending(row) ? { ...row, status: "queued" as const } : row);
    const update = (index: number, status: MatchStatus, reason?: string, attempts = results[index].attempts) => {
      results[index] = { ...results[index], status, reason, attempts }; setRows([...results]);
    };
    for (let index = 0; index < results.length; index++) {
      if (results[index].status !== "queued") continue;
      if (stop.current || Date.now() >= deadline) { stop.current = true; break; }
      for (let attempt = 1; attempt <= 3; attempt++) {
        if (Date.now() >= deadline) { update(index, "needs-retry", "Matching run reached its time limit. Resume when ready."); stop.current = true; break; }
        update(index, "matching", undefined, results[index].attempts + 1);
        let failure: { message: string; retryable: boolean; seconds: number };
        try {
          const response = await execute(results[index].id, !refreshSaved);
          if (!mounted.current) return;
          const error = response.result?.errors?.[0] || response.errors?.[0];
          if (error) failure = { message: error.error || "Matching failed", retryable: error.retryable === true, seconds: Number(error.retry_after_seconds) || 0 };
          else {
            const album = response.album;
            if (album === null) {
              failure = { message: "Album is no longer available in this queue. Refresh the queue.", retryable: false, seconds: 0 };
            } else if (!album) throw new Error("Matching response was incomplete; saved results will be checked on retry");
            else if (album.candidate_status !== "complete") {
              failure = { message: album.candidate_error || "Match evidence needs attention", retryable: false, seconds: 0 };
            } else {
              const count = album.candidates?.length || 0;
              update(index, !count ? "no-match" : response.already_matched || response.result?.already_matched ? "cached" : "matched");
              break;
            }
          }
        } catch (reason) {
          if (!mounted.current) return;
          failure = { message: (reason as Error).message, retryable: reason instanceof ApiError ? reason.retryable : true, seconds: reason instanceof ApiError ? reason.retryAfterSeconds : 0 };
        }
        if (!failure.retryable) { update(index, "failed", failure.message); break; }
        const seconds = Math.max(10 * 2 ** (attempt - 1), Number.isFinite(failure.seconds) ? Math.ceil(failure.seconds) : 0);
        const resumeAt = Date.now() + seconds * 1000;
        setReadyAt(resumeAt); setNow(Date.now());
        if (attempt === 3 || resumeAt > deadline || stop.current) {
          update(index, "needs-retry", failure.message); stop.current = true; break;
        }
        update(index, "cooldown", failure.message);
        await wait(seconds * 1000);
        if (!mounted.current) return;
        if (stop.current) { update(index, "needs-retry", "Stopped while waiting. Successful matches have been kept."); break; }
        setReadyAt(0);
      }
    }
    if (!mounted.current) return;
    const final = results.map(row => row.status === "queued" ? { ...row, status: "not-started" as const } : row);
    setRows(final); running.current = false; onRunning(false);
    setPhase(final.some(pending) ? "paused" : "done");
    if (!final.some(pending)) setReadyAt(0);
    onFinish(final);
  };
  const remaining = Math.max(0, Math.ceil((readyAt - now) / 1000));
  const complete = rows.filter(row => ["matched", "cached", "no-match"].includes(row.status)).length;
  return <dialog ref={dialog} tabIndex={-1} className="dialog bulk-import-dialog matching-dialog" aria-labelledby="matching-title" onCancel={event => { if (phase === "running") event.preventDefault(); else onClose(); }}>
    <header><div><p className="eyebrow">MusicBrainz</p><h2 id="matching-title">{phase === "confirm" ? "Match selected albums" : phase === "running" ? "Matching your albums" : phase === "paused" ? "Matching paused" : "Matching results"}</h2></div></header>
    <div className="dialog-body"><p>{complete} of {rows.length} {rows.length === 1 ? "album" : "albums"} checked. Successful matches are saved.</p>
      {phase === "confirm" && <><p>Reuse current saved results unless refreshed below. Matching does not import or modify audio.</p><label className="check"><input type="checkbox" checked={refreshSaved} onChange={event => setRefreshSaved(event.target.checked)}/>Refresh saved matches</label></>}
      {phase !== "confirm" && <progress aria-label="Albums matched" value={complete} max={rows.length}/>}
      <p role="status">{remaining ? `MusicBrainz cooldown: ${remaining}s remaining.` : phase === "running" ? stopping ? "Stopping after the current album finishes…" : "Matching one album at a time." : phase === "paused" ? "You can resume unfinished albums; successful results will be skipped." : phase === "done" ? "Matching finished. Review the results before importing." : "Start when ready. Temporary failures get two retries; sustained failures pause the queue."}</p>
      <ul className="bulk-import-rows" aria-label="Album matching progress">{rows.map(row => <li key={row.id}><div><strong>{row.album}</strong>{row.reason && <small>{row.reason}</small>}</div><span className={`bulk-result ${row.status}`}>{row.status === "matching" && <span className="spinner"/>}{labels[row.status]}</span></li>)}</ul>
    </div>
    <footer>{phase === "running" ? <button className="secondary" type="button" disabled={stopping} onClick={() => { stop.current = true; setStopping(true); wake.current?.(); }}>Stop after current album</button> : <><button className="secondary" type="button" onClick={onClose}>{phase === "confirm" ? "Cancel" : "Close results"}</button>{phase !== "done" && <button type="button" disabled={remaining > 0} onClick={() => void run()}>{phase === "paused" ? "Resume unfinished" : "Start matching"}</button>}</>}</footer>
  </dialog>;
}
