#!/usr/bin/env python3
"""Local auto-refreshing HTML dashboard for the polymaker paper test.

Reads (read-only, no network): journal/paper.jsonl, logs/paper.jsonl,
my_tools/markout.csv, state.db (market names). Writes my_tools/dashboard.html
every --every seconds; the page reloads itself.

  python3 my_tools/dashboard.py            # loop forever
  python3 my_tools/dashboard.py --once     # write one snapshot
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import sqlite3
import subprocess
import time
from collections import deque
from pathlib import Path

HORIZONS = (10, 60, 300)
LOSS_PATH = Path("my_tools/big_losses.csv")


class State:
    def __init__(self) -> None:
        self.books: dict[str, tuple[dict[float, float], dict[float, float]]] = {}
        self.top: dict[str, tuple[float, float]] = {}  # token -> (best_bid, best_ask)
        self.quotes: dict[str, dict[float, tuple[float, float]]] = {}  # token -> {price: (size, ts)} BUY layers
        self.last_trade: dict[str, tuple[float, float, float]] = {}  # token -> (px,size,ts)
        self.mid_hist: dict[str, deque[tuple[float, float]]] = {}
        self.trades_seen = 0
        self.quotes_sent = 0
        self.first_ts: float | None = None
        self.last_ts = 0.0
        self.j_off = 0
        self.l_off = 0
        self.events: deque[dict] = deque(maxlen=14)
        self.regime: dict[str, str] = {}
        self.requotes = 0

    def mid(self, tok: str) -> float | None:
        if tok in self.top:
            return (self.top[tok][0] + self.top[tok][1]) / 2
        b = self.books.get(tok)
        if not b or not b[0] or not b[1]:
            return None
        return (max(b[0]) + min(b[1])) / 2

    def feed_journal(self, path: Path) -> None:
        if not path.exists():
            return
        if path.stat().st_size < self.j_off:  # journal_guard truncated it
            self.j_off = 0
        with path.open() as fh:
            fh.seek(self.j_off)
            for line in fh:
                if not line.endswith("\n"):
                    break
                self.j_off += len(line.encode())
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                ts, kind, d = float(ev["ts"]), ev["kind"], ev["data"]
                self.first_ts = self.first_ts or ts
                self.last_ts = max(self.last_ts, ts)
                touched: set[str] = set()
                if kind == "book":
                    tok = str(d["asset_id"])
                    self.top.pop(tok, None)
                    self.books[tok] = ({float(x["price"]): float(x["size"]) for x in d.get("bids", [])},
                                       {float(x["price"]): float(x["size"]) for x in d.get("asks", [])})
                    touched.add(tok)
                elif kind == "price_change":
                    for ch in d.get("price_changes", []) or []:
                        tok = str(ch["asset_id"])
                        bids, asks = self.books.setdefault(tok, ({}, {}))
                        lvl = bids if str(ch["side"]).upper() == "BUY" else asks
                        px, sz = float(ch["price"]), float(ch["size"])
                        lvl.pop(px, None) if sz <= 0 else lvl.__setitem__(px, sz)
                        try:
                            self.top[tok] = (float(ch["best_bid"]), float(ch["best_ask"]))
                        except (KeyError, TypeError, ValueError):
                            pass
                        touched.add(tok)
                elif kind == "last_trade_price":
                    self.last_trade[str(d["asset_id"])] = (float(d["price"]), float(d.get("size", 0)), ts)
                    self.trades_seen += 1
                elif kind == "orders_out":
                    for q in d:
                        self.quotes.setdefault(str(q["token_id"]), {})[float(q["price"])] = (float(q["size"]), ts)
                        self.quotes_sent += 1
                for tok in touched:
                    m = self.mid(tok)
                    if m is None:
                        continue
                    h = self.mid_hist.setdefault(tok, deque(maxlen=240))
                    if not h or ts - h[-1][0] >= 15 or abs(h[-1][1] - m) > 1e-9:
                        h.append((ts, m))

    def feed_logs(self, path: Path) -> None:
        if not path.exists():
            return
        with path.open() as fh:
            fh.seek(self.l_off)
            for line in fh:
                if not line.endswith("\n"):
                    break
                self.l_off += len(line.encode())
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("event") == "requote":
                    self.requotes += 1
                    self.regime[str(ev.get("cid"))] = str(ev.get("regime"))
                    continue
                if str(ev.get("event", "")).startswith("HTTP Request"):
                    continue
                self.events.append(ev)


def market_names(db: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        for cid, q, mj in c.execute("select condition_id, question, meta_json from markets"):
            out[cid] = {"q": q, "tokens": json.loads(mj).get("tokens", []), "end": json.loads(mj).get("end_date_iso")}
    except sqlite3.Error:
        pass
    return out


def tracked_cids(markets_toml: Path, names: dict[str, dict]) -> list[str]:
    import tomllib
    try:
        slugs = [m["slug"] for m in tomllib.loads(markets_toml.read_text()).get("markets", []) if m.get("enabled", True)]
    except (OSError, ValueError):
        return []
    by_slug: dict[str, str] = {}
    try:
        c = sqlite3.connect(f"file:{markets_toml.parent.parent / 'state.db'}?mode=ro", uri=True)
        by_slug = {s: cid for cid, s in c.execute("select condition_id, slug from markets")}
    except sqlite3.Error:
        pass
    return [by_slug[s] for s in slugs if s in by_slug and by_slug[s] in names]


def read_fills(path: Path) -> list[dict]:
    """Finished rows (with markouts) from markout.csv, plus just-happened fills from
    fills_live.csv whose markout horizons haven't elapsed yet."""
    def load(p: Path) -> list[dict]:
        if not p.exists():
            return []
        with p.open() as fh:
            return list(csv.DictReader(fh))
    done = load(path)
    key = lambda r: (r.get("fill_ts"), r.get("token_id"), r.get("fill_price"))  # noqa: E731
    seen = {key(r) for r in done}
    rows = done + [r for r in load(path.with_name("fills_live.csv")) if key(r) not in seen]
    return sorted(rows, key=lambda r: float(r.get("fill_ts") or 0))


