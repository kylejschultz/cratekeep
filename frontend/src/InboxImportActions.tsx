import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { useToast } from "./toastContext";

type Preview = { preview_id: string; fingerprint: string; files: { path: string; location: string }[]; count: number };
export function InboxActionDialog({ inboxId, kind, jobs, onClose, onDone }: { inboxId: string; kind: "undo" | "delete-imported"; jobs: number[]; onClose: () => void; onDone: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  const [preview, setPreview] = useState<Preview | null>(null), [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const notify = useToast();
  const base = `/api/inbox/${encodeURIComponent(inboxId)}/actions`;
  useEffect(() => {
    const node = ref.current!; node.showModal();
    let current = true;
    api<Preview>(`${base}/preview`, { method: "POST", body: JSON.stringify({ kind, job_ids: jobs }) }).then(value => { if (current) setPreview(value); }).catch(reason => { if (current) setError(reason.message); });
    return () => { current = false; node.close(); };
  }, [base, kind, jobs]);
  function finish(result: any) {
    if (result.status !== "complete") { setError(result.message + " " + (result.errors || []).map((row: any) => row.error).join("; ")); setPreview(null); onDone(); }
    else { notify(result.message); onDone(); onClose(); }
  }
  async function apply() {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      finish(await api<any>(`${base}/apply`, { method: "POST", body: JSON.stringify({ preview_id: preview.preview_id, expected_fingerprint: preview.fingerprint, confirmed: true }) }));
    } catch (reason) {
      // A lost response must never resubmit deletion. Read the durable result.
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10000);
      try {
        const saved = await api<any>(`${base}/${encodeURIComponent(preview.preview_id)}`, { signal: controller.signal });
        if (["complete", "partial"].includes(saved.status)) finish({ ...saved.result, status: saved.status });
        else { setError(saved.status === "running" ? "This action is still running. Close this dialog and refresh Inbox to check its result." : `${(reason as Error).message}. Refresh Inbox before trying another action.`); setPreview(null); }
      } catch { setError(`${(reason as Error).message}. The result is uncertain; refresh Inbox before trying another action.`); setPreview(null); }
      finally { clearTimeout(timeout); }
    } finally { setBusy(false); }
  }
  return <dialog ref={ref} className="dialog inbox-action-dialog" aria-labelledby="inbox-action-title" onCancel={event => { if (busy) event.preventDefault(); else onClose(); }}>
    <header><h2 id="inbox-action-title">{kind === "undo" ? "Undo import" : "Remove imported originals"}</h2></header>
    <div className="dialog-body"><p>{kind === "undo" ? "Remove only the Library copies created by this import and their Beets records. Inbox originals stay unchanged." : "Permanently delete the imported originals and accompanying files in their album folders from Inbox. Folders containing other audio are kept. Library files stay unchanged. Undo will no longer be available."}</p>
      {!preview && !error && <p role="status">Checking files…</p>}
      {preview && <><p>{preview.count} {preview.count === 1 ? "file" : "files"}</p>{kind === "undo" && <ul>{preview.files.map(row => <li key={`${row.location}/${row.path}`}><strong>{row.location}</strong> · {row.path}</li>)}</ul>}</>}
      {error && <p role="alert" className="notice error">{error}</p>}
    </div><footer><button type="button" className="secondary" disabled={busy} onClick={onClose}>{error ? "Close" : "Cancel"}</button><button type="button" className="danger-action" disabled={!preview || busy} onClick={() => void apply()}>{busy ? "Working…" : kind === "undo" ? "Remove Library copies and undo" : "Permanently remove Inbox originals"}</button></footer>
  </dialog>;
}
