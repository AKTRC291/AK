"""CLI entry point.

  python -m psx_monitor close        # post-close full scan (KSE-100 + watchlist) -> WhatsApp
  python -m psx_monitor preopen      # 9:15 brief: today's trigger levels + events -> WhatsApp
  python -m psx_monitor intraday     # every 30 min in market hours: new triggers only
  python -m psx_monitor backtest [--start 2023-11-01]
  python -m psx_monitor test-whatsapp
  python -m psx_monitor diagnose     # which PSX endpoints answer from this PC
  python -m psx_monitor import       # load terminal exports from inbox/
Add --dry-run to print instead of sending.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

from .data import History, PSXClient, PSXUnavailable, import_inbox
from .indicators import add_all
from .notify import send_whatsapp
from .signals import investment_signals, swing_signals

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PKT = ZoneInfo("Asia/Karachi")
EVENT_WORDS = ("Financial Results", "Board Meeting", "Material Information", "Book Closure",
               "Right Shares", "Right Issue", "Bonus", "General Meeting", "Merger", "Acquisition", "Buy-back", "Buyback")
EVENT_SKIP = ("credit of", "dispatch", "publication", "transmission", "dissemination")  # admin notices


def cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


def now() -> dt.datetime:
    return dt.datetime.now(PKT)


def is_trading_day(c: dict, d: dt.date | None = None) -> bool:
    d = d or now().date()
    return d.weekday() < 5 and d.isoformat() not in {str(h) for h in c.get("holidays", [])}


def macro_lines(c: dict) -> str:
    f = ROOT / c["macro"]["file"]
    return f.read_text().strip() if f.exists() else "Macro: (data/macro.txt not set)"


def universe(cl: PSXClient, c: dict) -> list[str]:
    try:
        u = cl.index_constituents(c["universe"]["index"])
        (DATA / "universe.json").write_text(json.dumps(u))
    except PSXUnavailable:
        f = DATA / "universe.json"
        u = json.loads(f.read_text()) if f.exists() else []
    return list(dict.fromkeys(u + c["universe"]["extra_watchlist"]))


def recent(date_str: str, days: int = 1) -> bool:
    d = pd.to_datetime(date_str, errors="coerce")
    return pd.notna(d) and (now().date() - d.date()).days <= days


def run_close(c: dict, dry: bool) -> None:
    cl = PSXClient(c["http"]["delay_sec"], c["http"]["timeout_sec"])
    syms = universe(cl, c)
    sw, p_u = c["swing"], c["universe"]
    import_inbox()                       # terminal exports dropped in inbox/
    hist = History(cl)   # prices come only from terminal exports (PSX blocks automated price requests)
    swing, inv, events, levels, snaps, failed = [], [], [], {}, {}, []
    for s in syms:
        try:
            snaps[s] = cl.company(s)
        except PSXUnavailable:
            pass
        try:
            raw = hist.symbol(s)
            if len(raw) < sw["trend_fast"] + 5:
                raise PSXUnavailable("not enough history")
            df = add_all(raw, sw)
        except PSXUnavailable:
            failed.append(s); continue
        swing += swing_signals(s, df, sw, p_u)
        r = df.iloc[-1]
        # levels for tomorrow's intraday breakout watch
        hh_next = df["close"].iloc[-sw["breakout_lookback"]:].max()
        levels[s] = dict(close=r["close"], breakout=hh_next, vol20=r["vol20"], atr=r["atr"],
                         uptrend=bool(r["close"] > r["sma_fast"]), value20=r["value20"])
    pes = [x.pe_ttm for x in snaps.values() if x.pe_ttm and 0 < x.pe_ttm < 100]
    index_pe = statistics.median(pes) if pes else None
    for s, snap in snaps.items():
        price = levels.get(s, {}).get("close") or snap.price
        if price:
            inv += investment_signals(s, price, snap, c["investment"], index_pe)
        for d, t in snap.announcements:
            tl = t.lower()
            if recent(d) and any(w.lower() in tl for w in EVENT_WORDS) and not any(x in tl for x in EVENT_SKIP):
                events.append(f"{s}: {t} ({d})")
    (DATA / "levels.json").write_text(json.dumps(levels, default=float))

    p = hist.panel()
    asof = pd.Timestamp(p["date"].max()).date() if "date" in p and len(p) else None
    stale = asof is not None and asof < now().date() and now().time() >= dt.time(15, 45)
    head = f"*PSX close scan {now():%d-%b-%Y}*"
    if asof:
        head += f" | prices as of {asof:%d-%b}" + (" (STALE - save today's export to inbox/)" if stale else "")
    lines = [head, macro_lines(c), ""]
    order = {"BREAKOUT": 0, "PULLBACK": 1, "TREND-BREAK": 2, "OVERBOUGHT": 3}
    lines.append("*Swing (3-10d)*")
    if failed and len(failed) == len(syms):
        lines.append("no price data - save today's terminal export into inbox/")
    else:
        lines += [x.row() for x in sorted(swing, key=lambda x: order.get(x.kind, 9))] or ["none"]
    zones = [x for x in inv if x.kind == "ZONE"]
    value = sorted((x for x in inv if x.kind == "VALUE"), key=lambda x: x.score, reverse=True)
    n_val = c["investment"].get("max_value_flags", 10)
    lines += ["", "*Investment*"] + ([x.row() for x in zones + value[:n_val]] or ["none"])
    if len(value) > n_val:
        lines.append(f"(+{len(value) - n_val} more cheap names not shown)")
    if events:
        lines += ["", "*Events*"] + events
    if index_pe:
        lines.append(f"\nUniverse median P/E {index_pe:.1f} | scanned {len(syms) - len(failed)}/{len(syms)}")
    if failed and len(failed) < len(syms):
        lines.append(f"No/short price history: {', '.join(failed[:15])}{' ...' if len(failed) > 15 else ''}")
    send_whatsapp("\n".join(lines), dry)


def run_preopen(c: dict, dry: bool) -> None:
    f = DATA / "levels.json"
    if not f.exists():
        send_whatsapp("PSX pre-open: no levels yet - run the close scan first.", dry); return
    lv = json.loads(f.read_text())
    watch = sorted(((s, v) for s, v in lv.items() if v["uptrend"] and v["value20"] >= c["universe"]["min_avg_traded_value_rs"]
                    and v["close"] >= 0.97 * v["breakout"]), key=lambda x: x[1]["close"] / x[1]["breakout"], reverse=True)[:15]
    lines = [f"*PSX pre-open {now():%d-%b}*", macro_lines(c), "", "*Breakout watch (within 3% of 20d high)*"]
    lines += [f"{s} {v['close']:,.2f} -> trigger >{v['breakout']:,.2f} on {c['swing']['breakout_volume_mult']}x vol" for s, v in watch] or ["none"]
    send_whatsapp("\n".join(lines), dry)


def run_intraday(c: dict, dry: bool) -> None:
    t = now()
    if not (dt.time(9, 30) <= t.time() <= dt.time(15, 35)):
        return
    lv = json.loads((DATA / "levels.json").read_text()) if (DATA / "levels.json").exists() else {}
    state_f = DATA / "intraday_state.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    if state.get("date") != t.date().isoformat():
        state = {"date": t.date().isoformat(), "sent": []}
    cl = PSXClient(c["http"]["delay_sec"], c["http"]["timeout_sec"])
    session_frac = max(0.05, min(1.0, ((t.hour - 9) * 60 + t.minute - 30) / 360))
    zones = c["investment"].get("zones") or {}
    watch = [s for s, v in lv.items() if v["uptrend"] and v["close"] >= 0.97 * v["breakout"]]
    watch = list(dict.fromkeys(watch + c["universe"]["extra_watchlist"] + list(zones)))
    alerts = []
    for s in watch[:40]:                  # company pages: public, fetched politely
        try:
            snap = cl.company(s)
        except PSXUnavailable:
            continue
        if not snap.price:
            continue
        px, vol = snap.price, snap.volume or 0.0
        v = lv.get(s)
        if v:
            chg = 100 * (px / v["close"] - 1)
            if v["uptrend"] and px > v["breakout"] and vol >= c["swing"]["breakout_volume_mult"] * v["vol20"] * session_frac:
                key = f"{s}:BRK"
                if key not in state["sent"]:
                    risk = c["swing"]["stop_atr_mult"] * v["atr"]
                    alerts.append(f"{s} BREAKOUT live {px:,.2f} (>{v['breakout']:,.2f}, vol pace {vol/(v['vol20']*session_frac):.1f}x) "
                                  f"SL {px-risk:,.2f} T {px+c['swing']['target_r_multiple']*risk:,.2f}")
                    state["sent"].append(key)
            if abs(chg) >= c["intraday"]["big_move_pct"] and f"{s}:MOVE" not in state["sent"]:
                alerts.append(f"{s} MOVE {chg:+.1f}% @ {px:,.2f}"); state["sent"].append(f"{s}:MOVE")
        for name, (lo, hi) in ((n, zones.get(s, {}).get(n, [None, None])) for n in ("accumulate", "trim")):
            if lo is not None and lo <= px <= hi and f"{s}:{name}" not in state["sent"]:
                alerts.append(f"{s} in {name} zone {lo}-{hi} @ {px:,.2f}"); state["sent"].append(f"{s}:{name}")
    state_f.write_text(json.dumps(state))
    if alerts:
        send_whatsapp(f"*PSX intraday {t:%H:%M}*\n" + "\n".join(alerts), dry)


def run_backtest(c: dict, start: str | None) -> None:
    from .backtest import backtest_symbol, summarize
    cl = PSXClient(c["http"]["delay_sec"], c["http"]["timeout_sec"])
    start = start or str((now() - dt.timedelta(days=3 * 365)).date())
    warmup = str((pd.Timestamp(start) - pd.Timedelta(days=330)).date())  # 200DMA warm-up
    import_inbox()
    hist = History(cl)
    trades = []
    syms = universe(cl, c)
    for s in syms:
        raw = hist.symbol(s)
        if len(raw) < c["swing"]["trend_slow"]:
            print("skip", s, f"only {len(raw)} days", file=sys.stderr); continue
        trades += backtest_symbol(s, raw, c["swing"], c["universe"], start)
    t = pd.DataFrame(trades)
    DATA.mkdir(exist_ok=True)
    t.to_csv(DATA / "backtest_trades.csv", index=False)
    print(summarize(t).to_string())
    if not t.empty:
        t["year"] = pd.to_datetime(t.entry_date).dt.year
        print("\nBy year:\n", t.groupby(["year", "setup"]).ret.agg(["count", "mean"]).round(4).to_string())


def run_diagnose(c: dict) -> None:
    cl = PSXClient(c["http"]["delay_sec"], c["http"]["timeout_sec"])
    d = now().date() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    tests = [("index list  /indices/KSE100", lambda: f"{len(cl.index_constituents())} symbols"),
             ("company     /company/LUCK", lambda: f"price {cl.company('LUCK').price}"),
             ("price files in inbox/ + data/daily/", lambda: f"{len(list((DATA / 'daily').glob('*.csv')))} trading days stored")]
    for name, fn in tests:
        try:
            print(f"OK    {name}: {fn()}")
        except Exception as e:
            print(f"FAIL  {name}: {e}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="psx_monitor")
    ap.add_argument("mode", choices=["close", "preopen", "intraday", "backtest", "test-whatsapp", "diagnose", "import"])
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even on weekends/holidays")
    ap.add_argument("--start")
    a = ap.parse_args(argv)
    c = cfg()
    DATA.mkdir(exist_ok=True)
    if a.mode == "backtest":
        return run_backtest(c, a.start)
    if a.mode == "import":
        return print(f"{import_inbox()} file(s) imported")
    if a.mode == "diagnose":
        return run_diagnose(c)
    if a.mode == "test-whatsapp":
        return send_whatsapp("PSX monitor: WhatsApp link OK.", a.dry_run)
    if not (a.force or is_trading_day(c)):
        return
    {"close": run_close, "preopen": run_preopen, "intraday": run_intraday}[a.mode](c, a.dry_run)


if __name__ == "__main__":
    main()
