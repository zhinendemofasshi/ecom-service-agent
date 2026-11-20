import { createSSEParser, subscribeEvents } from "./sse";
import type { AgentEvent } from "./types";

const frame = (seq: number, type = "token_delta", extra: object = { delta: `t${seq}` }) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ sequence: seq, type, request_id: "r1", timestamp: "", ...extra })}\n\n`;

function streamOf(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return new Response(body, { status: 200 });
}

describe("createSSEParser", () => {
  it("handles frames split across chunks, CRLF and keepalive comments", () => {
    const frames: string[] = [];
    const feed = createSSEParser((f) => frames.push(`${f.id}|${f.event}|${f.data}`));
    feed(": keepalive\n\nid: 1\r\nevent: a\r\nda");
    feed('ta: {"x":1}\r\n\r\nid: 2\nevent: b\ndata: line1\ndata: line2\n');
    expect(frames).toEqual(['1|a|{"x":1}']);
    feed("\n");
    expect(frames).toEqual(['1|a|{"x":1}', "2|b|line1\nline2"]);
  });
});

describe("subscribeEvents", () => {
  it("reconnects with the latest cursor and drops duplicate events", async () => {
    const controller = new AbortController();
    const seen: number[] = [];
    const urls: string[] = [];
    const responses = [
      streamOf([frame(1), frame(2)]),
      // Server replays 2 after reconnect; it must not be delivered twice.
      streamOf([frame(2), frame(3)]),
    ];
    const fetchImpl = vi.fn(async (url: string) => {
      urls.push(url);
      const next = responses.shift();
      if (!next) {
        controller.abort();
        throw new DOMException("aborted", "AbortError");
      }
      return next;
    }) as unknown as typeof fetch;

    await subscribeEvents({
      sessionId: "s1",
      after: 0,
      signal: controller.signal,
      fetchImpl,
      sleep: async () => {},
      onResync: async () => 0,
      onEvent: (e: AgentEvent) => seen.push(e.sequence),
    });

    expect(seen).toEqual([1, 2, 3]);
    expect(urls[0]).toContain("after=0");
    expect(urls[1]).toContain("after=2");
    expect(urls[2]).toContain("after=3");
  });

  it("resyncs on 410 and backs off on server errors", async () => {
    const controller = new AbortController();
    const delays: number[] = [];
    const urls: string[] = [];
    const responses = [new Response(null, { status: 410 }), new Response(null, { status: 502 }), streamOf([frame(43)])];
    const fetchImpl = vi.fn(async (url: string) => {
      urls.push(url);
      const next = responses.shift();
      if (!next) {
        controller.abort();
        throw new DOMException("aborted", "AbortError");
      }
      return next;
    }) as unknown as typeof fetch;
    const seen: number[] = [];

    await subscribeEvents({
      sessionId: "s1",
      after: 5,
      signal: controller.signal,
      fetchImpl,
      sleep: async (ms) => void delays.push(ms),
      onResync: async () => 42,
      onEvent: (e) => seen.push(e.sequence),
    });

    expect(urls[1]).toContain("after=42");
    expect(delays).toHaveLength(1);
    expect(delays[0]).toBeGreaterThanOrEqual(500);
    expect(seen).toEqual([43]);
  });

  it("stops on auth errors instead of retrying forever", async () => {
    const fetchImpl = vi.fn(async () => new Response(null, { status: 401 })) as unknown as typeof fetch;
    await expect(
      subscribeEvents({
        sessionId: "s1",
        after: 0,
        signal: new AbortController().signal,
        fetchImpl,
        onResync: async () => 0,
        onEvent: () => {},
      }),
    ).rejects.toThrow("401");
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
