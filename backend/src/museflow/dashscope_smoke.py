from __future__ import annotations

import argparse
import os

from museflow.providers import DASHSCOPE_SIZE, DashScopeProvider, GenerationRequest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one explicitly authorized DashScope smoke request."
    )
    parser.add_argument(
        "--authorize-real-request",
        action="store_true",
        help="confirm one real request, up to about CNY 0.20, with no automatic retry",
    )
    args = parser.parse_args()

    configured = {
        name: bool(os.environ.get(name)) for name in ("DASHSCOPE_API_KEY", "DASHSCOPE_API_HOST")
    }
    print(
        "DASHSCOPE_API_KEY configured:",
        "yes" if configured["DASHSCOPE_API_KEY"] else "no",
    )
    print(
        "DASHSCOPE_API_HOST configured:",
        "yes" if configured["DASHSCOPE_API_HOST"] else "no",
    )
    if not all(configured.values()):
        print("No request sent: both required environment variables must exist.")
        return 2
    if not args.authorize_real_request:
        print(
            "No request sent. Re-run with --authorize-real-request only after confirming "
            "Beijing region/workspace, model permission, billing, one request up to about "
            "CNY 0.20, no automatic retry, and immediate stop on any explicit error."
        )
        return 2

    provider = DashScopeProvider()
    try:
        result = provider.generate(
            GenerationRequest(
                prompt="A small ceramic lighthouse on a quiet blue sea at sunrise.",
                size_preset=DASHSCOPE_SIZE,
            ),
            request_key="controlled-smoke",
            remote_request_id=None,
        )
    finally:
        provider.close()

    print("Provider: dashscope")
    print("HTTP statuses:", result.metadata.get("poll_http_statuses", "unknown"))
    print("Task status sequence:", result.metadata.get("task_status_sequence", "unknown"))
    print(
        "Result:",
        result.metadata.get("content_type", "unknown"),
        result.metadata.get("width", "unknown"),
        "x",
        result.metadata.get("height", "unknown"),
        result.metadata.get("size_bytes", "unknown"),
        "bytes",
        result.metadata.get("sha256", "unknown"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
