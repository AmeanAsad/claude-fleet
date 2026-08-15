import type { Worker, Machine, FleetConfig, TaskInfo, Message } from "./types";

function getToken(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem("cfleet_token") || "";
}

export function setToken(token: string) {
  localStorage.setItem("cfleet_token", token);
}

export function getStoredToken(): string {
  return getToken();
}

async function apiFetch<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers: Record<string, string> = {
    ...(opts.headers as Record<string, string>),
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (opts.body && typeof opts.body === "string") {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(`/api${path}`, { ...opts, headers });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || "Request failed");
  }
  return res.json();
}

export async function fetchWorkers(): Promise<Worker[]> {
  return apiFetch("/workers");
}

export async function fetchWorker(name: string): Promise<Worker> {
  return apiFetch(`/workers/${encodeURIComponent(name)}`);
}

export async function fetchMachines(): Promise<Machine[]> {
  return apiFetch("/machines");
}

export async function fetchConfig(): Promise<FleetConfig> {
  return apiFetch("/config");
}

export async function fetchTasks(): Promise<TaskInfo[]> {
  return apiFetch("/tasks");
}

export interface MessagesPage {
  messages: Message[];
  total: number;
  head: number;
  has_more: boolean;
}

/**
 * Tail-first paginated message fetch.
 *
 * - Default: returns the last `limit` messages (newest window).
 * - Pass `before=<head-of-current-window>` to fetch the previous chunk when
 *   the user scrolls to the top of the list ("Load older" affordance).
 *
 * The response `head` is the index of the first message in the returned
 * window — use it as the next `before` value to keep paging backward.
 */
export async function fetchMessages(
  name: string,
  opts: { limit?: number; before?: number } = {},
): Promise<MessagesPage> {
  const limit = opts.limit ?? 200;
  const params = new URLSearchParams({ limit: String(limit) });
  if (opts.before !== undefined) {
    params.set("before", String(opts.before));
  }
  const res = await apiFetch<Partial<MessagesPage>>(
    `/workers/${encodeURIComponent(name)}/messages?${params.toString()}`,
  );
  return {
    messages: res.messages ?? [],
    total: res.total ?? 0,
    head: res.head ?? 0,
    has_more: res.has_more ?? false,
  };
}

export async function sendPrompt(
  name: string,
  prompt: string,
): Promise<void> {
  await apiFetch(`/workers/${encodeURIComponent(name)}/ask`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function interruptWorker(name: string): Promise<void> {
  await apiFetch(`/workers/${encodeURIComponent(name)}/interrupt`, {
    method: "POST",
  });
}

export async function killWorker(name: string): Promise<void> {
  await apiFetch(`/workers/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
}

export interface SpawnRequest {
  name: string;
  provider?: string;
  vm_type?: string;
  model?: string;
  instance_type?: string;
  region?: string;
  machine_name?: string;
  cwd?: string;
  agent_backend?: string;
}

export async function spawnWorker(
  req: SpawnRequest,
): Promise<{ task_id: string }> {
  return apiFetch("/workers", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function fetchFleetUsage(): Promise<{
  worker_count: number;
  total_cost_usd: number;
  workers: Record<string, { total_cost_usd: number }>;
}> {
  return apiFetch("/fleet/usage");
}

export function createLogStream(
  name: string,
  onMessage: (msg: Message) => void,
  onStatus?: (data: Record<string, unknown>) => void,
): EventSource {
  const token = getToken();
  const url = `/api/workers/${encodeURIComponent(name)}/logs${token ? `?token=${encodeURIComponent(token)}` : ""}`;
  const source = new EventSource(url);

  source.addEventListener("message", (e) => {
    try {
      const data = JSON.parse(e.data);
      if (data.message) onMessage(data.message);
    } catch {
      /* ignore */
    }
  });

  source.addEventListener("status", (e) => {
    try {
      const data = JSON.parse(e.data);
      onStatus?.(data);
    } catch {
      /* ignore */
    }
  });

  return source;
}
