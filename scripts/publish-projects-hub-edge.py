#!/usr/bin/env python3
"""Publish the one allowlisted Projects Hub HTTPS edge route."""
from __future__ import annotations

import argparse
import json

from runtime.projects_hub_edge_operator import (
    ProjectsHubEdgeOperator,
    TARGET_HOSTNAME,
    TARGET_PORT,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idempotency-key", required=True)
    args = parser.parse_args()
    receipt = ProjectsHubEdgeOperator().publish_edge_route(
        hostname=TARGET_HOSTNAME,
        loopback_port=TARGET_PORT,
        idempotency_key=args.idempotency_key,
    )
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
