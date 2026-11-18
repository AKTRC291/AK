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
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DAILY = DATA_DIR / "daily"          # one CSV per trading day (whole market)
CACHE = DATA_DIR / "eod"


class PSXClient:
    def __init__(self, delay_sec: float = 0.6, timeout_sec: float = 30):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "X-Requested-With": "XMLHttpRequest",
                               "Referer": BASE + "/"})
        self.delay = delay_sec
        self.timeout = timeout_sec
        self._last = 0.0
        self.last_error = ""

    def _req(self, path: str, data: dict | None = None, tries: int = 3) -> requests.Response:
        for attempt in range(tries):
            wait = self.delay - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            try:
                if data is None:
                    r = self.s.get(BASE + path, timeout=self.timeout)
                else:
                    r = self.s.post(BASE + path, data=data, timeout=self.timeout)
                self._last = time.time()
                if r.status_code == 200:
                    return r
                self.last_error = f"HTTP {r.status_code}"
                if r.status_code == 404:
                    break
            except requests.RequestException as e:
                self._last = time.time()
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(2 * (attempt + 1))
        raise PSXUnavailable(f"{path}: {self.last_error}")

    _get = _req

    # ---------- whole-market daily table (PSX 'Historical Data' page) ----------
    def day_table(self, date: str) -> pd.DataFrame:
        """All equities for one date: index=symbol, cols open high low close volume ldcp."""
        html = self._req("/historical", data={"date": date}).text
        return parse_price_table(html)

    def market_watch(self) -> pd.DataFrame:
        """Live snapshot of all symbols (same columns as day_table; close = current)."""
        return parse_price_table(self._req("/market-watch").text)

    # ---------- per-symbol JSON (may be filtered; used only as fallback) ----------
    def eod_timeseries(self, symbol: str) -> pd.DataFrame:
        return eod_from_json(self._req(f"/timeseries/eod/{symbol}", tries=1).json())

    # ---------- universe ----------
    def index_constituents(self, code: str = "KSE100") -> list[str]:
        return parse_constituents(self._req(f"/indices/{code}").text)

    # ---------- fundamentals / announcements ----------
    def company(self, symbol: str) -> "CompanySnapshot":
        return parse_company(symbol, self._req(f"/company/{symbol}").text)


class History:
    """Local store of whole-market daily tables -> per-symbol OHLCV frames."""

    def __init__(self, client: PSXClient):
        self.cl = client
        DAILY.mkdir(parents=True, exist_ok=True)
        self._panel: pd.DataFrame | None = None

    def sync(self, start: str, end: str | None = None, verbose: bool = True) -> int:
        days = pd.bdate_range(start, end or pd.Timestamp.now(tz="Asia/Karachi").date())
        todo = [d for d in days if not (DAILY / f"{d:%Y-%m-%d}.csv").exists()]
        fetched = 0
        for k, d in enumerate(todo, 1):
            ds = f"{d:%Y-%m-%d}"
            try:
                df = self.cl.day_table(ds)
            except PSXUnavailable as e:
                if verbose:
                    print(f"  {ds}: failed ({e})")
                continue
            if df.empty and d.date() >= pd.Timestamp.now(tz="Asia/Karachi").date():
                continue                     # today's table not published yet; retry later
            df.to_csv(DAILY / f"{ds}.csv")   # empty file = holiday / no trading
            fetched += 1
            if verbose and (k % 25 == 0 or k == len(todo)):
                print(f"  synced {k}/{len(todo)} days (last {ds}, {len(df)} symbols)")
        self._panel = None
        return fetched

    def panel(self) -> pd.DataFrame:
        if self._panel is None:
            frames = []
            for f in sorted(DAILY.glob("*.csv")):
                try:
                    df = pd.read_csv(f, index_col=0)
                except pd.errors.EmptyDataError:
                    continue
                if not df.empty:
                    df["date"] = pd.Timestamp(f.stem)
                    frames.append(df)
            self._panel = (pd.concat(frames).rename_axis("symbol").reset_index()
                           if frames else pd.DataFrame(columns=["symbol", "date"]))
        return self._panel

    def symbol(self, sym: str) -> pd.DataFrame:
        p = self.panel()
        df = p[p["symbol"] == sym].set_index("date").sort_index()
        df = df[["open", "high", "low", "close", "volume"]].astype(float)
        df = df[df["close"] > 0]
        for c in ("open", "high", "low"):
            df[c] = df[c].where(df[c] > 0, df["close"])
        return df

    def has_data(self) -> bool:
        return any(f.stat().st_size > 50 for f in DAILY.glob("*.csv"))


class PSXUnavailable(RuntimeError):
    pass


def eod_from_json(j: dict) -> pd.DataFrame:
    rows = j.get("data") or []
    df = pd.DataFrame([r[:4] for r in rows], columns=["ts", "close", "volume", "open"])
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Karachi").dt.normalize().dt.tz_localize(None)
    df = df.drop(columns="ts").drop_duplicates("date", keep="last").set_index("date").sort_index()
    df["open"] = df["open"].fillna(df["close"])
    return df[["open", "close", "volume"]].astype(float)


_COLMAP = {"symbol": "symbol", "scrip": "symbol", "ldcp": "ldcp", "open": "open", "high": "high", "low": "low",
           "close": "close", "current": "close", "volume": "volume", "change": "change", "change (%)": "change_pct"}


def parse_price_table(html: str) -> pd.DataFrame:
    """Parse PSX historical / market-watch HTML tables by header name."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "ldcp"])
    heads = [th.get_text(" ", strip=True).lower() for th in table.find_all("th")]
    cols = [_COLMAP.get(h, _COLMAP.get(h.split(" ")[0], h)) for h in heads]
    rows = []
    for tr in table.select("tbody tr"):
        if tr.get("data-type") == "debt":
            continue
        tds = tr.find_all("td")
        if len(tds) < len(cols):
            continue
        rec = {}
        for c, td in zip(cols, tds):
            if c == "symbol":
                rec[c] = (td.get("data-value") or td.get_text(" ", strip=True).split(" ")[0]).strip()
            elif c in ("open", "high", "low", "close", "volume", "ldcp", "change", "change_pct"):
                rec[c] = _num(td.get_text(strip=True))
        if rec.get("symbol"):
            rows.append(rec)
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "ldcp"])
    df = df.drop_duplicates("symbol").set_index("symbol")
    for c in ("open", "high", "low", "close", "volume", "ldcp"):
        if c not in df:
            df[c] = None
    return df


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