def fnum(x: object) -> float | None:
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def bot_running() -> bool:
    try:
        return subprocess.run(["pgrep", "-f", "polymaker run --paper"], capture_output=True).returncode == 0
    except OSError:
        return False


# ── rendering ───────────────────────────────────────────────────────────
def esc(s: object) -> str:
    return html.escape(str(s))


def usd(x: float) -> str:
    return f"{'−' if x < 0 else '+'}${abs(x):.2f}"


def cents(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "–"
    return f"{x * 100:+.2f}¢" if signed else f"{x * 100:.1f}¢"


def sparkline(points: list[tuple[float, float]], bid: float | None, ask: float | None) -> str:
    w, h, pad = 320, 70, 4
    vals = [p[1] for p in points] + [v for v in (bid, ask) if v is not None]
    if len(points) < 2 or not vals:
        return '<div class="spark-empty">collecting price history…</div>'
    lo, hi = min(vals), max(vals)
    if hi - lo < 0.01:
        lo, hi = lo - 0.005, hi + 0.005
    t0, t1 = points[0][0], points[-1][0] or points[0][0] + 1
    span = max(t1 - t0, 1)
    y = lambda v: h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)  # noqa: E731
    x = lambda t: pad + (t - t0) / span * (w - 2 * pad)  # noqa: E731
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(points))
    bands = ""
    for v, cls in ((bid, "bidl"), (ask, "askl")):
        if v is not None:
            bands += f'<line class="{cls}" x1="{pad}" x2="{w - pad}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none">{bands}'
            f'<path class="midl" d="{path}"/></svg>')


