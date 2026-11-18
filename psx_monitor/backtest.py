"""Backtest of the swing rules on PSX EOD history.

Execution model (deliberately conservative):
  - signal on day t close -> enter at day t+1 OPEN
  - exits checked on CLOSE only (feed has no high/low): stop, target,
    PULLBACK mean-reversion exit (close > 5DMA), or time stop at max_hold_days
  - one open trade per symbol; cost 0.25% per side (brokerage + CVT/SST + slippage)
"""
from __future__ import annotations

import pandas as pd

from .indicators import add_all
from .signals import entry_flags

COST = 0.0025


def backtest_symbol(sym: str, raw: pd.DataFrame, p: dict, p_univ: dict, start: str | None = None) -> list[dict]:
    df = add_all(raw, p)
    if start:
        first = df.index.searchsorted(pd.Timestamp(start))
    else:
        first = p["trend_slow"]
    trades, i, n = [], max(first, p["trend_fast"] + 1), len(df)
    while i < n - 1:
        r = df.iloc[i]
        kinds = entry_flags(r, p) if r["value20"] >= p_univ["min_avg_traded_value_rs"] else []
        if not kinds:
            i += 1
            continue
        kind = kinds[0]
        e_i = i + 1
        entry = df["open"].iloc[e_i]
        risk = p["stop_atr_mult"] * r["atr"]
        stop, target = entry - risk, entry + p["target_r_multiple"] * risk
        exit_i, reason = None, "time"
        for j in range(e_i, min(e_i + p["max_hold_days"], n)):
            c = df["close"].iloc[j]
            if c <= stop:
                exit_i, reason = j, "stop"; break
            if c >= target:
                exit_i, reason = j, "target"; break
            if kind == "PULLBACK" and j > e_i and c > df["sma5"].iloc[j]:
                exit_i, reason = j, "mean-rev"; break
        if exit_i is None:
            exit_i = min(e_i + p["max_hold_days"] - 1, n - 1)
        px = df["close"].iloc[exit_i]
        ret = (px * (1 - COST)) / (entry * (1 + COST)) - 1
        trades.append(dict(symbol=sym, setup=kind, signal_date=df.index[i].date(), entry_date=df.index[e_i].date(),
                           exit_date=df.index[exit_i].date(), entry=entry, exit=px, days=exit_i - e_i + 1,
                           ret=ret, r_mult=(px - entry) / risk if risk else 0, reason=reason))
        i = exit_i + 1
    return trades


def summarize(t: pd.DataFrame) -> pd.DataFrame:
    if t.empty:
        return pd.DataFrame()
    def stats(g):
        wins, losses = g.loc[g.ret > 0, "ret"], g.loc[g.ret <= 0, "ret"]
        pf = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
        return pd.Series({"trades": len(g), "win_rate_%": 100 * len(wins) / len(g), "avg_ret_%": 100 * g.ret.mean(),
                          "avg_win_%": 100 * wins.mean() if len(wins) else 0, "avg_loss_%": 100 * losses.mean() if len(losses) else 0,
                          "profit_factor": pf, "avg_R": g.r_mult.mean(), "avg_days": g.days.mean()})
    out = t.groupby("setup").apply(stats, include_groups=False)
    out.loc["ALL"] = stats(t)
    return out.round(2)
