#!/usr/bin/env python3
"""Scan several Polymarket categories (tags) into the bot's catalog DB.

The stock `polymaker scan` only sweeps politics. This reuses the bot's own
scanner for each tag, so pick_markets.py can rank everything together.
Read-only public API calls (gamma-api / clob), no keys.

  uv run python my_tools/scan_tags.py                    # default tags
  uv run python my_tools/scan_tags.py --tags crypto sports
"""

from __future__ import annotations

import argparse
import asyncio

from polymaker.catalog.scanner import ScanConfig, run_scan
from polymaker.catalog.store import CatalogStore
from polymaker.config import Config

DEFAULT_TAGS = ["politics", "crypto", "sports", "economy", "finance", "tech",
                "geopolitics", "world", "culture", "business"]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", default=DEFAULT_TAGS)
    ap.add_argument("--min-liquidity", type=float, default=1000.0)
    a = ap.parse_args()
    cfg = Config.load("config")
    store = CatalogStore(cfg.paths.db)
    for tag in a.tags:
        try:
            kept = await run_scan(store, ScanConfig(tag_slug=tag, min_liquidity=a.min_liquidity,
                                                    rewards_only=False))
            print(f"{tag:>12}: {len(kept)} markets")
        except Exception as exc:  # noqa: BLE001 - one bad tag shouldn't stop the sweep
            print(f"{tag:>12}: failed ({exc})")
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