def markout_chart(fills: list[dict]) -> str:
    rows = []
    for hz in HORIZONS:
        vs = [v for v in (fnum(f.get(f"markout_{hz}s")) for f in fills) if v is not None]
        avg = sum(vs) / len(vs) if vs else None
        rows.append((hz, avg, len(vs)))
    scale = max([abs(a) for _, a, _ in rows if a is not None] + [0.005])
    out = []
    for hz, avg, n in rows:
        label = {10: "+10s", 60: "+1m", 300: "+5m"}[hz]
        if avg is None:
            bar = '<div class="mo-bar none"></div>'
            val = "no data"
        else:
            pct = abs(avg) / scale * 50
            side = "pos" if avg >= 0 else "neg"
            style = f"left:50%;width:{pct:.1f}%" if avg >= 0 else f"left:{50 - pct:.1f}%;width:{pct:.1f}%"
            bar = f'<div class="mo-bar {side}" style="{style}"></div>'
            val = f"{cents(avg, True)} <span class='muted'>· n={n}</span>"
        out.append(f'<div class="mo-row"><div class="mo-lbl">{label}</div>'
                   f'<div class="mo-track"><div class="mo-zero"></div>{bar}</div>'
                   f'<div class="mo-val">{val}</div></div>')
    return "".join(out)


def big_losses_html(path: Path, names: dict[str, dict]) -> str:
    if not path.exists():
        return '<tr><td colspan="7" class="empty">No big losses yet 🙏</td></tr>'
    with path.open() as fh:
        rows = sorted(csv.DictReader(fh), key=lambda r: fnum(r.get("pnl_300s_usd")) or 0)[:10]
    tok_q = {t["token_id"]: (m["q"], t["outcome"]) for m in names.values() for t in m.get("tokens", [])}
    out = []
    for r in rows:
        q, outc = tok_q.get(r.get("token_id", ""), ("?", ""))
        p, mid = fnum(r.get("fill_price")), fnum(r.get("mid_at_fill"))
        clue = []
        if p is not None and mid is not None and p > mid:
            clue.append("paid above mid")
        if (fnum(r.get("quote_age_s")) or 0) > 30:
            clue.append("stale quote")
        if (fnum(r.get("spread_at_fill")) or 0) >= 0.03:
            clue.append("wide/thin book")
        if (fnum(r.get("markout_10s")) or 0) < 0 and (fnum(r.get("markout_300s")) or 0) < (fnum(r.get("markout_10s")) or 0):
            clue.append("kept falling = informed flow")
        out.append(f"<tr><td class='neg'>${fnum(r.get('pnl_300s_usd')) or 0:.2f}</td><td class='trunc'>{esc(q)}</td>"
                   f"<td>BUY {esc(outc)} {cents(p)}</td><td>{esc(r.get('fill_size'))}</td><td>{cents(mid)}</td>"
                   f"<td>{esc(r.get('quote_age_s'))}s</td><td>{esc(', '.join(clue) or '–')}</td></tr>")
    return "".join(out)


def all_time_rows(root: Path) -> list[dict]:
    """Every finished fill across all runs (archived + current), deduped, oldest first."""
    files = sorted((root / "my_tools/archive").glob("*markout.csv")) + [root / "my_tools/markout.csv"]
    seen: set[tuple] = set()
    rows: list[dict] = []
    for f in files:
        if not f.exists():
            continue
        with f.open() as fh:
            for r in csv.DictReader(fh):
                k = (r.get("fill_ts"), r.get("token_id"), r.get("fill_price"))
                if k in seen:
                    continue
                seen.add(k)
                pnl = fnum(r.get("pnl_300s_usd"))
                if pnl is None:
                    mk, sz = fnum(r.get("markout_300s")), fnum(r.get("fill_size"))
                    pnl = mk * sz if mk is not None and sz is not None else None
                r["_pnl"] = pnl
                rows.append(r)
    return sorted(rows, key=lambda r: float(r.get("fill_ts") or 0))


def load_rewards(root: Path) -> dict:
    try:
        return json.loads((root / "my_tools/rewards_summary.json").read_text())
    except (OSError, ValueError):
        return {}


def rewards_table_html(rw: dict) -> str:
    pm = sorted((rw.get("per_market") or {}).values(), key=lambda m: -float(m.get("usd") or 0))[:10]
    if not pm:
        return ""
    body = "".join(f"<tr><td class='trunc'>{esc(m.get('q'))}</td><td class='pos'>${float(m.get('usd') or 0):.4f}</td>"
                   f"<td>{float(m.get('share_now') or 0) * 100:.1f}%</td></tr>" for m in pm)
    return ('<div class="panel" style="margin-bottom:16px"><h2>🎁 Top reward markets (estimate)</h2>'
            '<div class="tbl"><table><tr><th>Market</th><th>Earned</th><th>Our share now</th></tr>'
            f'{body}</table></div><div class="legend">Estimated from Polymarket\'s reward scoring vs the visible book. '
            'Real payouts depend on everyone\'s orders over the day.</div></div>')


