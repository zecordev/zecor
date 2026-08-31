# command-center PWA

A Lit client for `fleet serve`. One installable page: the transcript, a chat box, the
board, open questions, and a STOP toggle. Responsive — chat-only on a phone, chat + board
on a wide screen. Offline-capable (service worker caches the shell); reconnects on its
own.

## Build

```
cd webui
pnpm install
pnpm run build     # -> webui/dist/, which `zecor serve` serves as static files
pnpm test          # Vitest + jsdom
```

## Run

```
fleet serve --bind <tailnet-ip> --token <secret>
```

Then open `http://<tailnet-ip>:8787/?t=<secret>` on the phone (Tailscale up). The token
is stashed in `localStorage` and stripped from the URL on first load; every `/api/*`
call carries it, the static shell does not.

## Stack

Lit 3 (web components), Vite, TypeScript, Vitest, pnpm — matching `platform-toolkit`. No
React, no framework runtime beyond Lit.
