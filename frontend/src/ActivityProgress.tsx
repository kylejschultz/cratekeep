import { useEffect, useState } from "react";
import { useProgressDelay } from "./useProgressDelay";
import { activityRunning, type ActivityJob, workCount } from "./api";

export function ActivityProgress({ job }: { job: ActivityJob }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now() / 1000), 1000); return () => clearInterval(timer); }, []);
  const running = activityRunning(job);
  const detailed = useProgressDelay(running, job.id, job.created_at);
  const batch = ["matching", "import", "batch"].includes(job.mode || "");
  const total = batch ? job.total : job.stage_total;
  const value = batch ? job.processed : job.stage_done;
  if (running && !detailed) return <div className="activity-progress" role="status">Loading…</div>;
  return <div className="activity-progress"><span aria-live="polite">{job.status === "waiting" && job.resume_at ? `MusicBrainz cooldown · resumes ${new Date(job.resume_at * 1000).toLocaleTimeString()}` : job.stage}</span>
    {running && <progress aria-label={`${job.label} progress`} {...(total != null && total > 0 ? { value: value || 0, max: total } : {})}/>}
    <small>{[job.created_at ? `${Math.floor(((job.finished_at || now) - job.created_at) / 60)}m ${Math.floor(((job.finished_at || now) - job.created_at) % 60)}s elapsed` : "", workCount(job), job.current].filter(Boolean).join(" · ")}</small>
  </div>;
}