def ab_html(root: Path, rows: list[dict], rw: dict) -> str:
    """A/B scoreboard: paper-aggressive vs paper-rewards since my_tools/ab_start.txt."""
    import tomllib
    try:
        start = float((root / "my_tools/ab_start.txt").read_text().strip())
        mk = tomllib.loads((root / "config/markets.toml").read_text()).get("markets", [])
        prof_cfg = tomllib.loads((root / "config/strategy.toml").read_text()).get("profiles", {})
        c = sqlite3.connect(f"file:{root / 'state.db'}?mode=ro", uri=True)
        slug_meta = {s_: (cid, json.loads(mj)) for cid, s_, mj in c.execute("select condition_id, slug, meta_json from markets")}
    except (OSError, ValueError, sqlite3.Error):
        return ""
    arms: dict[str, dict] = {}
    tok_arm: dict[str, str] = {}
    for e in mk:
        prof = e.get("profile", "?")
        a = arms.setdefault(prof, {"cids": set(), "budget": float(prof_cfg.get(prof, {}).get("paper_capital_usdc") or 0)})
        if e.get("slug") in slug_meta:
            cid, meta = slug_meta[e["slug"]]
            a["cids"].add(cid)
            for t in meta.get("tokens", []):
                tok_arm[t["token_id"]] = prof
    if len(arms) < 2:
        return ""
    days = max((time.time() - start) / 86400, 1e-6)
    pm = rw.get("per_market") or {}
    head = "<tr><th></th>" + "".join(f"<th>{esc(n)}</th>" for n in arms) + "</tr>"
    stats: dict[str, dict] = {}
    for name, a in arms.items():
        fills = [r for r in rows if tok_arm.get(r.get("token_id", "")) == name and float(r.get("fill_ts") or 0) >= start]
        trade = sum(r["_pnl"] for r in fills if r["_pnl"] is not None)
        rew = sum(float(pm.get(cid, {}).get("usd") or 0) for cid in a["cids"])
        in_use = sum(float(pm.get(cid, {}).get("open_usd") or 0) for cid in a["cids"])
        total = trade + rew
        roc = (total / a["budget"] / days * 100) if a["budget"] else 0.0
        stats[name] = {"Markets": f"{len(a['cids'])}", "Fake cash": f"${a['budget']:,.0f}",
                       "Cash in orders now": f"${in_use:,.0f}", "Fills": f"{len(fills)}",
                       "Trading PnL @ +5m": usd(trade), "🎁 Est. rewards": usd(rew),
                       "💰 Total": usd(total), "Return on cash / day": f"{roc:+.2f}%"}
    body = "".join("<tr><td class='lbl'>" + esc(k) + "</td>" + "".join(f"<td>{esc(stats[n][k])}</td>" for n in arms) + "</tr>"
                   for k in next(iter(stats.values())))
    since = time.strftime("%b %d %H:%M", time.gmtime(start))
    return (f'<div class="panel" style="margin-bottom:16px"><h2>🥊 A/B test since {esc(since)} UTC '
            f'({days * 24:.1f} h)</h2><div class="tbl"><table>{head}{body}</table></div>'
            '<div class="legend">Fair comparison = return on cash per day. Fills count once their 5-min markout '
            'is in. Rewards are estimates.</div></div>')


