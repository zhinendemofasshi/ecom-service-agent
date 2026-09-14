"""Send and read back one content-free trace; never calls a model."""
import logging
import time
from uuid import uuid4

from app.config.settings import settings


def main():
    # Avoid echoing SDK/server error payloads or credentials on operational checks.
    logging.disable(logging.CRITICAL)
    if not settings.langsmith_tracing or not settings.langsmith_api_key:
        print("Tracing is disabled or LANGSMITH_API_KEY is missing.")
        return 1
    from langsmith import Client
    from langsmith.run_trees import RunTree
    try:
        client = Client(api_url=settings.langsmith_endpoint, api_key=settings.langsmith_api_key,
                        timeout_ms=5000, auto_batch_tracing=False,
                        hide_inputs=True, omit_traced_runtime_info=True)
        run = RunTree(name="deployment.smoke", run_type="chain", inputs={},
                      id=uuid4(), client=client, project_name=settings.langsmith_project,
                      tags=["deployment-smoke"])
        run.post()
        run.end(outputs={"success": True})
        run.patch()
        for _ in range(5):
            try:
                recorded = client.read_run(run.id)
                if recorded.end_time is not None:
                    print("Trace export and readback verified. Trace ID: " + str(run.id))
                    return 0
            except Exception:
                pass
            time.sleep(1)
        print("Trace sent, but readback was not confirmed.")
        return 1
    except Exception as exc:
        print("Trace verification failed: " + type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
