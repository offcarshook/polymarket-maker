#!/usr/bin/env python3
"""Markout logger + liquidity-rewards estimator for polymaker --paper.

Reads the bot's journal (journal/paper.jsonl) offline: no network, no keys.
The patched bot simulates its own paper fills (queue position, trade-through,
book cross) and journals them, so this script does NOT invent fills; it:

  * logs every paper fill and the token mid at +10s, +1m, +5m (markout,
    signed so POSITIVE = good for us)        -> my_tools/markout.csv
  * logs fills the moment they happen        -> my_tools/fills_live.csv
  * logs only fills losing >= --loss-usd     -> my_tools/big_losses.csv
  * estimates liquidity rewards every minute -> my_tools/rewards_summary.json

Journal events used:
  paper_orders / paper_cancels / paper_cancel_asset / paper_fills  (our orders)
  book / price_change                                              (order books)

Rewards estimate (Polymarket's published scoring, simplified):
  per order within the market's max spread v of the mid and >= min size:
      S = ((v - distance) / v)^2 * size
  Q1 = buy-YES side (YES bids), Q2 = sell-YES side (NO bids / YES asks)
  Qmin = max(min(Q1,Q2), max(Q1,Q2)/3) if 0.10 <= mid <= 0.90 else min(Q1,Q2)
  our share = our Qmin / (our Qmin + visible book's Qmin), times the market's
  daily pool / 1440 per minute. Rough: real payouts use sampled snapshots of
  everyone's orders, and other makers' hidden behaviour isn't visible here.

  python3 my_tools/markout.py --follow --from-end
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

HORIZONS = (10, 60, 300)
FIELDS = ["fill_ts", "fill_time_utc", "token_id", "side", "fill_price", "fill_size",
          "why", "trade_price", "mid_at_fill"] + \
         [f"mid_{h}s" for h in HORIZONS] + [f"markout_{h}s" for h in HORIZONS] + \
         ["quote_age_s", "spread_at_fill", "pnl_300s_usd"]


@dataclass
class Book:
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    top: tuple[float, float] | None = None  # (best_bid, best_ask) from price_change
    snap: bool = False  # saw a full snapshot: depth is complete (needed for rewards)

    def mid(self) -> float | None:
        if self.top is not None:
            return round((self.top[0] + self.top[1]) / 2.0, 5)
        if not self.bids or not self.asks:
            return None
        return round((max(self.bids) + min(self.asks)) / 2.0, 5)


@dataclass
class Order:
    token_id: str
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


def _qmin(q1: float, q2: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / 3.0)
    return min(q1, q2)


class Sim:
    def __init__(self, out: Path, loss_usd: float, db: Path) -> None:
        self.books: dict[str, Book] = {}
        self.orders: dict[str, Order] = {}
        self.run_prefix = ""
        self.pending: list[PendingFill] = []
        self.n_fills = 0
        self.loss_usd = loss_usd
        self.live_path = out.with_name("fills_live.csv")
        self.loss_path = out.with_name("big_losses.csv")
        self.rewards_path = out.with_name("rewards_summary.json")
        self.db = db
        self.markets: dict[str, dict] = {}   # cid -> {yes, no, q, v, min_size, rate}
        self.tok_cid: dict[str, str] = {}
        self._markets_loaded = 0.0
        self.rw = self._load_rewards()
        self.catchup = False  # replaying old journal lines: rebuild books/orders only
        new = not out.exists() or out.stat().st_size == 0
        self._fh = out.open("a", newline="")
        self._w = csv.DictWriter(self._fh, fieldnames=FIELDS)
        if new:
            self._w.writeheader()

    # ── market metadata (reward params) ────────────────────────────────
    def load_markets(self) -> None:
        try:
            c = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
            rows = c.execute("select condition_id, question, meta_json from markets").fetchall()
        except sqlite3.Error:
            return
        for cid, q, mj in rows:
            m = json.loads(mj)
            toks = {t["outcome"].lower(): t["token_id"] for t in m.get("tokens", [])}
            if "yes" not in toks or "no" not in toks:
                continue
            self.markets[cid] = {
                "yes": toks["yes"], "no": toks["no"], "q": q or "",
                "v": float(m.get("rewards_max_spread") or 0) / 100.0,
                "min_size": float(m.get("rewards_min_size") or 0),
                "rate": float(m.get("rewards_daily_rate") or 0),
            }
            self.tok_cid[toks["yes"]] = cid
            self.tok_cid[toks["no"]] = cid
        self._markets_loaded = time.time()

    def _load_rewards(self) -> dict:
        try:
            return json.loads(self.rewards_path.read_text())
        except (OSError, ValueError):
            return {"since": None, "last_sample_ts": 0.0, "total_usd": 0.0, "per_market": {}}

    # ── event handling ─────────────────────────────────────────────────
    def handle(self, ev: dict) -> None:
        ts, kind, data = float(ev.get("ts", 0)), ev.get("kind"), ev.get("data")
        self.resolve(ts)
        if kind == "paper_orders" and isinstance(data, list):
            for o in data:
                oid = str(o["id"])
                prefix = oid.rsplit("-", 1)[0]
                if prefix != self.run_prefix:  # bot restarted: its old orders are gone
                    self.orders = {k: v for k, v in self.orders.items() if k.startswith(prefix + "-")}
                    self.run_prefix = prefix
                self.orders[oid] = Order(str(o["token_id"]), str(o["side"]).upper(),
                                         float(o["price"]), float(o["size"]), ts)
        elif kind == "paper_cancels" and isinstance(data, list):
            for oid in data:
                self.orders.pop(str(oid), None)
        elif kind == "paper_cancel_asset":
            for k in [k for k, o in self.orders.items() if o.token_id == str(data)]:
                del self.orders[k]
        elif kind == "paper_fills" and isinstance(data, dict):
            if not self.catchup:
                self._on_fill(ts, data)
            else:
                o = self.orders.get(str(data.get("order_id")))
                if o:
                    o.size -= float(data["size"])
        elif kind == "book" and isinstance(data, dict):
            b = Book(snap=True)  # full snapshot: depth is authoritative, drop stale top
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
        if not self.catchup and ts - float(self.rw.get("last_sample_ts") or 0) >= 60:
            self.sample_rewards(ts)

    def _on_fill(self, ts: float, d: dict) -> None:
        token, side, price = str(d["token_id"]), str(d["side"]).upper(), float(d["price"])
        size = float(d["size"])
        oid = str(d.get("order_id"))
        o = self.orders.get(oid)
        age = round(ts - o.ts, 1) if o else None
        if o:
            o.size -= size
            if o.size <= 1e-9:
                self.orders.pop(oid, None)
        book = self.books.get(token, Book())
        mid = book.mid()
        row: dict[str, object] = {
            "fill_ts": round(ts, 3),
            "fill_time_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)),
            "token_id": token, "side": side, "fill_price": price,
            "fill_size": round(size, 4), "why": d.get("why"),
            "trade_price": d.get("print_px"), "mid_at_fill": mid,
            "quote_age_s": age,
            "spread_at_fill": round(book.top[1] - book.top[0], 5) if book.top else None,
        }
        self.pending.append(PendingFill(row, side, price, token, ts))
        self.n_fills += 1  # (never called during catch-up)
        new = not self.live_path.exists()
        with self.live_path.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerow(row)
        print(f"[fill] {row['fill_time_utc']} {side} {size:.2f} @ {price} ({d.get('why')}) "
              f"token {token[:10]}… mid {mid}", flush=True)

    # ── rewards ────────────────────────────────────────────────────────
    def sample_rewards(self, ts: float) -> None:
        if time.time() - self._markets_loaded > 600:
            self.load_markets()
        last = float(self.rw.get("last_sample_ts") or 0)
        if ts <= last:
            return  # replayed journal: never credit the same minute twice
        minutes = min((ts - last) / 60.0, 5.0) if last else 1.0
        self.rw["last_sample_ts"] = ts
        self.rw["since"] = self.rw.get("since") or ts
        mine: dict[str, list[Order]] = {}
        for o in self.orders.values():
            cid = self.tok_cid.get(o.token_id)
            if cid:
                mine.setdefault(cid, []).append(o)
        earned_now, earning = 0.0, 0
        for pm in self.rw["per_market"].values():
            pm["open_usd"] = 0.0
        for cid, orders in mine.items():
            pm = self.rw["per_market"].setdefault(cid, {"q": self.markets[cid]["q"][:70], "usd": 0.0})
            pm["open_usd"] = round(sum(o.price * o.size for o in orders if o.side == "BUY"), 2)
            m = self.markets[cid]
            v, rate, min_size = m["v"], m["rate"], m["min_size"]
            book = self.books.get(m["yes"])
            mid = book.mid() if book else None
            if v <= 0 or rate <= 0 or mid is None or book is None or not book.snap:
                continue  # no full snapshot yet: competing depth unknown, credit nothing

            def score(dist: float, size: float, v: float = v, min_size: float = min_size) -> float:
                return ((v - dist) / v) ** 2 * size if 0 <= dist < v and size >= min_size else 0.0

            q1 = sum(score(mid - o.price, o.size) for o in orders if o.token_id == m["yes"] and o.side == "BUY")
            q2 = sum(score((1 - o.price) - mid, o.size) for o in orders if o.token_id == m["no"] and o.side == "BUY")
            ours = _qmin(q1, q2, mid)
            if ours <= 0:
                continue
            b1 = sum(score(mid - p, s) for p, s in book.bids.items())
            b2 = sum(score(p - mid, s) for p, s in book.asks.items())
            share = ours / (ours + _qmin(b1, b2, mid))
            usd = rate / 1440.0 * share * minutes
            earned_now += usd
            earning += 1
            pm["usd"] = round(pm["usd"] + usd, 6)
            pm["share_now"] = round(share, 4)
        self.rw["total_usd"] = round(float(self.rw.get("total_usd") or 0) + earned_now, 6)
        self.rw["per_day_now_usd"] = round(earned_now / minutes * 1440, 2) if minutes else 0.0
        self.rw["markets_earning"] = earning
        tmp = self.rewards_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.rw))
        tmp.replace(self.rewards_path)

    # ── markouts ───────────────────────────────────────────────────────
    def resolve(self, now: float) -> None:
        if self.catchup:
            return
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
              f"({row['why']}, mid {row['mid_at_fill']})", flush=True)

    def close(self) -> None:
        for pf in self.pending:  # flush incomplete rows (missing horizons stay blank)
            self._w.writerow(pf.row)
        self._fh.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--journal", default="journal/paper.jsonl")
    ap.add_argument("--out", default="my_tools/markout.csv")
    ap.add_argument("--db", default="state.db", help="catalog DB (reward parameters per market)")
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
    sim = Sim(out, args.loss_usd, Path(args.db))
    sim.load_markets()
    try:
        with jpath.open() as fh:
            # --from-end: replay old lines only to rebuild books + live orders (needed
            # for rewards), without re-logging old fills or re-crediting old minutes
            catchup_until = jpath.stat().st_size if (args.follow and args.from_end) else 0
            sim.catchup = catchup_until > 0
            while True:
                if sim.catchup and fh.tell() >= catchup_until:
                    sim.catchup = False
                    print(f"caught up: {len(sim.books)} books, {len(sim.orders)} live orders", flush=True)
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
        print(f"done: {sim.n_fills} paper fills -> {out}; est. rewards ${float(sim.rw.get('total_usd') or 0):.4f}")


if __name__ == "__main__":
    main()
