import { LitElement, css, html, nothing } from "lit";
import { customElement, state } from "lit/decorators.js";
import { api, type Card, type Question, type State, type Turn } from "./api";

// The command-center PWA. One element: a status strip, the transcript, a chat box, a
// collapsible board, and any open questions. It polls `/api/state` every 3s and holds
// an SSE connection that pokes an immediate refresh when the server changes.

@customElement("zecor-console")
export class ZecorConsole extends LitElement {
  static styles = css`
    :host {
      display: grid;
      grid-template-rows: auto 1fr auto;
      height: 100dvh;
      background: #0f1210;
      color: #e7ece6;
    }
    header {
      padding: 0.5rem 0.75rem;
      font: 12px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
      color: #93a099;
      border-bottom: 1px solid #28302a;
      display: flex;
      gap: 0.75rem;
      flex-wrap: wrap;
      align-items: center;
    }
    .flag { font-weight: 700; }
    .go { color: #79b98a; }
    .stop { color: #e0736b; }
    main { overflow-y: auto; padding: 0.5rem 0.75rem; }
    .turn { margin: 0.35rem 0; white-space: pre-wrap; word-break: break-word; }
    .who { font: 11px ui-monospace, monospace; color: #93a099; margin-right: 0.4rem; }
    .you .who { color: #7fd0c0; }
    .q { border: 1px solid #d29a4d; border-radius: 0.4rem; padding: 0.5rem; margin: 0.4rem 0; }
    .q input { width: 100%; margin-top: 0.35rem; }
    footer {
      display: flex;
      gap: 0.4rem;
      padding: 0.5rem 0.75rem;
      border-top: 1px solid #28302a;
      padding-bottom: max(0.5rem, env(safe-area-inset-bottom));
    }
    input, button {
      font: inherit;
      background: #161a16;
      color: inherit;
      border: 1px solid #28302a;
      border-radius: 0.4rem;
      padding: 0.5rem 0.6rem;
    }
    footer input { flex: 1; }
    button.danger { border-color: #6b1d1d; }
    details.board { border-top: 1px solid #28302a; padding: 0.4rem 0.75rem; }
    .card { font: 12px ui-monospace, monospace; padding: 0.15rem 0; }
    .card .st { display: inline-block; width: 10rem; color: #93a099; }
    .zombie { color: #e0736b; }
    @media (min-width: 900px) {
      :host { grid-template-columns: 2fr 1fr; grid-template-rows: auto 1fr auto; }
      header { grid-column: 1 / -1; }
      details.board { grid-row: 2 / 4; grid-column: 2; border-top: 0; border-left: 1px solid #28302a; overflow-y: auto; }
      footer { grid-column: 1; }
    }
  `;

  @state() private st: State | null = null;
  @state() private turns: Turn[] = [];
  @state() private busy = false;

  private lastTurn = 0;
  private timer = 0;
  private stop?: () => void;

  connectedCallback(): void {
    super.connectedCallback();
    void this.refresh();
    this.timer = window.setInterval(() => void this.refresh(), 3000);
    this.stop = api.stream(() => void this.refresh());
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("./sw.js").catch(() => {});
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    window.clearInterval(this.timer);
    this.stop?.();
  }

  private async refresh(): Promise<void> {
    try {
      this.st = await api.state();
      const t = await api.transcript(this.lastTurn);
      if (t.turns.length) {
        this.turns = [...this.turns, ...t.turns].slice(-400);
        this.lastTurn = t.latest;
      }
    } catch {
      /* offline / token missing -- keep the last render */
    }
  }

  private async send(text: string): Promise<void> {
    const v = text.trim();
    if (!v || this.busy) return;
    this.busy = true;
    try {
      await api.say(v);
    } finally {
      this.busy = false;
      await this.refresh();
    }
  }

  private async verb(v: string, args: Record<string, string> = {}): Promise<void> {
    await api.intent(v, args);
    await this.refresh();
  }

  render() {
    const s = this.st;
    const stopped = s?.stopped ?? false;
    return html`
      <header>
        <span class="flag ${stopped ? "stop" : "go"}">${stopped ? "STOP" : "GO"}</span>
        <span>${this.counts()}</span>
        ${s?.momentum ? html`<span style="color:#79b98a">${s.momentum}</span>` : nothing}
        <span style="flex:1"></span>
        <button @click=${() => this.verb(stopped ? "go" : "stop")}>
          ${stopped ? "clear STOP" : "STOP"}
        </button>
      </header>

      <main id="log">
        ${this.turns.map(
          (t) => html`<div class="turn ${t.role}">
            <span class="who">${t.role}${t.kind === "question" ? " ?" : ""}</span>${t.text}
          </div>`,
        )}
        ${(s?.questions ?? []).map((q) => this.questionCard(q))}
      </main>

      <footer>
        <input
          id="chat"
          placeholder="talk to zecor  ·  stop / retry 0043 with opus / …"
          ?disabled=${this.busy}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === "Enter") {
              const el = e.target as HTMLInputElement;
              void this.send(el.value);
              el.value = "";
            }
          }}
        />
      </footer>

      <details class="board" open>
        <summary>${(s?.cards ?? []).length} cards</summary>
        ${(s?.cards ?? []).map((c) => this.cardRow(c))}
      </details>
    `;
  }

  private counts(): string {
    const c = (this.st?.health?.["counts"] as Record<string, number>) ?? {};
    return Object.entries(c)
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
  }

  private cardRow(c: Card) {
    return html`<div class="card ${c.zombie ? "zombie" : ""}">
      <span class="st">${c.zombie ? "ZOMBIE" : c.state}</span>${c.id} ${c.repo}
    </div>`;
  }

  private questionCard(q: Question) {
    return html`<div class="q">
      <b>${q.card_id} needs you</b> — ${q.reason}
      <input
        placeholder="answer (Enter)"
        @keydown=${(e: KeyboardEvent) => {
          if (e.key === "Enter") {
            const el = e.target as HTMLInputElement;
            void this.verb("answer", { qid: String(q.id), text: el.value });
            el.value = "";
          }
        }}
      />
    </div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "zecor-console": ZecorConsole;
  }
}
