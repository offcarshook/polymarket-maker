#!/usr/bin/env python3
"""Paper-fill simulator + markout logger for polymaker --paper.

polymaker's paper mode never simulates fills, so this script does it OFFLINE
from the bot's own journal (journal/paper.jsonl): no network, no keys.

Inputs read from the journal:
  orders_out        -> our paper quotes (token_id, side, price, size)
  book/price_change -> order book per token (to compute the mid)
  last_trade_price  -> public trade prints

Fill rule (conservative "trade-through"):
  our BUY  @ p fills only if a print trades strictly BELOW p
  our SELL @ p fills only if a print trades strictly ABOVE p
  fill size = min(print size, our remaining size). Touching p is NOT a fill.

Quote lifetime (approximation; paper cancels aren't journaled):
  each price level we quote is live until it is re-quoted at the same price
  (refreshing it) or is older than --max-age seconds. Layers are filled best-first.

Markout per fill at +10s, +60s, +300s, signed so POSITIVE = good for us:
  BUY:  mid_t - fill_price      SELL: fill_price - mid_t

Usage:
  python3 my_tools/markout.py                      # replay journal once
  python3 my_tools/markout.py --follow             # tail live while the bot runs
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

HORIZONS = (10, 60, 300)
FIELDS = ["fill_ts", "fill_time_utc", "token_id", "side", "fill_price", "fill_size",
          "quote_price", "trade_price", "mid_at_fill"] + \
         [f"mid_{h}s" for h in HORIZONS] + [f"markout_{h}s" for h in HORIZONS] + \
         ["quote_age_s", "spread_at_fill", "pnl_300s_usd"]


@dataclass
class Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    top: tuple[float, float] | None = None  # (best_bid, best_ask) from price_change

    def mid(self) -> float | None:
        if self.top is not None:
            return round((self.top[0] + self.top[1]) / 2.0, 5)
        if not self.bids or not self.asks:
            return None
        return round((max(self.bids) + min(self.asks)) / 2.0, 5)


@dataclass
class Quote:
    side: str
    price: float
    size: float
    ts: float


@dataclass
class PendingFill:
    row: dict[str, object]
    side: str
    price: float
    token_id: str
    ts: float
    done: set[int] = field(default_factory=set)


class Sim:
    def __init__(self, out: Path, max_age: float, loss_usd: float = 1.0) -> None:
        self.books: dict[str, Book] = {}
        # (token, side) -> {price: Quote}; multiple layers per side
        self.quotes: dict[tuple[str, str], dict[float, Quote]] = {}
        self.pending: list[PendingFill] = []
        self.max_age = max_age
        self.n_fills = 0
        self.live_path: Path | None = out.with_name("fills_live.csv")
        # loss journal: only fills that lost >= loss_usd after 5 min, with clues why
        self.loss_usd = loss_usd
        self.loss_path = out.with_name("big_losses.csv")
        self.n_ghosts = 0
        new = not out.exists() or out.stat().st_size == 0
        self._fh = out.open("a", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=FIELDS)
        if new:
            self._w.writeheader()

    # ── event handling ─────────────────────────────────────────────────
    def handle(self, ev: dict) -> None:
        ts, kind, data = float(ev.get("ts", 0)), ev.get("kind"), ev.get("data")
        self.resolve(ts)
        if kind == "orders_out" and isinstance(data, list):
            for q in data:
                side = str(q.get("side", "")).upper().replace("SIDE.", "")
                px = float(q["price"])
                self.quotes.setdefault((str(q["token_id"]), side), {})[px] = Quote(
                    side, px, float(q["size"]), ts)
        elif kind == "book" and isinstance(data, dict):
            b = Book()  # full snapshot: depth is authoritative, drop stale top
            b.bids = {float(x["price"]): float(x["size"]) for x in data.get("bids", [])}
            b.asks = {float(x["price"]): float(x["size"]) for x in data.get("asks", [])}
            self.books[str(data.get("asset_id"))] = b
        elif kind == "price_change" and isinstance(data, dict):
            for ch in data.get("price_changes", []) or []:
                b = self.books.setdefault(str(ch.get("asset_id")), Book())
                lvl = b.bids if str(ch.get("side")).upper() == "BUY" else b.asks
                px, sz = float(ch["price"]), float(ch["size"])
                if sz <= 0:
                    lvl.pop(px, None)
                else:
                    lvl[px] = sz
                try:
                    b.top = (float(ch["best_bid"]), float(ch["best_ask"]))
                except (KeyError, TypeError, ValueError):
                    pass
                self._drop_crossed(str(ch.get("asset_id")), b)
        elif kind == "last_trade_price" and isinstance(data, dict):
            self.on_trade(ts, str(data.get("asset_id")), float(data["price"]),
                          float(data.get("size", 0)))

    def _drop_crossed(self, token: str, b: Book) -> None:
        """Ghost-quote fix: a resting post-only bid can't sit at/above the best ask
        (it would have matched). If the book moved through our level without a trade
        print, the real bot already cancelled/repriced it -> it can no longer fill."""
        if b.top is None:
            return
        bb, ba = b.top
        for side, dead in (("BUY", lambda p: p >= ba), ("SELL", lambda p: p <= bb)):
            levels = self.quotes.get((token, side))
            if not levels:
                continue
            for p in [p for p in levels if dead(p)]:
                del levels[p]
                self.n_ghosts += 1

    def on_trade(self, ts: float, token: str, px: float, size: float) -> None:
        for side in ("BUY", "SELL"):
            levels = self.quotes.get((token, side), {})
            for p in [p for p, q in levels.items() if ts - q.ts > self.max_age or q.size <= 0]:
                del levels[p]
            # best price first: highest bid / lowest ask
            for qp in sorted(levels, reverse=(side == "BUY")):
                if size <= 0:
                    break
                q = levels[qp]
                through = px < q.price if side == "BUY" else px > q.price
                if not through:
                    break
                fill_sz = min(size, q.size)
                size -= fill_sz
                q.size -= fill_sz
                if q.size <= 1e-9:
                    del levels[qp]
                self._record(ts, token, side, q.price, fill_sz, px, q.ts)

    def _record(self, ts: float, token: str, side: str, price: float, fill_sz: float, px: float,
                quote_ts: float) -> None:
        book = self.books.get(token, Book())
        mid = book.mid()
        spread = round(book.top[1] - book.top[0], 5) if book.top else None
        row: dict[str, object] = {
            "fill_ts": round(ts, 3),
            "fill_time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)),
            "token_id": token, "side": side, "fill_price": price,
            "fill_size": round(fill_sz, 4), "quote_price": price,
            "trade_price": px, "mid_at_fill": mid,
            "quote_age_s": round(ts - quote_ts, 1), "spread_at_fill": spread,
        }
        self.pending.append(PendingFill(row, side, price, token, ts))
        self.n_fills += 1
        if self.live_path is not None:  # instant view for the dashboard (markouts fill in later)
            new = not self.live_path.exists()
            with self.live_path.open("a", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        print(f"[fill] {row['fill_time_utc']} {side} {fill_sz:.2f} @ {price} "
              f"(print {px}) token {token[:10]}… mid {mid}", flush=True)

    def resolve(self, now: float) -> None:
        still = []
        for pf in self.pending:
            for h in HORIZONS:
                if h not in pf.done and now >= pf.ts + h:
                    mid = self.books.get(pf.token_id, Book()).mid()
                    pf.row[f"mid_{h}s"] = mid
                    if mid is not None:
                        mk = (mid - pf.price) if pf.side == "BUY" else (pf.price - mid)
                        pf.row[f"markout_{h}s"] = round(mk, 5)
                    pf.done.add(h)
            if len(pf.done) == len(HORIZONS):
                mk = pf.row.get("markout_300s")
                if isinstance(mk, float):
                    pf.row["pnl_300s_usd"] = round(mk * float(pf.row["fill_size"]), 4)
                self._w.writerow(pf.row)
                self._fh.flush()
                pnl = pf.row.get("pnl_300s_usd")
                if isinstance(pnl, float) and pnl <= -self.loss_usd:
                    self._log_loss(pf.row)
            else:
                still.append(pf)
        self.pending = still

    def _log_loss(self, row: dict[str, object]) -> None:
        new = not self.loss_path.exists()
        with self.loss_path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        print(f"[BIG LOSS] ${row['pnl_300s_usd']} on {row['fill_size']} @ {row['fill_price']} "
              f"(mid {row['mid_at_fill']}, quote age {row['quote_age_s']}s)", flush=True)

    def close(self) -> None:
        for pf in self.pending:  # flush incomplete rows (missing horizons stay blank)
            self._w.writerow(pf.row)
        self._fh.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", default="journal/paper.jsonl")
    ap.add_argument("--out", default="my_tools/markout.csv")
    ap.add_argument("--max-age", type=float, default=30.0,
                    help="treat a quote as dead after N s without being re-placed")
    ap.add_argument("--follow", action="store_true", help="keep tailing the journal")
    ap.add_argument("--from-end", action="store_true",
                    help="with --follow: start at the end of the journal (no replay -> no duplicate fills after a restart)")
    ap.add_argument("--loss-usd", type=float, default=1.0,
                    help="log fills losing at least this much after 5 min to big_losses.csv")
    args = ap.parse_args()

    jpath, out = Path(args.journal), Path(args.out)
    if args.follow:
        while not jpath.exists():
            print(f"waiting for {jpath} …")
            time.sleep(2)
    sim = Sim(out, args.max_age, args.loss_usd)
    try:
        with jpath.open() as fh:
            if args.follow and args.from_end:
                fh.seek(0, 2)
            while True:
                line = fh.readline()
                if not line:
                    if not args.follow:
                        break
                    if jpath.stat().st_size < fh.tell():  # journal_guard truncated it
                        fh.seek(0)
                    sim.resolve(time.time())  # quiet market: horizons still elapse
                    time.sleep(0.5)
                    continue
                try:
                    sim.handle(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    continue
    except KeyboardInterrupt:
        pass
    finally:
        sim.close()
        print(f"done: {sim.n_fills} simulated fills -> {out} ({sim.n_ghosts} ghost quotes dropped)")


if __name__ == "__main__":
    main()
