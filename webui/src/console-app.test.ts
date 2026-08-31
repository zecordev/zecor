import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "./console-app";

const STATE = {
  stopped: false,
  cards: [{ id: "0043", repo: "macro-logbook", intent: "x", state: "blocked", zombie: false, move: null }],
  health: { counts: { blocked: 1, landed: 4 } },
  questions: [{ id: 7, card_id: "0043", reason: "gate exits 127" }],
  momentum: "✦ 4",
  latest_turn: 2,
};
const TURNS = {
  turns: [
    { id: 1, role: "you", kind: "message", text: "why is 0043 stuck?", card_id: null },
    { id: 2, role: "zecor", kind: "reply", text: "no verify.sh yet", card_id: null },
  ],
  latest: 2,
};

describe("<zecor-console>", () => {
  beforeEach(() => {
    vi.stubGlobal("EventSource", class {
      onmessage: (() => void) | null = null;
      onerror: (() => void) | null = null;
      close() {}
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        const path = new URL(url, "http://x").pathname;
        const body =
          path === "/api/state" ? STATE : path === "/api/transcript" ? TURNS : { ok: true, message: "" };
        return { ok: true, json: async () => body } as Response;
      }),
    );
  });
  afterEach(() => vi.unstubAllGlobals());

  it("renders the status flag, the transcript and an open question", async () => {
    const el = document.createElement("zecor-console");
    document.body.append(el);
    await new Promise((r) => setTimeout(r, 0));
    await (el as any).updateComplete;
    const text = el.shadowRoot?.textContent ?? "";
    expect(text).toContain("GO");
    expect(text).toContain("no verify.sh yet");
    expect(text).toContain("0043 needs you");
    el.remove();
  });

  it("posts a chat line on Enter", async () => {
    const el = document.createElement("zecor-console");
    document.body.append(el);
    await new Promise((r) => setTimeout(r, 0));
    await (el as any).updateComplete;
    const input = el.shadowRoot?.querySelector("#chat") as HTMLInputElement;
    input.value = "stop";
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" }));
    await new Promise((r) => setTimeout(r, 0));
    const calls = (fetch as any).mock.calls.map((c: unknown[]) => String(c[0]));
    expect(calls.some((u: string) => u.includes("/api/say"))).toBe(true);
    el.remove();
  });
});
