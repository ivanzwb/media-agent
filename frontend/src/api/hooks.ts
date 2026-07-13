import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { getJson } from "./client";

export function useLocalState<T>(key: string, initial: T): [T, (v: T | ((prev: T) => T)) => void] {
  const [val, setVal] = useState<T>(() => {
    try { const s = localStorage.getItem(key); return s !== null ? JSON.parse(s) : initial; } catch { return initial; }
  });
  useEffect(() => { try { localStorage.setItem(key, JSON.stringify(val)); } catch { /* ignore */ } }, [key, val]);
  return [val, setVal];
}

export interface LicenseStatus {
  active: boolean;
  dev: boolean;
  edition?: string;
  reason?: string;
  expires_at?: string | null;
  machine_code?: string;
  features?: string[];
}

export interface RunStatus {
  running: boolean;
  paused: boolean;
  stopped: boolean;
  error: string | null;
  stats: { fetched?: number; archived?: number; classified?: number; drafted?: number };
  logs: string[];
  source_current: number;
  source_total: number;
}

export function useLicense() {
  return useQuery({
    queryKey: ["license"],
    queryFn: () => getJson<LicenseStatus>("/api/license/status"),
    staleTime: 30_000,
  });
}

export function useRunStatus(poll: boolean) {
  return useQuery({
    queryKey: ["run-status"],
    queryFn: () => getJson<RunStatus>("/api/run-status"),
    refetchInterval: poll ? 1200 : false,
  });
}
