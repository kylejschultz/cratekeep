import { afterEach, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
afterEach(()=>vi.unstubAllGlobals());
it.each(["120",new Date(Date.now()+120000).toUTCString()])("preserves proxy Retry-After %s",async header=>{
  vi.stubGlobal("fetch",vi.fn(async()=>({ok:false,status:503,headers:new Headers({"Retry-After":header}),json:async()=>({})})));
  await expect(api("/api/example")).rejects.toMatchObject({retryable:true,retryAfterSeconds:expect.any(Number)});
  try {await api("/api/example");}catch(reason){expect((reason as ApiError).retryAfterSeconds).toBeGreaterThan(118);}
});
it("retains structured cooldown metadata and nonretryable errors",async()=>{
  vi.stubGlobal("fetch",vi.fn(async()=>({ok:false,status:409,headers:new Headers(),json:async()=>({error:"Changed files",code:"inventory_stale",retryable:false,retry_after_seconds:0})})));
  await expect(api("/api/example")).rejects.toMatchObject({code:"inventory_stale",retryable:false,retryAfterSeconds:0});
});
