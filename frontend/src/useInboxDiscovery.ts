import { useEffect, useRef, useState } from "react";
import { api } from "./api";

export type DiscoveryStatus = { mode: string; revision: string; scanned_at: string | null; error: string; watcher_error: string };

// Poll only a cheap status marker, never the filesystem. Do not keep a server
// request thread occupied for each open tab. Pending revisions survive edits.
export function useInboxDiscovery(inboxId: string | undefined, paused: boolean, onChange: () => Promise<void>) {
  const [status, setStatus] = useState<DiscoveryStatus | null>(null);
  const [pending, setPending] = useState(false);
  const latest = useRef({ paused, onChange });
  useEffect(() => { latest.current = { paused, onChange }; }, [paused, onChange]);
  useEffect(() => {
    if (!inboxId) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    let applied = "";
    let controller = new AbortController();
    const poll = async () => {
      let delay = 2000;
      try {
        controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 15000);
        let next: DiscoveryStatus;
        try { next = await api<DiscoveryStatus>(`/api/inbox/${encodeURIComponent(inboxId)}/discovery`, { signal: controller.signal }); }
        finally { clearTimeout(timeout); }
        if (stopped) return;
        setStatus(next);
        if (next.mode === "manual") { setPending(false); delay = 30000; } // Pick up settings changed in another tab.
        if (next.mode !== "manual" && next.revision !== applied) {
          setPending(true);
          if (!latest.current.paused) {
            await latest.current.onChange();
            if (stopped) return;
            applied = next.revision;
            setPending(false);
          }
        }
      } catch { delay = 5000; } // The next status request catches up after reconnect.
      if (!stopped) timer = setTimeout(() => { void poll(); }, delay);
    };
    timer = setTimeout(() => { void poll(); }, 2000);
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [inboxId]);
  return { status, pending };
}
