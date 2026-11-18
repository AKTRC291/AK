"""Technical indicators on PSX EOD data (open, close, volume).

Daily data comes from PSX's Historical Data page (open/high/low/close/volume),
so ATR is a true Wilder ATR. If only open/close exist, an open/close proxy is used.
"""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def atr_proxy(df: pd.DataFrame, n: int) -> pd.Series:
    """True ATR when high/low are available, else the open/close proxy."""
    pc = df["close"].shift()
    if {"high", "low"} <= set(df.columns):
        tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    else:
        tr = pd.concat([(df["close"] - df["open"]).abs(), (df["close"] - pc).abs(), (df["open"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def add_all(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    df = df.copy()
    c = df["close"]
    df["sma5"] = sma(c, 5)
    df["sma_fast"] = sma(c, p["trend_fast"])
    df["sma_slow"] = sma(c, p["trend_slow"])
    df["rsi2"] = rsi(c, p["pullback_rsi_period"])
    df["rsi14"] = rsi(c, 14)
    df["atr"] = atr_proxy(df, p["atr_period"])
    df["vol20"] = df["volume"].rolling(20, min_periods=20).mean()
    df["value20"] = (c * df["volume"]).rolling(20, min_periods=20).mean()
    df["hh"] = c.shift().rolling(p["breakout_lookback"], min_periods=p["breakout_lookback"]).max()
    df["hi252"] = c.rolling(252, min_periods=60).max()
    df["lo252"] = c.rolling(252, min_periods=60).min()
    return df
