#!/usr/bin/env python3
"""Write config/markets.toml with the N most-traded markets from the scan catalog.

Reads state.db (filled by `polymaker scan --all`), ranks by 24h volume, and skips
markets that are nearly resolved, about to end, or have no two-sided book.
PAPER TEST ONLY: every market gets the given profile (default paper-aggressive).

  python3 my_tools/pick_markets.py --n 100
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone


# single games / short in-play markets: whoever watches the game or the spot feed
# sees the move before we can requote -> they were our biggest paper losses
GAME_RE = re.compile(r"\bvs\.?\b|\bv\.\s|O/U|Over/Under|Spread|Handicap|Up or Down|"
                     r"\bMap \d|\bGame \d|\bSet \d|\bRound \d|Total (Kills|Goals|Points)|"
                     r"\bwin on \d{4}-\d{2}-\d{2}|\bon \d{4}-\d{2}-\d{2}\?|\bdraw\b|\bend in a draw", re.I)
# dated single-event slugs (one match / one day), e.g. "...-win-on-2026-10-25"
DATED_SLUG_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--profile", default="paper-aggressive")
    ap.add_argument("--min-hours-left", type=float, default=48.0)
    ap.add_argument("--rank", choices=["volume", "opportunity"], default="opportunity",
                    help="opportunity = est. maker $/day: 24h volume x half-spread + daily rewards pool")
    ap.add_argument("--max-spread-ticks", type=float, default=0,
                    help="skip books wider than this many ticks (0 = no limit); wide = stale/toxic")
    ap.add_argument("--exclude", action="append", default=[],
                    help="skip slugs containing this text (repeatable), e.g. events resolving today")
    ap.add_argument("--no-games", action="store_true",
                    help="skip single games / in-play style markets (X vs Y, O/U, spreads, up-or-down)")
    ap.add_argument("--db", default="state.db")
    ap.add_argument("--out", default="config/markets.toml")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    rows = []
    for slug, mj in c.execute("select slug, meta_json from markets"):
        m = json.loads(mj)
        try:
            bb, ba = float(m.get("best_bid") or 0), float(m.get("best_ask") or 0)
        except (TypeError, ValueError):
            continue
        if not (0 < bb < ba < 1):
            continue
        mid = (bb + ba) / 2
        if not 0.04 <= mid <= 0.96:  # near-resolved: nothing to make
            continue
        end = m.get("end_date_iso")
        if end:
            try:
                left = (datetime.fromisoformat(end.replace("Z", "+00:00")) - now).total_seconds() / 3600
            except ValueError:
                left = None
            if left is not None and 0 < left < a.min_hours_left:
                continue
        if len(m.get("tokens", [])) != 2 or any(x in slug for x in a.exclude):
            continue
        if a.no_games and (GAME_RE.search(str(m.get("question", ""))) or DATED_SLUG_RE.search(slug)):
            continue
        tick_sz = float(m.get("tick_size") or 0.01)
        if a.max_spread_ticks and (ba - bb) > a.max_spread_ticks * tick_sz + 1e-9:
            continue
        vol = float(m.get("volume_24hr") or 0)
        if a.rank == "volume":
            rows.append((vol, slug))
        else:
            tick = float(m.get("tick_size") or 0.01)
            half_spread = min(max(ba - bb, tick), 0.06) / 2  # wide spreads cap out: those books are toxic/stale
            rewards = float(m.get("rewards_daily_rate") or 0)
            rows.append((vol * half_spread + rewards, slug))

    rows.sort(reverse=True)
    picked = rows[: a.n]
    with open(a.out, "w") as fh:
        fh.write(f"# Auto-picked by my_tools/pick_markets.py: top {len(picked)} by 24h volume (PAPER ONLY)\n")
        for vol, slug in picked:
            fh.write(f'\n[[markets]]\nslug    = "{slug}"\nprofile = "{a.profile}"\nenabled = true\n')
    total = sum(v for v, _ in picked)
    label = "combined_24h_volume" if a.rank == "volume" else "est_maker_opportunity_per_day"
    print(f"eligible={len(rows)} picked={len(picked)} {label}=${total:,.0f}")
    for vol, slug in picked[:10]:
        print(f"  ${vol:>12,.0f}  {slug}")


if __name__ == "__main__":
    main()
