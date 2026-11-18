"""Data access for the PSX Data Portal (dps.psx.com.pk).

The portal's JSON endpoints (/timeseries, /symbols, /indices, /market-watch)
only answer from Pakistani IPs, so this module must run on a machine in
Pakistan. The /company/<SYMBOL> HTML page is used for fundamentals and
corporate announcements.

Endpoint formats (taken from the portal's own script.js):
  /timeseries/eod/<SYM>  -> {"data": [[unix_ts, close, volume, open], ...]}
  /timeseries/int/<SYM>  -> {"data": [[unix_ts, price, volume], ...]}  (today's ticks)
  /indices/<CODE>        -> HTML table of constituents
  /market-watch          -> HTML table of all symbols for the day
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://dps.psx.com.pk"
UA = "psx-monitor/0.1 (personal research tool)"
CACHE = Path(__file__).resolve().parent.parent / "data" / "eod"


class PSXClient:
    def __init__(self, delay_sec: float = 0.6, timeout_sec: float = 30):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest"})
        self.delay = delay_sec
        self.timeout = timeout_sec
        self._last = 0.0

    def _get(self, path: str) -> requests.Response:
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            try:
                r = self.s.get(BASE + path, timeout=self.timeout)
                self._last = time.time()
                if r.status_code == 200:
                    return r
                if r.status_code == 404:
                    raise PSXUnavailable(f"{path}: 404 (portal data is geo-restricted to Pakistan IPs)")
            except requests.RequestException:
                pass
            time.sleep(2 * (attempt + 1))
        raise PSXUnavailable(f"{path}: no response after retries")

    # ---------- prices ----------
    def eod(self, symbol: str) -> pd.DataFrame:
        """Full daily history: index=date, columns open, close, volume."""
        j = self._get(f"/timeseries/eod/{symbol}").json()
        return eod_from_json(j)

    def eod_cached(self, symbol: str, max_age_hours: float = 12) -> pd.DataFrame:
        CACHE.mkdir(parents=True, exist_ok=True)
        f = CACHE / f"{symbol}.csv"
        if f.exists() and (time.time() - f.stat().st_mtime) < max_age_hours * 3600:
            return pd.read_csv(f, index_col=0, parse_dates=True)
        df = self.eod(symbol)
        df.to_csv(f)
        return df

    def intraday(self, symbol: str) -> pd.DataFrame:
        j = self._get(f"/timeseries/int/{symbol}").json()
        rows = j.get("data") or []
        df = pd.DataFrame([r[:3] for r in rows], columns=["ts", "price", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Karachi")
        return df.sort_values("ts").reset_index(drop=True)

    # ---------- universe ----------
    def index_constituents(self, code: str = "KSE100") -> list[str]:
        html = self._get(f"/indices/{code}").text
        return parse_constituents(html)

    # ---------- fundamentals / announcements ----------
    def company(self, symbol: str) -> "CompanySnapshot":
        return parse_company(symbol, self._get(f"/company/{symbol}").text)


class PSXUnavailable(RuntimeError):
    pass


def eod_from_json(j: dict) -> pd.DataFrame:
    rows = j.get("data") or []
    df = pd.DataFrame([r[:4] for r in rows], columns=["ts", "close", "volume", "open"])
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Karachi").dt.normalize().dt.tz_localize(None)
    df = df.drop(columns="ts").drop_duplicates("date", keep="last").set_index("date").sort_index()
    df["open"] = df["open"].fillna(df["close"])
    return df[["open", "close", "volume"]].astype(float)


def parse_constituents(html: str) -> list[str]:
    syms = re.findall(r'/company/([A-Z0-9]+)"', html)
    if not syms:  # fallback: first column of the table
        soup = BeautifulSoup(html, "html.parser")
        syms = [tr.find("td").get_text(strip=True) for tr in soup.select("tbody tr") if tr.find("td")]
    return list(dict.fromkeys(syms))


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.replace(",", "").replace("Rs.", "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()%")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


@dataclass
class CompanySnapshot:
    symbol: str
    name: str = ""
    price: float | None = None
    pe_ttm: float | None = None
    low_52w: float | None = None
    high_52w: float | None = None
    shares: float | None = None
    free_float_pct: float | None = None
    annual_eps: dict = field(default_factory=dict)      # {"2026": 31.83, ...}
    quarterly_eps: dict = field(default_factory=dict)   # {"Q1 2027": 11.69, ...}
    announcements: list = field(default_factory=list)   # [(date, title), ...] newest first


def parse_company(symbol: str, html: str) -> CompanySnapshot:
    snap = CompanySnapshot(symbol=symbol)
    m = re.search(r'class="quote__name">([^<]*)<', html)
    snap.name = m.group(1).strip() if m else ""
    m = re.search(r'class="quote__close">([^<]*)<', html)
    snap.price = _num(m.group(1)) if m else None

    stats = {}
    for k, v in re.findall(r'class="stats_label">([^<]*)</div>\s*<div class="stats_value">([^<]*)<', html):
        stats.setdefault(k.strip(), v.strip())  # first occurrence = REG market
    snap.pe_ttm = _num(stats.get("P/E Ratio (TTM) **"))
    rng = stats.get("52-WEEK RANGE ^", "")
    parts = [p for p in re.split(r"\s*[—-]\s*", rng) if p]
    if len(parts) == 2:
        snap.low_52w, snap.high_52w = _num(parts[0]), _num(parts[1])
    snap.shares = _num(stats.get("Shares"))
    ff = [v for k, v in re.findall(r'class="stats_label">([^<]*)</div>\s*<div class="stats_value">([^<]*)<', html) if k.strip() == "Free Float" and "%" in v]
    snap.free_float_pct = _num(ff[0]) if ff else None

    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])] for tr in table.find_all("tr")]
        rows = [r for r in rows if r]
        if not rows:
            continue
        head = rows[0]
        if head and head[0] in ("Date",) and len(head) >= 2 and head[1] == "Title":
            for r in rows[1:]:
                if len(r) >= 2:
                    snap.announcements.append((r[0], r[1]))
            continue
        eps_row = next((r for r in rows if r and r[0] == "EPS"), None)
        if eps_row:
            periods = [h for h in head if re.match(r"^(Q\d )?\d{4}$", h)]
            vals = [_num(v) for v in eps_row[1:]]
            target = snap.quarterly_eps if periods and periods[0].startswith("Q") else snap.annual_eps
            target.update({p: v for p, v in zip(periods, vals) if v is not None})
    # newest first, de-duplicated across the several announcement tabs
    seen, ann = set(), []
    for d, t in snap.announcements:
        if (d, t) not in seen:
            seen.add((d, t)); ann.append((d, t))
    snap.announcements = sorted(ann, key=lambda x: pd.to_datetime(x[0], errors="coerce"), reverse=True)
    return snap
