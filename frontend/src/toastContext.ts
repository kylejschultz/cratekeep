import { createContext, useContext } from "react";

export type Kind = "success" | "info";
export const ToastContext = createContext<((text: string, kind?: Kind) => void) | null>(null);

export function useToast() {
  const notify = useContext(ToastContext);
  if (!notify) throw new Error("ToastProvider is required");
  return notify;
}
