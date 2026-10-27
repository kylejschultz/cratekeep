import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { InboxActionDialog } from "./InboxImportActions";
import { ToastProvider } from "./Toasts";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it.each(["complete", "partial", "running", "unavailable"])("reads the saved %s result after losing the removal response without retrying deletion", async status => {
  const fetch = vi.fn(async (url: string) => {
    if (url.endsWith("/preview")) return { ok:true, json:async()=>({preview_id:"action-1",fingerprint:"hash",count:2,files:[]}) };
    if (url.endsWith("/apply")) throw new TypeError("Connection lost");
    return {ok:status!=="unavailable",status:404,json:async()=>({status,result:{message:"Saved removal result",errors:status==="partial" ? [{error:"One file was kept"}] : []}})};
  });
  vi.stubGlobal("fetch",fetch);
  const onDone=vi.fn(),onClose=vi.fn();
  render(<ToastProvider><InboxActionDialog inboxId="manual" kind="delete-imported" jobs={[7]} onDone={onDone} onClose={onClose}/></ToastProvider>);
  const confirm=screen.getByRole("button",{name:"Permanently remove Inbox originals"});
  await waitFor(()=>expect(confirm).toBeEnabled());
  fireEvent.click(confirm);
  await waitFor(()=>expect(fetch).toHaveBeenCalledWith("/api/inbox/manual/actions/action-1",expect.objectContaining({signal:expect.any(AbortSignal)})));
  if(status==="complete") { await waitFor(()=>expect(onClose).toHaveBeenCalledOnce());expect(onDone).toHaveBeenCalledOnce(); }
  else {
    expect(await screen.findByRole("alert")).toHaveTextContent(status==="partial" ? "One file was kept" : status==="running" ? "still running" : "result is uncertain");
    expect(onClose).not.toHaveBeenCalled();
    expect(onDone).toHaveBeenCalledTimes(status==="partial" ? 1 : 0);
    expect(confirm).toBeDisabled();
  }
  expect(fetch.mock.calls.filter(([url])=>url.endsWith("/apply"))).toHaveLength(1);
});

it.each(["delete-albums","delete-track"] as const)("recovers a lost %s response from durable status without resubmitting", async kind => {
  const fetch=vi.fn(async(url:string)=>{
    if(url.endsWith("/preview"))return {ok:true,json:async()=>({preview_id:"delete",fingerprint:"hash",count:1,files:[{location:"Inbox",path:"Album/Bad.wav"}]})};
    if(url.endsWith("/apply"))throw new TypeError("Lost response");
    return {ok:true,json:async()=>({status:"complete",result:{message:"Deleted without importing"}})};
  });
  vi.stubGlobal("fetch",fetch);const onDone=vi.fn(),onClose=vi.fn();
  render(<ToastProvider><InboxActionDialog inboxId="manual" kind={kind} albumIds={[3]} albumId={3} trackId={4} onDone={onDone} onClose={onClose}/></ToastProvider>);
  const confirm=await screen.findByRole("button",{name:"Permanently delete from Inbox"});
  await waitFor(()=>expect(confirm).toBeEnabled());fireEvent.click(confirm);
  await waitFor(()=>expect(onClose).toHaveBeenCalledOnce());
  expect(onDone).toHaveBeenCalledOnce();expect(fetch.mock.calls.filter(([url])=>url.endsWith("/apply"))).toHaveLength(1);
});
