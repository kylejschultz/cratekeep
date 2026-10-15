import { useEffect, useState, type ReactNode } from "react";

const links = [
  ["overview", "/", "Overview", "⌂"],
  ["inbox", "/inbox", "Inbox", "↓"],
  ["library-import", "/library-import", "Library Import", "⇣"],
  ["library", "/library", "Library", "♫"],
  ["settings", "/settings", "Settings", "⚙"],
] as const;

export function Shell({ page, buildSha, children }: { page: string; buildSha: string; children: ReactNode }) {
  const [dark, setDark] = useState(() => localStorage.getItem("cratekeep-theme") === "dark");
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("cratekeep-sidebar") === "collapsed");
  const [open, setOpen] = useState(false);
  useEffect(() => { document.documentElement.dataset.theme = dark ? "dark" : "light"; localStorage.setItem("cratekeep-theme", dark ? "dark" : "light"); }, [dark]);
  useEffect(() => localStorage.setItem("cratekeep-sidebar", collapsed ? "collapsed" : "expanded"), [collapsed]);
  return <div className={`app ${collapsed ? "is-collapsed" : ""} ${open ? "nav-open" : ""}`}>
    <a className="skip-link" href="#main-content">Skip to main content</a>
    <aside className="sidebar" aria-label="Application sidebar">
      <a className="brand" href="/"><img src="/static/cratekeep-logo.png" alt=""/><span><strong>Cratekeep</strong><small>Music library manager</small></span></a>
      <button className="collapse" type="button" onClick={() => setCollapsed(v => !v)} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed}>{collapsed ? "›" : "‹"}</button>
      <nav aria-label="Primary navigation"><ul>{links.map(([key, href, label, icon]) => <li key={key}><a href={href} aria-current={page === key ? "page" : undefined}><span aria-hidden="true">{icon}</span><span className="nav-label">{label}</span></a></li>)}</ul></nav>
      <footer><label>Dark mode <button id="theme-toggle" className="switch" type="button" onClick={() => setDark(v => !v)} aria-pressed={dark}>{dark ? "☀" : "☾"}</button></label><a href="https://github.com/kylejschultz/cratekeep" target="_blank" rel="noreferrer">GitHub ↗</a><code>build {buildSha.slice(0, 7)}</code></footer>
    </aside>
    <button className="scrim" aria-label="Close navigation" onClick={() => setOpen(false)}/>
    <div className="app-content"><header className="mobile-header"><button type="button" aria-label="Open navigation" aria-expanded={open} onClick={() => setOpen(true)}>☰</button><img src="/static/cratekeep-logo.png" alt=""/><strong>Cratekeep</strong></header><main id="main-content">{children}</main></div>
  </div>;
}

export function PageHeader({ eyebrow, title, children }: { eyebrow: string; title: string; children?: ReactNode }) {
  return <header className="page-header"><p className="eyebrow">{eyebrow}</p><h1>{title}</h1>{children && <p className="lede">{children}</p>}</header>;
}

export function Notice({ kind = "info", children }: { kind?: "info" | "error" | "success"; children: ReactNode }) {
  return <div className={`notice ${kind}`} role={kind === "error" ? "alert" : "status"}>{children}</div>;
}

