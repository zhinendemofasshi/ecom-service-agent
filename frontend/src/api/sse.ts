import { authHeaders } from "./client";
import type { AgentEvent } from "./types";

export interface SSEFrame {
  id?: string;
  event?: string;
  data: string;
}

/**
 * Incremental text/event-stream parser. Chunks can split anywhere (mid-line,
 * mid-UTF-8 sequence is handled by TextDecoder upstream), so it buffers until
 * a blank line terminates a frame. Comment lines (": keepalive") are dropped.
 */
export function createSSEParser(onFrame: (frame: SSEFrame) => void) {
  let buffer = "";
  return (chunk: string) => {
    buffer += chunk.replace(/\r\n?/g, "\n");
    let end: number;
    while ((end = buffer.indexOf("\n\n")) >= 0) {
      const raw = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const frame: SSEFrame = { data: "" };
      const data: string[] = [];
      for (const line of raw.split("\n")) {
        if (!line || line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon < 0 ? line : line.slice(0, colon);
        const value = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
        if (field === "data") data.push(value);
        else if (field === "id") frame.id = value;
        else if (field === "event") frame.event = value;
      }
      if (data.length) {
        frame.data = data.join("\n");
        onFrame(frame);
      }
    }
  };
}

export interface SubscribeOptions {
  sessionId: string;
  /** Resume after this sequence; events <= cursor are never delivered twice. */
  after: number;
  onEvent: (event: AgentEvent) => void;
  /** Cursor fell out of the server's replay window (HTTP 410): reload state. */
  onResync: () => Promise<number>;
  onStatus?: (status: "live" | "reconnecting") => void;
  signal: AbortSignal;
  /** Injected for tests. */
  fetchImpl?: typeof fetch;
  sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Long-lived event subscription. EventSource cannot send an Authorization
 * header, so this uses fetch + ReadableStream. The server closes each response
 * after a timeout or event limit; we reconnect with `after=<cursor>`, backing
 * off exponentially (with jitter) only on failures.
 */
export async function subscribeEvents(opts: SubscribeOptions): Promise<void> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  const sleep = opts.sleep ?? defaultSleep;
  let cursor = opts.after;
  let failures = 0;

  while (!opts.signal.aborted) {
    try {
      const url = `/sessions/${encodeURIComponent(opts.sessionId)}/events?after=${cursor}&timeout_seconds=25`;
      const response = await fetchImpl(url, { headers: authHeaders(), signal: opts.signal });
      if (response.status === 410) {
        cursor = await opts.onResync();
        continue;
      }
      if (response.status === 401 || response.status === 403 || response.status === 404) {
        throw Object.assign(new Error(`events ${response.status}`), { fatal: true });
      }
      if (!response.ok || !response.body) throw new Error(`events ${response.status}`);

      failures = 0;
      opts.onStatus?.("live");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      const parse = createSSEParser((frame) => {
        let event: AgentEvent;
        try {
          event = JSON.parse(frame.data);
        } catch {
          return;
        }
        if (event.sequence <= cursor) return; // de-duplicate across reconnects
        cursor = event.sequence;
        opts.onEvent(event);
      });
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        parse(decoder.decode(value, { stream: true }));
      }
    } catch (error) {
      if (opts.signal.aborted) return;
      if ((error as { fatal?: boolean }).fatal) throw error;
      failures += 1;
      opts.onStatus?.("reconnecting");
      const delay = Math.min(1000 * 2 ** (failures - 1), 10_000);
      await sleep(delay / 2 + Math.random() * (delay / 2));
    }
  }
}
