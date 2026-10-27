import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { BulkImportDialog, type ImportRow } from "./BulkImportDialog";
const rows: ImportRow[] = [{ id:1,album:"First",status:"queued" }, { id:2,album:"Second",status:"queued" }];
afterEach(cleanup);
function show(execute = vi.fn(), extra = {}) {
  const props = { rows, source:"library-import" as const, execute, onRunning:vi.fn(), onFinish:vi.fn(), onClose:vi.fn(), ...extra };
  render(<BulkImportDialog {...props}/>);
  return props;
}
it("requires themed confirmation and cancel does not import", () => {
  const props = show();
  expect(screen.getByRole("dialog")).toHaveAttribute("open");
  expect(props.execute).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name:"Cancel" }));
  expect(props.onClose).toHaveBeenCalledOnce();
});
it("shows live sequential progress and stops before the next album", async () => {
  let finish: ((value: unknown) => void) | undefined;
  const execute = vi.fn(() => new Promise(resolve => { finish=resolve; }));
  const props = show(execute);
  fireEvent.click(screen.getByRole("button", { name:"Start import" }));
  expect(execute).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Importing…")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name:"Stop after current album" }));
  finish?.({ imported:[{ album_review_id:1 }] });
  await screen.findByRole("heading", { name:"Import results" });
  expect(execute).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Imported", { exact:true })).toBeInTheDocument();
  expect(screen.getByText("Not started")).toBeInTheDocument();
  expect(props.onFinish).toHaveBeenCalledWith(expect.arrayContaining([expect.objectContaining({id:1,status:"imported"}),expect.objectContaining({id:2,status:"not-started"})]));
});
it("continues after a reported per-album failure and retains blocked reasons", async () => {
  const props = show(vi.fn(async id => id===1 ? { failed:[{error:"File changed"}] } : { skipped:[{reason:"Duplicate needs review"}] }));
  fireEvent.click(screen.getByRole("button", { name:"Start import" }));
  await waitFor(() => expect(props.onFinish).toHaveBeenCalledOnce());
  expect(screen.getByText("File changed")).toBeInTheDocument();
  expect(screen.getByText("Duplicate needs review")).toBeInTheDocument();
});
it("does not retry an uncertain transport failure or start more albums", async () => {
  const props = show(vi.fn(async () => { throw new TypeError("Network disconnected"); }));
  fireEvent.click(screen.getByRole("button", { name:"Start import" }));
  await screen.findByRole("heading", { name:"Import results" });
  expect(props.execute).toHaveBeenCalledTimes(1);
  expect(screen.getByText("Check result")).toBeInTheDocument();
  expect(screen.getByText(/Check the Library and queue before retrying/)).toBeInTheDocument();
});

it("offers cleanup only for successful retained Inbox copies from this batch", async () => {
  const remove=vi.fn();
  show(vi.fn(async id => id===1 ? {imported:[{album_review_id:1,job_id:77}]} : {failed:[{error:"File changed"}]}),{source:"inbox",onRemoveImported:remove});
  expect(screen.queryByRole("button",{name:"Clear imported from Inbox"})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button",{name:"Start import"}));
  fireEvent.click(await screen.findByRole("button",{name:"Clear imported from Inbox"}));
  expect(remove).toHaveBeenCalledWith([77]);
});
it.each(["library-import","inbox"] as const)("omits cleanup for %s without retained originals", async source => {
  show(vi.fn(async id => ({imported:[{album_review_id:id,...(source==="library-import"?{job_id:77}:{})}]})),{source,onRemoveImported:vi.fn()});
  fireEvent.click(screen.getByRole("button",{name:"Start import"}));
  await screen.findByRole("heading",{name:"Import results"});
  expect(screen.queryByRole("button",{name:"Clear imported from Inbox"})).not.toBeInTheDocument();
});
