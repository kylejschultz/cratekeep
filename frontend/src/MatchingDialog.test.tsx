import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { MatchingDialog } from "./MatchingDialog";
const albums=[{id:1,album:"First"},{id:2,album:"Second"}];
const success=(id: number, count=1, cached=false)=>({already_matched:cached,album:{id,candidate_status:"complete",candidates:count ? [{id:20}] : []}});
function show(execute: (id: number,reuse: boolean)=>Promise<any>) {
  const onFinish=vi.fn();render(<MatchingDialog albums={albums} execute={execute} onRunning={vi.fn()} onFinish={onFinish} onClose={vi.fn()}/>);
  return onFinish;
}
afterEach(()=>{cleanup();vi.useRealTimers();});
it("waits for explicit start, reuses saved results, and distinguishes no matches",async()=>{
  const execute=vi.fn(async id=>success(id,id===1?1:0,true));show(execute);
  expect(execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await screen.findByRole("heading",{name:"Matching results"});
  expect(execute).toHaveBeenNthCalledWith(1,1,true);
  expect(screen.getByText("Saved match",{exact:true})).toBeInTheDocument();
  expect(screen.getByText("No matches",{exact:true})).toBeInTheDocument();
});
it("can explicitly refresh saved matches",async()=>{
  const execute=vi.fn(async id=>success(id));show(execute);
  fireEvent.click(screen.getByLabelText("Refresh saved matches"));
  fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await screen.findByRole("heading",{name:"Matching results"});
  expect(execute).toHaveBeenNthCalledWith(1,1,false);
});
it("waits for cooldown before retrying the same album without advancing",async()=>{
  vi.useFakeTimers();let calls=0;
  const execute=vi.fn(async id=>++calls===1?{errors:[{error:"Busy",retryable:true,retry_after_seconds:12}]}:success(id));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await act(async()=>{});
  expect(screen.getByRole("status")).toHaveTextContent("12s remaining");
  await act(async()=>vi.advanceTimersByTimeAsync(11999));
  expect(execute).toHaveBeenCalledTimes(1);
  await act(async()=>vi.advanceTimersByTimeAsync(1));
  expect(execute.mock.calls.map(call=>call[0])).toEqual([1,1,2]);
  expect(screen.getByRole("heading",{name:"Matching results"})).toBeInTheDocument();
});
it("pauses after three attempts and resumes only unfinished albums",async()=>{
  vi.useFakeTimers();let failed=0;
  const execute=vi.fn(async id=>id===2&&failed++<3?{errors:[{error:"503",retryable:true,retry_after_seconds:1}]}:success(id));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await act(async()=>{});await act(async()=>vi.advanceTimersByTimeAsync(30000));
  expect(screen.getByRole("heading",{name:"Matching paused"})).toBeInTheDocument();
  expect(execute.mock.calls.map(call=>call[0])).toEqual([1,2,2,2]);
  expect(screen.getByRole("button",{name:"Resume unfinished"})).toBeDisabled();
  await act(async()=>vi.advanceTimersByTimeAsync(40000));
  fireEvent.click(screen.getByRole("button",{name:"Resume unfinished"}));await act(async()=>{});
  expect(execute.mock.calls.map(call=>call[0])).toEqual([1,2,2,2,2]);
  expect(screen.getByRole("heading",{name:"Matching results"})).toBeInTheDocument();
});
it("can stop immediately during cooldown without retrying or touching remaining albums",async()=>{
  vi.useFakeTimers();const execute=vi.fn(async()=>({errors:[{error:"503",retryable:true,retry_after_seconds:120}]}));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));await act(async()=>{});
  fireEvent.click(screen.getByRole("button",{name:"Stop after current album"}));await act(async()=>{});
  await act(async()=>vi.advanceTimersByTimeAsync(120000));
  expect(execute).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Not started")).toBeInTheDocument();
});
it("pauses instead of waiting beyond the bounded run",async()=>{
  const execute=vi.fn(async()=>({errors:[{error:"Long cooldown",retryable:true,retry_after_seconds:3600}]}));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await screen.findByRole("heading",{name:"Matching paused"});
  expect(execute).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button",{name:"Resume unfinished"})).toBeDisabled();
});
it("continues after nonretryable errors without calling an empty result a match",async()=>{
  const execute=vi.fn(async id=>id===1?{errors:[{error:"Bad source",retryable:false}]}:success(id,0));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await screen.findByRole("heading",{name:"Matching results"});
  expect(execute).toHaveBeenCalledTimes(2);
  expect(screen.getByText("Failed",{exact:true})).toBeInTheDocument();
  expect(screen.getByText("No matches",{exact:true})).toBeInTheDocument();
});
it("finishes an in-flight result but starts no next album after the run budget",async()=>{
  vi.useFakeTimers();let finish:((value:unknown)=>void)|undefined;
  const execute=vi.fn(()=>new Promise(resolve=>{finish=resolve;}));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await act(async()=>vi.advanceTimersByTimeAsync(16*60*1000));
  await act(async()=>{finish?.(success(1));});
  expect(execute).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("heading",{name:"Matching paused"})).toBeInTheDocument();
  expect(screen.getByText("Matched",{exact:true})).toBeInTheDocument();
  expect(screen.getByText("Not started",{exact:true})).toBeInTheDocument();
});
it("does not retry an album the server confirms is gone",async()=>{
  const execute=vi.fn(async id=>id===1?{album:null}:success(id));
  show(execute);fireEvent.click(screen.getByRole("button",{name:"Start matching"}));
  await screen.findByRole("heading",{name:"Matching results"});
  expect(execute.mock.calls.map(call=>call[0])).toEqual([1,2]);
  expect(screen.getByText(/Album is no longer available/)).toBeInTheDocument();
});
