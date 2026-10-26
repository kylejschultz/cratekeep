import React from "react";
import { createRoot } from "react-dom/client";
import { Shell } from "./Shell";
import { ToastProvider } from "./Toasts";
import { Page } from "./pages";
import type { Bootstrap } from "./types";
import "./styles.css";
import "./listening-desk.css";

declare global { interface Window { __CRATEKEEP_BOOTSTRAP__: Bootstrap } }
const boot = window.__CRATEKEEP_BOOTSTRAP__;
createRoot(document.getElementById("root")!).render(<React.StrictMode><ToastProvider durationSeconds={boot.toastDurationSeconds}><Shell page={boot.page} buildSha={boot.buildSha}><Page boot={boot}/></Shell></ToastProvider></React.StrictMode>);
