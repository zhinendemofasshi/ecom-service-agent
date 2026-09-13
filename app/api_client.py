"""SSE client with durable caller-owned cursors and bounded reconnect backoff."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator

import httpx


def session_events(base_url: str, session_id: str, token: str, *, after: int = 0,
                   stop: Callable[[], bool] = lambda: False,
                   client: httpx.Client | None = None) -> Iterator[dict]:
    """Yield events; persist event['sequence'] after handling for process restart.

    Authentication/cursor errors are raised to the caller. Network errors and
    normal SSE EOF reconnect automatically. Delivery across restarts is at least once.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=65)
    cursor = after
    delay = .1
    try:
        while not stop():
            try:
                with client.stream("GET", f"{base_url.rstrip('/')}/sessions/{session_id}/events",
                                   headers={"Authorization": f"Bearer {token}", "Last-Event-ID": str(cursor)}) as response:
                    response.raise_for_status()
                    data = []
                    for line in response.iter_lines():
                        if stop():
                            return
                        if line.startswith("data:"):
                            data.append(line[5:].lstrip())
                        elif not line and data:
                            event = json.loads("\n".join(data))
                            data = []
                            if event["sequence"] > cursor:
                                yield event
                                cursor = event["sequence"]
                                delay = .1
            except httpx.TransportError:
                pass
            if not stop():
                time.sleep(delay)
                delay = min(delay * 2, 5)
    finally:
        if owns_client:
            client.close()