def all_time_html(rows: list[dict], rw: dict | None = None) -> str:
    rw = rw or {}
    rewards = float(rw.get("total_usd") or 0)
    done = [r for r in rows if r["_pnl"] is not None]
    if not done:
        return ('<div class="panel" style="margin-bottom:16px"><h2>🏆 All-time</h2>'
                f'<div class="muted">No finished fills yet · 🎁 est. rewards so far {usd(rewards)}</div></div>')
    total = sum(r["_pnl"] for r in done)
    stats = []
    for h, lbl in ((10, "+10s"), (60, "+1m"), (300, "+5m")):
        v = [x for x in (fnum(r.get(f"markout_{h}s")) for r in done) if x is not None]
        stats.append((lbl, sum(v) / len(v) if v else None))
    adverse = sum(1 for r in done if r["_pnl"] < 0) / len(done)
    worst = min(done, key=lambda r: r["_pnl"])
    best = max(done, key=lambda r: r["_pnl"])
    first = time.strftime("%b %d %H:%M", time.gmtime(float(done[0]["fill_ts"])))
    # cumulative PnL line
    w, h, pad = 640, 120, 6
    cum, pts = 0.0, []
    for r in done:
        cum += r["_pnl"]
        pts.append(cum)
    lo, hi = min(pts + [0.0]), max(pts + [0.0])
    if hi - lo < 1:
        lo, hi = lo - 0.5, hi + 0.5
    y = lambda v: h - pad - (v - lo) / (hi - lo) * (h - 2 * pad)  # noqa: E731
    x = lambda i: pad + i / max(len(pts) - 1, 1) * (w - 2 * pad)  # noqa: E731
    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))
    color = "var(--pos)" if total >= 0 else "var(--neg)"
    chart = (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" style="width:100%;height:120px;display:block">'
             f'<line x1="{pad}" x2="{w - pad}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="var(--muted)" stroke-dasharray="3 3" stroke-width="1"/>'
             f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"/></svg>')
    tiles = [("Fills (all runs)", f"{len(done)}"), ("Trading PnL @ +5m", usd(total)),
             ("🎁 Est. rewards", usd(rewards)), ("💰 Total (PnL + rewards)", usd(total + rewards)),
             ("Rewards pace now", f"${float(rw.get('per_day_now_usd') or 0):,.2f}/day · {int(rw.get('markets_earning') or 0)} mkts"),
             ("Got picked off", f"{adverse * 100:.0f}%"),
             ("Best fill", usd(best["_pnl"])), ("Worst fill", usd(worst["_pnl"]))]
    tiles += [(f"Avg markout {lbl}", cents(v, True)) for lbl, v in stats]
    tile_html = "".join(f'<div class="kpi"><div class="kpi-l">{esc(a)}</div><div class="kpi-v" style="font-size:18px">{esc(b)}</div></div>' for a, b in tiles)
    return (f'<div class="panel" style="margin-bottom:16px"><h2>🏆 All-time · every run since {esc(first)} UTC</h2>'
            f'<div class="kpis" style="margin-bottom:10px">{tile_html}</div>'
            f'<div class="lbl">Cumulative PnL after 5 min, fill by fill</div>{chart}</div>')


def render(st: State, names: dict[str, dict], cids: list[str], fills: list[dict]) -> str:
    now = time.time()
    quoting = sum(1 for c in cids if any(t["token_id"] in st.quotes for t in names.get(c, {}).get("tokens", [])))
    running = bot_running()
    age = now - st.last_ts if st.last_ts else None
    up = (st.last_ts - st.first_ts) if st.first_ts else 0
    status_cls = "ok" if running and age is not None and age < 90 else ("warn" if running else "bad")
    status_txt = ("LIVE FEED" if status_cls == "ok" else "QUIET / STALE" if running else "BOT STOPPED")

    # paper PnL: fills held to now, valued at current mid
    pnl = 0.0
    inv: dict[str, float] = {}
    for f in fills:
        p, s, tok = fnum(f.get("fill_price")), fnum(f.get("fill_size")), f.get("token_id", "")
        m = st.mid(tok)
        if p is None or s is None:
            continue
        inv[tok] = inv.get(tok, 0.0) + s
        if m is not None:
            pnl += (m - p) * s
    m300 = [v for v in (fnum(f.get("markout_300s")) for f in fills) if v is not None]
    adverse = (sum(1 for v in m300 if v < 0) / len(m300)) if m300 else None

    kpis = [
        ("Simulated fills", f"{len(fills)}", "trade-through only"),
        ("Paper PnL (mark-to-mid)", usd(pnl), "if held to now"),
        ("Adverse @ +5m", f"{adverse * 100:.0f}%" if adverse is not None else "–", "fills that moved against us"),
        ("Trades watched", f"{st.trades_seen}", f"{st.quotes_sent} paper quotes sent"),
        ("Markets", f"{len(cids)}", f"{quoting} quoting now · top 50 shown"),
    ]
    kpi_html = "".join(f'<div class="kpi"><div class="kpi-l">{esc(a)}</div><div class="kpi-v">{esc(b)}</div>'
                       f'<div class="kpi-s">{esc(c)}</div></div>' for a, b, c in kpis)

    # with hundreds of markets, show only the most active ones as cards
    def activity(cid: str) -> tuple[int, float]:
        toks = [t["token_id"] for t in names.get(cid, {}).get("tokens", [])]
        nf = sum(1 for f in fills if f.get("token_id") in toks)
        lt = max([st.last_trade[t][2] for t in toks if t in st.last_trade] or [0.0])
        return (nf, lt)
    shown = sorted(cids, key=activity, reverse=True)[:50]
    cards = []
    for cid in shown:
        meta = names.get(cid, {})
        toks = {t["outcome"].lower(): t["token_id"] for t in meta.get("tokens", [])}
        yes, no = toks.get("yes", ""), toks.get("no", "")
        mid = st.mid(yes)
        fresh = lambda tok: [p for p, (_, t) in st.quotes.get(tok, {}).items() if st.last_ts - t <= 30]  # noqa: E731
        qy, qn = fresh(yes), fresh(no)
        our_bid = max(qy) if qy else None
        our_ask = (1 - max(qn)) if qn else None  # buying NO @ p == selling YES @ 1-p
        b = st.books.get(yes, ({}, {}))
        if yes in st.top:
            bb, ba = st.top[yes]
        else:
            bb = max(b[0]) if b[0] else None
            ba = min(b[1]) if b[1] else None
        lt = st.last_trade.get(yes) or st.last_trade.get(no)
        lt_txt = "none yet"
        if lt:
            px = lt[0] if st.last_trade.get(yes) is lt else 1 - lt[0]
            lt_txt = f"{cents(px)} YES · {int(now - lt[2])}s ago"
        reg = st.regime.get(cid[:8], "–")
        nf = sum(1 for f in fills if f.get("token_id") in (yes, no))
        cards.append(f"""
<div class="card">
  <div class="card-h"><div class="q">{esc(meta.get('q', cid))}</div><span class="pill r-{esc(reg.lower())}">{esc(reg)}</span></div>
  <div class="row3">
    <div><div class="lbl">Our bid</div><div class="v bid">{cents(our_bid)}</div></div>
    <div><div class="lbl">Mid (YES)</div><div class="v">{cents(mid)}</div></div>
    <div><div class="lbl">Our ask</div><div class="v ask">{cents(our_ask)}</div></div>
  </div>
  {sparkline(list(st.mid_hist.get(yes, [])), our_bid, our_ask)}
  <div class="meta">book {cents(bb)} / {cents(ba)} · our width {cents((our_ask - our_bid) if our_bid is not None and our_ask is not None else None)} · last trade {esc(lt_txt)} · fills {nf} · ends {esc(str(meta.get('end') or '–')[:10])}</div>
</div>""")

    fill_rows = []
    for f in reversed(fills[-15:]):
        mk = [fnum(f.get(f"markout_{h}s")) for h in HORIZONS]
        cells = "".join(f'<td class="{"pos" if v is not None and v >= 0 else "neg" if v is not None else "muted"}">{cents(v, True)}</td>' for v in mk)
        q = next((names[c]["q"] for c in cids for t in names.get(c, {}).get("tokens", []) if t["token_id"] == f.get("token_id")), "?")
        outc = next((t["outcome"] for c in cids for t in names.get(c, {}).get("tokens", []) if t["token_id"] == f.get("token_id")), "")
        fill_rows.append(f"<tr><td>{esc(f.get('fill_time_utc', '')[11:])}</td><td class='trunc'>{esc(q)}</td>"
                         f"<td>BUY {esc(outc)}</td><td>{cents(fnum(f.get('fill_price')))}</td>"
                         f"<td>{esc(f.get('fill_size'))}</td>{cells}</tr>")
    fills_html = ("".join(fill_rows) if fill_rows else
                  '<tr><td colspan="8" class="empty">No simulated fills yet. Quiet markets + wide quotes = this can take hours. That\'s the point 😴</td></tr>')

    ev_rows = "".join(
        f'<li class="lv-{esc(e.get("level", "info"))}"><span class="muted">{esc(str(e.get("timestamp", ""))[11:19])}</span> '
        f'<b>{esc(e.get("event"))}</b> {esc(" ".join(f"{k}={v}" for k, v in e.items() if k not in ("event", "level", "timestamp"))[:110])}</li>'
        for e in reversed(st.events)) or '<li class="muted">nothing notable</li>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15">
<title>Paper MM Dashboard</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#14161a;--muted:#6b7280;--line:#e5e7eb;--pos:#0f9d58;--neg:#d93025;--bid:#1a73e8;--ask:#e8710a;--warn:#f9ab00}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#0e1013;--card:#171a1f;--ink:#e8eaed;--muted:#9aa0a6;--line:#2a2f36;--pos:#34d399;--neg:#f87171;--bid:#60a5fa;--ask:#fb923c;--warn:#fbbf24}}}}:root[data-theme="dark"]{{--bg:#0e1013;--card:#171a1f;--ink:#e8eaed;--muted:#9aa0a6;--line:#2a2f36;--pos:#34d399;--neg:#f87171;--bid:#60a5fa;--ask:#fb923c;--warn:#fbbf24}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:20px 16px 40px}}
header{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between;margin-bottom:16px}}
h1{{font-size:20px;margin:0}}.sub{{color:var(--muted);font-size:12px}}
.badge{{font-weight:700;font-size:11px;letter-spacing:.06em;padding:4px 9px;border-radius:999px;border:1px solid}}
.paper{{color:var(--bid);border-color:var(--bid)}}.ok{{color:var(--pos);border-color:var(--pos)}}.warn{{color:var(--warn);border-color:var(--warn)}}.bad{{color:var(--neg);border-color:var(--neg)}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(150px,100%),1fr));gap:10px;margin-bottom:16px}}
.kpi,.card,.panel{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}
.kpi-l,.lbl{{color:var(--muted);font-size:12px}}.kpi-v{{font-size:24px;font-weight:700;margin:2px 0;font-variant-numeric:tabular-nums}}.kpi-s{{color:var(--muted);font-size:11px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:10px;margin-bottom:16px}}
.card-h{{display:flex;gap:8px;justify-content:space-between;align-items:flex-start;margin-bottom:10px}}.q{{font-weight:600}}
.pill{{font-size:10px;font-weight:700;padding:3px 7px;border-radius:6px;background:var(--line);white-space:nowrap}}
.r-quiet{{color:var(--pos)}}.r-trending{{color:var(--warn)}}.r-event{{color:var(--neg)}}
.row3{{display:grid;grid-template-columns:1fr 1fr 1fr;text-align:center}}.v{{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}}
.bid{{color:var(--bid)}}.ask{{color:var(--ask)}}
.spark{{width:100%;height:70px;margin:8px 0 4px;display:block}}.midl{{fill:none;stroke:var(--ink);stroke-width:1.6}}
.bidl{{stroke:var(--bid);stroke-dasharray:4 3;stroke-width:1}}.askl{{stroke:var(--ask);stroke-dasharray:4 3;stroke-width:1}}
.spark-empty{{height:70px;display:grid;place-items:center;color:var(--muted);font-size:12px}}
.meta{{color:var(--muted);font-size:11px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}}@media(max-width:760px){{.grid2{{grid-template-columns:1fr}}}}
h2{{font-size:14px;margin:0 0 10px}}
.mo-row{{display:grid;grid-template-columns:44px 1fr 120px;gap:8px;align-items:center;margin:8px 0}}
.mo-track{{position:relative;height:14px;background:var(--line);border-radius:4px}}.mo-zero{{position:absolute;left:50%;top:-3px;bottom:-3px;width:1px;background:var(--muted)}}
.mo-bar{{position:absolute;top:0;bottom:0;border-radius:4px}}.mo-bar.pos{{background:var(--pos)}}.mo-bar.neg{{background:var(--neg)}}
.mo-val{{font-variant-numeric:tabular-nums;font-size:12px}}.mo-lbl{{color:var(--muted);font-size:12px}}
.legend{{color:var(--muted);font-size:11px;margin-top:6px}}
ul.ev{{list-style:none;margin:0;padding:0;font-size:12px;max-height:260px;overflow:auto}}ul.ev li{{padding:4px 0;border-bottom:1px solid var(--line);overflow-wrap:anywhere}}
.lv-warning b{{color:var(--warn)}}.lv-error b,.lv-critical b{{color:var(--neg)}}
.tbl{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}}
th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}}th{{color:var(--muted);font-weight:600}}
td.trunc{{max-width:260px;overflow:hidden;text-overflow:ellipsis}}.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}.muted{{color:var(--muted)}}.empty{{text-align:center;color:var(--muted);padding:18px}}
</style></head><body><div class="wrap">
<header><div><h1>📈 Paper Market-Maker</h1><div class="sub">updated {time.strftime('%H:%M:%S UTC', time.gmtime(now))} · auto-refreshing · running {int(up // 60)}m · last feed event {f'{int(age)}s ago' if age is not None else '–'} · {st.requotes} requotes</div></div>
<div style="display:flex;gap:6px"><span class="badge paper">PAPER · NO REAL ORDERS</span><span class="badge {status_cls}">{status_txt}</span></div></header>
<div class="kpis">{kpi_html}</div>
{ab_html(Path("."), all_time_rows(Path(".")), load_rewards(Path(".")))}
{all_time_html(all_time_rows(Path(".")), load_rewards(Path(".")))}
{rewards_table_html(load_rewards(Path(".")))}
<div class="cards">{''.join(cards) or '<div class="card muted">No markets in config/markets.toml</div>'}</div>
<div class="grid2">
 <div class="panel"><h2>Avg markout after fills</h2>{markout_chart(fills)}
 <div class="legend">Green = price moved our way after a fill. Red = we got picked off (adverse selection). Consistent red at +5m = strategy is cooked.</div></div>
 <div class="panel"><h2>Bot events (warnings & state)</h2><ul class="ev">{ev_rows}</ul></div>
</div>
<div class="panel" style="margin-bottom:16px"><h2>🩸 Biggest losses (≥ $1 after 5 min)</h2><div class="tbl"><table>
<tr><th>PnL</th><th>Market</th><th>Fill</th><th>Shares</th><th>Mid at fill</th><th>Quote age</th><th>Likely why</th></tr>{big_losses_html(LOSS_PATH, names)}</table></div></div>
<div class="panel"><h2>Recent simulated fills</h2><div class="tbl"><table>
<tr><th>Time (UTC)</th><th>Market</th><th>Side</th><th>Price</th><th>Shares</th><th>+10s</th><th>+1m</th><th>+5m</th></tr>{fills_html}</table></div></div>
</div></body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--out", default="my_tools/dashboard.html")
    ap.add_argument("--every", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--snapshot-out", default="my_tools/dashboard_snapshot.html",
                    help="copy without auto-refresh, for publishing as a page")
    a = ap.parse_args()
    root = Path(a.root)
    st = State()
    while True:
        st.feed_journal(root / "journal/paper.jsonl")
        st.feed_logs(root / "logs/paper.jsonl")
        names = market_names(root / "state.db")
        cids = tracked_cids(root / "config/markets.toml", names)
        page = render(st, names, cids, read_fills(root / "my_tools/markout.csv"))
        tmp = Path(a.out + ".tmp")
        tmp.write_text(page)
        tmp.replace(a.out)
        if a.snapshot_out:
            import re
            Path(a.snapshot_out).write_text(re.sub(r'<meta http-equiv="refresh"[^>]*>', "", page))
        if a.once:
            break
        time.sleep(a.every)


if __name__ == "__main__":
    main()
