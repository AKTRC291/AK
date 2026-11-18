"""Swing (3-10 day) and investment signal rules."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class Signal:
    symbol: str
    kind: str          # BREAKOUT | PULLBACK | OVERBOUGHT | TREND-BREAK | ZONE | VALUE | EVENT | MOVE
    price: float
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    note: str = ""

    def row(self) -> str:
        if self.entry:
            return f"{self.symbol} {self.kind} @{self.price:,.2f} | E {self.entry:,.2f} SL {self.stop:,.2f} T {self.target:,.2f} {self.note}".strip()
        return f"{self.symbol} {self.kind} @{self.price:,.2f} {self.note}".strip()


def uptrend(r: pd.Series) -> bool:
    if pd.isna(r["sma_fast"]):
        return False
    if pd.isna(r["sma_slow"]):          # <200 days of history: use 50DMA only
        return r["close"] > r["sma_fast"]
    return r["close"] > r["sma_fast"] > r["sma_slow"]


def liquid(r: pd.Series, p_univ: dict) -> bool:
    return bool(r["value20"] >= p_univ["min_avg_traded_value_rs"])


def entry_flags(r: pd.Series, p: dict) -> list[str]:
    """Which entry setups fire on bar r (needs indicators.add_all columns)."""
    out = []
    if not uptrend(r):
        return out
    if r["close"] > r["hh"] and r["volume"] >= p["breakout_volume_mult"] * r["vol20"]:
        out.append("BREAKOUT")
    if not pd.isna(r["sma_slow"]) and r["close"] > r["sma_slow"] and r["rsi2"] <= p["pullback_rsi_max"]:
        out.append("PULLBACK")
    return out


def swing_signals(sym: str, df: pd.DataFrame, p: dict, p_univ: dict) -> list[Signal]:
    if len(df) < p["trend_fast"] + 5:
        return []
    r, prev = df.iloc[-1], df.iloc[-2]
    if not liquid(r, p_univ):
        return []
    sigs = []
    for kind in entry_flags(r, p):
        e = r["close"]
        risk = p["stop_atr_mult"] * r["atr"]
        note = f"vol {r['volume']/r['vol20']:.1f}x" if kind == "BREAKOUT" else f"RSI2 {r['rsi2']:.0f}"
        sigs.append(Signal(sym, kind, e, e, e - risk, e + p["target_r_multiple"] * risk, note))
    if r["rsi14"] >= p["overbought_rsi14"]:
        sigs.append(Signal(sym, "OVERBOUGHT", r["close"], note=f"RSI14 {r['rsi14']:.0f}"))
    if prev["close"] >= prev["sma_fast"] and r["close"] < r["sma_fast"] and r["volume"] >= r["vol20"]:
        sigs.append(Signal(sym, "TREND-BREAK", r["close"], note="closed below 50DMA on volume"))
    return sigs


def investment_signals(sym: str, price: float, snap, p_inv: dict, index_pe: float | None) -> list[Signal]:
    out = []
    z = (p_inv.get("zones") or {}).get(sym)
    if z:
        for name in ("accumulate", "trim"):
            lo, hi = z.get(name, [None, None])
            if lo is not None and lo <= price <= hi:
                out.append(Signal(sym, "ZONE", price, note=f"in {name} zone {lo}-{hi}"))
    if snap is not None:
        notes = []
        if snap.pe_ttm and index_pe and 0 < snap.pe_ttm < p_inv["pe_cheap_vs_index"] * index_pe:
            notes.append(f"P/E {snap.pe_ttm:.1f} vs mkt {index_pe:.1f}")
        if snap.low_52w and price <= snap.low_52w * (1 + p_inv["near_52w_low_pct"] / 100):
            notes.append(f"near 52w low {snap.low_52w:,.0f}")
        q = list(snap.quarterly_eps.values())
        if len(q) >= 4 and q[0] is not None and q[3] not in (None, 0) and q[0] < q[3]:
            notes.append("latest qtr EPS below yr-ago")  # quality check before calling it cheap
        if notes and not (len(notes) == 1 and notes[0].startswith("latest")):
            cav = (p_inv.get("pe_caveats") or {}).get(sym)
            if cav:
                notes.append(f"[{cav}]")
            out.append(Signal(sym, "VALUE", price, note="; ".join(notes)))
    return out
