#!/usr/bin/env python3
"""Generate and persist the weekly AI fund-manager portfolio snapshot.

This script is intended to run on the public server after data sync.
It does not push data anywhere and does not print secrets.
"""

from __future__ import annotations

import sys
import runpy
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "apps" / "fund-manager-workbench"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from server import Identity, WorkbenchService  # noqa: E402


def main() -> int:
    index_script = PROJECT_ROOT / "tools" / "Sync-IndexBenchmarks.py"
    if index_script.exists():
        namespace = runpy.run_path(str(index_script), run_name="consumer_research_index_sync")
        namespace["main"]()
    app = WorkbenchService(
        db_path=PROJECT_ROOT / "data" / "curated" / "consumer-research.db",
        data_root=PROJECT_ROOT / "data" / "workbench" / "module5-fund-manager",
        identity=Identity(name="system", role="public_fund_manager"),
        static_root=APP_DIR / "public",
        bound_host="127.0.0.1",
        deployment_mode="internal_network",
        client_scope="server_job",
        allowed_hosts={"127.0.0.1", "localhost"},
    )
    result = app.ai_fund_strategy()
    print(
        "AI fund snapshot generated:",
        result.get("version_id"),
        "date=" + str(result.get("date")),
        "ai_generated=" + str(result.get("ai_generated")),
        "cached=" + str(result.get("cached")),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
