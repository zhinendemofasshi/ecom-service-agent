"""Provision an idempotent, project-scoped LangSmith dashboard (--apply required)."""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from app.config.settings import settings


def charts(project_id):
    def series(name, field, filter, percentile=None):
        metric = ({"type":"percentile", "field":field, "params":{"p":percentile}}
                  if percentile is not None else {"type":"sum", "field":field})
        return {"name":name, "metric_definition":metric,
                "filters":{"session":[project_id], "filter":filter}}
    def count(name, filter):
        return {"name":name, "metric_definition":{"type":"count"},
                "filters":{"session":[project_id], "filter":filter}}
    def chart(title, items):
        return {"title":title, "chart_type":"line", "series":items}
    return [
        chart("Requests", [count("Requests", 'eq(name, "agent.invoke")')]),
        chart("Active run latency", [series(label, "latency_seconds", 'eq(name, "agent.invoke")', p)
                                    for label, p in [("P50", .5), ("P95", .95)]]),
        chart("Model latency", [series(label, "latency_seconds", 'eq(run_type, "llm")', p)
                               for label, p in [("P50", .5), ("P95", .95)]]),
        chart("Model error rate", [{"name":"Error rate", "metric":"error_rate",
                                   "filters":{"session":[project_id], "filter":'eq(run_type, "llm")'}}]),
        chart("Tool success rate", [{"name":"Success rate", "filters":{
            "session":[project_id], "filter":'eq(name, "tool.execute")'},
            "metric_definition":{"type":"ratio", "numerator":{"type":"count", "filter":'eq(error, null)'},
                                 "denominator":{"type":"count"}}}]),
        chart("Tool latency", [series(label, "latency_seconds", 'eq(name, "tool.execute")', p)
                              for label, p in [("P50", .5), ("P95", .95)]]),
        chart("Model tokens", [series("Input", "prompt_tokens", 'eq(run_type, "llm")'),
                               series("Output", "completion_tokens", 'eq(run_type, "llm")')]),
        chart("Estimated model cost", [series("USD", "total_cost", 'eq(run_type, "llm")')]),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps(charts("<project-id>"), indent=2))
        return 0
    logging.disable(logging.CRITICAL)
    try:
        from langsmith import Client
        client = Client(api_url=settings.langsmith_endpoint, api_key=settings.langsmith_api_key,
                        timeout_ms=5000)
        project = client.read_project(project_name=settings.langsmith_project)
        http = requests.Session()
        http.headers["x-api-key"] = settings.langsmith_api_key
        base = settings.langsmith_endpoint.rstrip("/") + "/api/v1"
        def api(method, path, **kwargs):
            response = http.request(method, base + path, timeout=10, **kwargs)
            if not response.ok:
                raise RuntimeError(f"Dashboard API {method} {path}: HTTP {response.status_code}")
            return response.json()
        title = "Ecom Service / " + settings.langsmith_project
        sections = api("GET", "/charts/section", params={"title_contains":title})
        matching = [s for s in sections if s["title"] == title]
        if len(matching) > 1:
            raise RuntimeError("Ambiguous dashboard title")
        section = matching[0] if matching else api("POST", "/charts/section", json={
            "title":title, "description":"Content-free ecom service runtime metrics"})
        sid = section["id"]
        window = {"omit_data":True, "start_time":(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),
                  "end_time":datetime.now(timezone.utc).isoformat()}
        existing = api("POST", "/charts/section/" + sid, json=window)
        by_title = {c["title"]:c for c in existing["charts"]}
        for index, spec in enumerate(charts(str(project.id))):
            spec.update(section_id=sid, index=index)
            if spec["title"] in by_title:
                api("PATCH", "/charts/" + by_title[spec["title"]]["id"], json=spec)
            else:
                api("POST", "/charts/create", json=spec)
        saved = api("POST", "/charts/section/" + sid, json=window)
        if not {c["title"] for c in charts(str(project.id))} <= {c["title"] for c in saved["charts"]}:
            raise RuntimeError("Dashboard verification failed")
        print("Verified 8 dashboard charts. Section ID: " + sid)
        return 0
    except Exception as exc:
        # Never echo credential-bearing request headers or response bodies.
        print(str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
