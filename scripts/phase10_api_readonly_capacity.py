"""Bounded read-only API capacity probe for P10-09.

This script deliberately exercises only dependency readiness and authenticated
identity reads. It never posts data, starts a model request, or controls a
service process. The default windows match Phase 10 section 12.3; short
windows are available for a harness smoke check before the full run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * p
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def read_request(
    client: httpx.Client, path: str, token: str, timeout: float
) -> tuple[float, int | None, str | None]:
    headers = {"Accept": "application/json"}
    if path.endswith("/me"):
        headers["Authorization"] = f"Bearer {token}"
    started = time.perf_counter()
    try:
        with client.stream("GET", path, headers=headers, timeout=timeout) as response:
            status = response.status_code
            response.read()
        return (time.perf_counter() - started) * 1000.0, status, None
    except (httpx.HTTPError, OSError) as error:
        return (time.perf_counter() - started) * 1000.0, None, type(error).__name__


def run_window(
    *,
    base_url: str,
    token: str,
    workers: int,
    seconds: int,
    interval: float,
    timeout: float,
    phase: str,
    stop_event: threading.Event,
    max_failures: int,
) -> dict[str, object]:
    started_at = utc_now()
    started = time.perf_counter()
    deadline = started + seconds
    lock = threading.Lock()
    latencies: list[float] = []
    status_counts: dict[str, int] = {}
    path_counts: dict[str, dict[str, int]] = {}
    errors: dict[str, int] = {}
    requests = 0
    failures = 0

    def worker(worker_id: int) -> None:
        nonlocal requests, failures
        index = worker_id
        paths = ("/health/ready", "/api/v1/auth/me")
        with httpx.Client(
            base_url=base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
            trust_env=False,
        ) as client:
            while time.perf_counter() < deadline and not stop_event.is_set():
                path = paths[index % len(paths)]
                index += 1
                latency, status, error = read_request(client, path, token, timeout)
                with lock:
                    requests += 1
                    latencies.append(latency)
                    key = str(status) if status is not None else "transport_error"
                    status_counts[key] = status_counts.get(key, 0) + 1
                    path_statuses = path_counts.setdefault(path, {})
                    path_statuses[key] = path_statuses.get(key, 0) + 1
                    if error:
                        errors[error] = errors.get(error, 0) + 1
                    if status != 200:
                        failures += 1
                    if failures >= max_failures:
                        stop_event.set()
                if interval > 0:
                    time.sleep(interval)

    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix=f"p10-capacity-{phase}"
    ) as pool:
        futures = [pool.submit(worker, worker_id) for worker_id in range(workers)]
        for future in futures:
            future.result()
    elapsed = time.perf_counter() - started
    return {
        "phase": phase,
        "workers": workers,
        "window_seconds_requested": seconds,
        "window_seconds_actual": round(elapsed, 3),
        "started_at_utc": started_at,
        "finished_at_utc": utc_now(),
        "requests": requests,
        "throughput_per_second": round(requests / elapsed, 3) if elapsed else None,
        "status_counts": status_counts,
        "path_status_counts": path_counts,
        "errors": errors,
        "failure_count": failures,
        "safe_stop": stop_event.is_set(),
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 3) if latencies else None,
            "p95": round(percentile(latencies, 0.95), 3) if latencies else None,
            "p99": round(percentile(latencies, 0.99), 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
        "latency_sample_count": len(latencies),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--warmup-seconds", type=int, default=60)
    parser.add_argument("--sample-seconds", type=int, default=300)
    parser.add_argument("--levels", default="1,5,10")
    parser.add_argument("--interval-seconds", type=float, default=0.2)
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--max-failures", type=int, default=20)
    args = parser.parse_args()
    parsed_url = urlsplit(args.base_url)
    try:
        parsed_port = parsed_url.port
    except ValueError:
        parsed_port = None
    if (
        parsed_url.scheme != "http"
        or parsed_url.hostname != "127.0.0.1"
        or parsed_port is None
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.path not in {"", "/"}
        or parsed_url.query
        or parsed_url.fragment
    ):
        parser.error("base-url must be an HTTP loopback origin")
    if not args.run_id.strip() or any(char in args.run_id for char in "\\/:\r\n"):
        parser.error("run-id must be a non-empty path-safe value")
    if args.warmup_seconds <= 0 or args.sample_seconds <= 0 or args.max_failures <= 0:
        parser.error("windows and max-failures must be positive")
    if args.interval_seconds < 0 or args.timeout_seconds <= 0:
        parser.error("interval must be nonnegative and timeout must be positive")
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("token file is empty")
    levels = [int(value) for value in args.levels.split(",") if value.strip()]
    if (
        not levels
        or levels != sorted(set(levels))
        or any(level not in (1, 5, 10) for level in levels)
    ):
        parser.error("levels must be an ascending subset of 1,5,10")
    result: dict[str, object] = {
        "schema_version": "p10-09-readonly-api-capacity-v2",
        "run_id": args.run_id,
        "started_at_utc": utc_now(),
        "base_url": args.base_url,
        "paths": ["GET /health/ready", "GET /api/v1/auth/me"],
        "token": {
            "source": str(args.token_file),
            "sha256_prefix": token_fingerprint(token),
            "value_logged": False,
        },
        "model_calls": 0,
        "writes": 0,
        "dependency_mode": "real shared API; read-only readiness and authenticated identity route",
        "transport_mode": "one HTTP client per probe worker; responses fully consumed for keep-alive",
        "window_policy": {
            "warmup_seconds": args.warmup_seconds,
            "sample_seconds": args.sample_seconds,
            "max_failures": args.max_failures,
        },
        "levels": [],
    }
    # A single preflight request confirms that a stale token does not turn the
    # capacity run into a meaningless 401 measurement.
    preflight: dict[str, object] = {}
    with httpx.Client(base_url=args.base_url, trust_env=False) as client:
        for path in ("/health/ready", "/api/v1/auth/me"):
            latency, status, error = read_request(
                client, path, token, args.timeout_seconds
            )
            preflight[path] = {
                "status": status,
                "error": error,
                "latency_ms": round(latency, 3),
            }
    result["preflight"] = preflight
    if any(item["status"] != 200 for item in preflight.values()):
        result["status"] = "blocked_token_or_api"
        result["finished_at_utc"] = utc_now()
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return 2
    stop_event = threading.Event()
    for workers in levels:
        warmup = run_window(
            base_url=args.base_url,
            token=token,
            workers=workers,
            seconds=args.warmup_seconds,
            interval=args.interval_seconds,
            timeout=args.timeout_seconds,
            phase=f"warmup-{workers}",
            stop_event=stop_event,
            max_failures=args.max_failures,
        )
        sample = run_window(
            base_url=args.base_url,
            token=token,
            workers=workers,
            seconds=args.sample_seconds,
            interval=args.interval_seconds,
            timeout=args.timeout_seconds,
            phase=f"sample-{workers}",
            stop_event=stop_event,
            max_failures=args.max_failures,
        )
        result["levels"].append(
            {"workers": workers, "warmup": warmup, "sample": sample}
        )
        if stop_event.is_set():
            break
    any_failures = any(
        window["failure_count"] > 0
        for level in result["levels"]
        for window in (level["warmup"], level["sample"])
    )
    result["status"] = (
        "safe_stopped"
        if stop_event.is_set()
        else "completed_with_failures"
        if any_failures
        else "completed"
    )
    result["finished_at_utc"] = utc_now()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "out": str(args.out),
                "status": result["status"],
                "levels": len(result["levels"]),
                "preflight_status": 200,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
