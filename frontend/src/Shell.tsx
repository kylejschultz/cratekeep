import { useEffect, useState, type ReactNode } from "react";

const links = [["overview", "/", "Overview"], ["inbox", "/inbox", "Inbox"], ["library", "/library", "Library"], ["settings", "/settings", "Settings"]] as const;

export function Shell({ page, buildSha, children }: { page: string; buildSha: string; children: ReactNode }) {
  const [dark, setDark] = useState(() => localStorage.getItem("cratekeep-theme") !== "light");
  useEffect(() => { document.documentElement.dataset.theme = dark ? "dark" : "light"; localStorage.setItem("cratekeep-theme", dark ? "dark" : "light"); }, [dark]);
  return <div className={`app listening-desk page-${page}`}>
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <header className="desk-header">
      <a className="desk-brand" href="/"><img src="/static/cratekeep-logo.png" alt=""/><span>cratekeep</span></a>
      <nav aria-label="Primary navigation">{links.map(([key, href, label]) => <a key={key} href={href} aria-current={(page === key || (page === "library-import" && key === "library")) ? "page" : undefined}>{label}</a>)}</nav>
      <button id="theme-toggle" className="secondary" type="button" onClick={() => setDark(v => !v)} aria-label="Dark mode" aria-pressed={dark}>{dark ? "Light" : "Dark"}</button>
      <code className="build-label" title={buildSha}>build {buildSha.slice(0, 7)}</code>
    </header>
    <div className="app-content"><main id="main-content">{children}</main></div>
  </div>;
}

export function PageHeader({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return <header className="page-header"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1>{children && <p className="lede">{children}</p>}</header>;
}
export function Notice({ kind = "info", children }: { kind?: "info" | "error" | "success"; children: ReactNode }) {
  return <div className={`notice ${kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}
