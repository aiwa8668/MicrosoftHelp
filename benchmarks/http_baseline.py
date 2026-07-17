import argparse
import json
import statistics
import time
from urllib.request import urlopen


def measure(url: str, requests: int, timeout: float) -> dict[str, float | int | str]:
    durations: list[float] = []
    errors = 0
    started = time.perf_counter()
    for _ in range(requests):
        request_started = time.perf_counter()
        try:
            with urlopen(url, timeout=timeout) as response:
                response.read()
                if response.status >= 400:
                    errors += 1
        except Exception:
            errors += 1
        durations.append((time.perf_counter() - request_started) * 1000)
    elapsed = time.perf_counter() - started
    ordered = sorted(durations)
    percentile_index = max(0, min(len(ordered) - 1, round(len(ordered) * 0.95) - 1))
    return {
        "url": url,
        "requests": requests,
        "errors": errors,
        "seconds": round(elapsed, 4),
        "requests_per_second": round(requests / elapsed, 2),
        "latency_mean_ms": round(statistics.mean(durations), 3),
        "latency_p95_ms": round(ordered[percentile_index], 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5300/admin/connection-status")
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=5.0)
    arguments = parser.parse_args()
    if arguments.requests <= 0 or arguments.timeout <= 0:
        parser.error("requests 和 timeout 必须大于 0")
    print(json.dumps(measure(arguments.url, arguments.requests, arguments.timeout), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
