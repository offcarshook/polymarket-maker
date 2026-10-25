#!/usr/bin/env python3
"""A/B test market split (PAPER ONLY) -> config/markets.toml

  arm A  paper-aggressive : the top --agg-n markets by opportunity (volume x half-spread + rewards)
  arm B  paper-rewards    : as many reward markets as --farm-capital can back at the reward
                            minimum size, best reward-per-dollar-locked first

Both arms skip near-resolved books, single games, wide books and anything ending
within --min-hours-left. Reads state.db (run `uv run python my_tools/scan_tags.py` first).

  python3 -I my_tools/ab_pick.py
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone

GAME_RE = re.compile(r"\bvs\.?\b|\bv\.\s|O/U|Over/Under|Spread|Handicap|Up or Down|"
                     r"\bMap \d|\bGame \d|\bSet \d|\bRound \d|Total (Kills|Goals|Points)", re.I)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agg-n", type=int, default=15)
    ap.add_argument("--agg-rank", choices=["opportunity", "volume"], default="opportunity",
                    help="volume = busiest markets (most trade flow) with books <= 2 ticks wide")
    ap.add_argument("--farm-capital", type=float, default=5000.0)
    ap.add_argument("--size-mult", type=float, default=1.1, help="must match paper-rewards reward_size_mult")
    ap.add_argument("--min-rate", type=float, default=5.0, help="skip reward pools under $/day")
    ap.add_argument("--min-hours-left", type=float, default=168.0)
    ap.add_argument("--max-spread-ticks", type=float, default=3.0)
    ap.add_argument("--exclude", action="append", default=["brazilian-presidential-election"])
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--out", default="config/markets.toml")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    rows = []
    for slug, mj in sqlite3.connect(f"file:{a.db}?mode=ro", uri=True).execute("select slug, meta_json from markets"):
        m = json.loads(mj)
        try:
            bb, ba = float(m.get("best_bid") or 0), float(m.get("best_ask") or 0)
        except (TypeError, ValueError):
            continue
        if not (0 < bb < ba < 1) or len(m.get("tokens", [])) != 2:
            continue
        mid = (bb + ba) / 2
        tick = float(m.get("tick_size") or 0.01)
        if not 0.10 <= mid <= 0.90 or (ba - bb) > a.max_spread_ticks * tick + 1e-9:
            continue
        if GAME_RE.search(str(m.get("question", ""))) or any(x in slug for x in a.exclude):
            continue
        end = m.get("end_date_iso")
        if end:
            try:
                left = (datetime.fromisoformat(end.replace("Z", "+00:00")) - now).total_seconds() / 3600
                if 0 < left < a.min_hours_left:
                    continue
            except ValueError:
                pass
        rate = float(m.get("rewards_daily_rate") or 0)
        min_size = float(m.get("rewards_min_size") or 0)
        opp = float(m.get("volume_24hr") or 0) * min(max(ba - bb, tick), 0.06) / 2 + rate
        # cash to rest one min-size bid on each side: YES ~mid + NO ~(1-mid) per share = ~$1/share
        cost = max(min_size, float(m.get("min_order_size") or 0)) * a.size_mult
        rows.append({"slug": slug, "opp": opp, "rate": rate, "cost": cost, "q": m.get("question", ""),
                     "vol": float(m.get("volume_24hr") or 0), "tight": (ba - bb) <= 2 * tick + 1e-9})

    if a.agg_rank == "volume":
        agg = sorted((r for r in rows if r["tight"]), key=lambda r: -r["vol"])[: a.agg_n]
    else:
        agg = sorted(rows, key=lambda r: -r["opp"])[: a.agg_n]
    taken = {r["slug"] for r in agg}
    farm_pool = [r for r in rows if r["slug"] not in taken and r["rate"] >= a.min_rate and r["cost"] > 0]
    farm_pool.sort(key=lambda r: -(r["rate"] / r["cost"]))  # reward $/day per $ locked
    farm, used = [], 0.0
    for r in farm_pool:
        if used + r["cost"] <= a.farm_capital * 0.9:  # keep 10% for fills held as inventory
            farm.append(r)
            used += r["cost"]

    with open(a.out, "w") as fh:
        fh.write(f"# A/B test (PAPER ONLY) by my_tools/ab_pick.py: {len(agg)} paper-aggressive, "
                 f"{len(farm)} paper-rewards\n")
        for arm, prof in ((agg, "paper-aggressive"), (farm, "paper-rewards")):
            for r in arm:
                fh.write(f'\n[[markets]]\nslug    = "{r["slug"]}"\nprofile = "{prof}"\nenabled = true\n')
    print(f"eligible={len(rows)}  A paper-aggressive={len(agg)}  B paper-rewards={len(farm)} "
          f"(cash to back: ${used:,.0f}, pools ${sum(r['rate'] for r in farm):,.0f}/day)")


if __name__ == "__main__":
    main()
