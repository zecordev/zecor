// Thin client for `zecor serve`. The token arrives once as `?t=<token>`, is moved to
// localStorage, and is stripped from the URL so it does not sit in history or a share.

const TOKEN_KEY = "zecor.token";

function readToken(): string {
  try {
    const u = new URL(location.href);
    const t = u.searchParams.get("t");
    if (t) {
      localStorage.setItem(TOKEN_KEY, t);
      u.searchParams.delete("t");
      history.replaceState(null, "", u.toString());
      return t;
    }
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

const token = readToken();

function qs(): string {
  return token ? `?t=${encodeURIComponent(token)}` : "";
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${path}${qs()}`, { headers: { "Cache-Control": "no-store" } });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return (await r.json()) as T;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${path}${qs()}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  return (await r.json()) as T;
}

export interface Card {
  id: string;
  repo: string;
  intent: string;
  state: string;
  zombie: boolean;
  move: string | null;
}
export interface Question {
  id: number;
  card_id: string;
  reason: string;
}
export interface State {
  stopped: boolean;
  cards: Card[];
  health: Record<string, unknown>;
  questions: Question[];
  momentum: string;
  latest_turn: number;
}
export interface Turn {
  id: number;
  role: string;
  kind: string;
  text: string;
  card_id: string | null;
}
export interface SayResult {
  ok: boolean;
  kind?: string;
  message: string;
  proposed?: { verb: string; args: Record<string, string> }[];
}

export const api = {
  state: () => get<State>("/api/state"),
  transcript: (since: number) =>
    get<{ turns: Turn[]; latest: number }>(`/api/transcript?since=${since}`),
  roadmap: () => get<{ nodes: unknown[] }>("/api/roadmap"),
  metrics: () => get<Record<string, unknown>>("/api/metrics"),
  say: (text: string) => post<SayResult>("/api/say", { text }),
  intent: (verb: string, args: Record<string, string> = {}) =>
    post<{ ok: boolean; message: string }>("/api/intent", { verb, args }),
  // SSE: fires `cb` whenever the transcript or board changes server-side.
  stream(cb: () => void): () => void {
    const es = new EventSource(`/api/stream${qs()}`);
    es.onmessage = () => cb();
    es.onerror = () => {
      /* EventSource auto-reconnects; nothing to do */
    };
    return () => es.close();
  },
};
