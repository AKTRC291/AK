"""Data access for the PSX Data Portal (dps.psx.com.pk).

PSX blocks automated requests to its price endpoints (/historical, /market-watch,
/timeseries return 403), so daily prices come from broker-terminal exports dropped
into inbox/ (see import_inbox). PSX's public pages used here:
  /indices/<CODE>    -> HTML table of index constituents
  /company/<SYMBOL>  -> live price/volume, P/E, EPS history, 52w range, announcements
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
                if r.status_code in (403, 404):   # blocked / not found: retrying won't help
                    break
            except requests.RequestException as e:
                self._last = time.time()
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            time.sleep(2 * (attempt + 1))
        raise PSXUnavailable(f"{path}: {self.last_error}")

    _get = _req

    # ---------- universe ----------
    def index_constituents(self, code: str = "KSE100") -> list[str]:
        return parse_constituents(self._req(f"/indices/{code}").text)

    # ---------- fundamentals / announcements ----------
    def company(self, symbol: str) -> "CompanySnapshot":
        return parse_company(symbol, self._req(f"/company/{symbol}").text)


# ---------- terminal / manual exports ----------
INBOX = Path(__file__).resolve().parent.parent / "inbox"
_SYN = {
    "symbol": ("symbol", "scrip", "script", "code", "ticker", "sym", "scripcode", "symbolcode"),
    "date": ("date", "tradedate", "datetime", "day", "dated"),
    "open": ("open", "openprice", "opening", "openrate"),
    "high": ("high", "highprice", "dayhigh", "highrate"),
    "low": ("low", "lowprice", "daylow", "lowrate"),
    "close": ("close", "closeprice", "closing", "last", "ltp", "lastprice", "current", "currentprice", "lasttradeprice", "rate"),
    "volume": ("volume", "vol", "totalvolume", "turnovershares", "qty", "tradedvolume"),
}


def _norm(h) -> str:
    return re.sub(r"[^a-z]", "", str(h).lower())


def _parse_dates(s: pd.Series) -> pd.Series:
    """Excel datetimes, ISO yyyy-mm-dd, or Pakistani dd/mm/yyyy."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s
    iso = pd.to_datetime(s, errors="coerce", format="ISO8601")
    if iso.notna().mean() > 0.9:
        return iso
    return pd.to_datetime(s, errors="coerce", dayfirst=True)


_WIDE_SHEETS = {"open": "OPEN", "high": "HIGH", "low": "LOW", "close": "UNADJUSTED PRICE",
                "volume": "VOLUME", "adj": "Total Return Price"}


def is_wide_workbook(sheets: list[str]) -> bool:
    up = {s.strip().upper() for s in sheets}
    return {"OPEN", "HIGH", "LOW", "UNADJUSTED PRICE"} <= up


def parse_wide_workbook(path) -> pd.DataFrame:
    """Terminal workbook: one sheet per field, dates down, symbols across.

    Row 1 = column numbers, row 2 = symbols, row 3 = field name, then data.
    Calendar days with no volume (weekends/holidays) are dropped. Prices are
    back-adjusted for splits/bonus/dividends using 'Total Return Price' (which
    equals the actual price on the latest date), so moving averages stay valid.
    Returns long format: date, symbol, open, high, low, close, volume.
    """
    raw = pd.read_excel(path, sheet_name=None, header=1)
    by_up = {k.strip().upper(): v for k, v in raw.items()}
    frames = {}
    for key, sheet in _WIDE_SHEETS.items():
        df = by_up.get(sheet.upper())
        if df is None:
            continue
        df = df.rename(columns={df.columns[0]: "date"})
        df = df[pd.to_datetime(df["date"], errors="coerce").notna()]
        df.index = pd.to_datetime(df.pop("date"))
        df.columns = [str(c).strip().upper() for c in df.columns]
        frames[key] = df.apply(pd.to_numeric, errors="coerce")
    vol = frames["volume"]
    trading = vol.notna().any(axis=1)
    out = {}
    factor = (frames["adj"] / frames["close"]) if "adj" in frames else None
    for k in ("open", "high", "low", "close"):
        f = frames[k].loc[trading]
        out[k] = f * factor.loc[trading] if factor is not None else f
    out["volume"] = vol.loc[trading]
    long = pd.concat({k: v.stack(future_stack=True) for k, v in out.items()}, axis=1)
    long.index.names = ["date", "symbol"]
    long = long.dropna(subset=["close"]).reset_index()
    long = long[long["close"] > 0]
    long["volume"] = long["volume"].fillna(0)
    return long


def standardize_export(df: pd.DataFrame, name: str, default_date: str) -> dict[str, pd.DataFrame]:
    """Map any broker/PSX export to {date: DataFrame(index=symbol, open high low close volume)}."""
    cols = {}
    for c in df.columns:
        n = _norm(c)
        for std, syns in _SYN.items():
            if std not in cols and n in syns:
                cols[std] = c
    if "close" not in cols:
        raise ValueError(f"{name}: no close/last/LTP column found in {list(df.columns)[:12]}")
    out = pd.DataFrame({k: df[v] for k, v in cols.items()})
    for k in ("open", "high", "low", "close", "volume"):
        if k in out:
            out[k] = out[k].map(lambda x: _num(str(x)) if not isinstance(x, (int, float)) else x)
        else:
            out[k] = out["close"] if k != "volume" else 0.0
    if "symbol" not in out:                       # per-symbol history file, e.g. LUCK.csv
        out["symbol"] = re.sub(r"[^A-Z0-9]", "", Path(name).stem.upper().split("_")[0])
    out["symbol"] = out["symbol"].astype(str).str.strip().str.upper()
    if "date" in out:
        out["date"] = _parse_dates(out["date"]).dt.strftime("%Y-%m-%d")
    else:
        m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name)
        out["date"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else default_date
    out = out.dropna(subset=["date", "close"])
    return {d: g.drop(columns="date").drop_duplicates("symbol", keep="last").set_index("symbol")
            for d, g in out.groupby("date")}


def parse_lis(text: str) -> dict[str, pd.DataFrame]:
    """PSX daily closing file (closingNN.lis inside YYYYMMDD_new.lis.Z):
    DATE|SYMBOL|SECTOR|NAME|OPEN|HIGH|LOW|CLOSE|VOLUME|LDCP|||
    Futures rows (symbol contains '-') are skipped. Returns {YYYY-MM-DD: frame}."""
    rows = []
    for line in text.splitlines():
        f = line.strip().split("|")
        if len(f) < 10 or "-" in f[1]:
            continue
        try:
            d = pd.to_datetime(f[0], format="%d%b%Y").strftime("%Y-%m-%d")
            o, h, l, c, v = (float(x) for x in f[4:9])
        except ValueError:
            continue
        if c <= 0:
            continue
        o = o if o > 0 else c                       # no trade today: open/high/low reported as 0
        h, l = (h if h > 0 else c), (l if l > 0 else c)
        rows.append((d, f[1].strip().upper(), o, h, l, c, v))
    df = pd.DataFrame(rows, columns=["date", "symbol", "open", "high", "low", "close", "volume"])
    return {d: g.drop(columns="date").drop_duplicates("symbol").set_index("symbol") for d, g in df.groupby("date")}


def _read_lis_file(f: Path) -> dict[str, pd.DataFrame]:
    import zipfile
    raw = f.read_bytes()
    if zipfile.is_zipfile(f):
        with zipfile.ZipFile(f) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".lis")] or z.namelist()
            text = "\n".join(z.read(n).decode("latin-1") for n in names)
    elif raw[:2] == b"\x1f\x8b":
        import gzip
        text = gzip.decompress(raw).decode("latin-1")
    else:
        text = raw.decode("latin-1")
    return parse_lis(text)


def import_inbox(folder: Path = INBOX, verbose: bool = True) -> int:
    """Merge every CSV/XLSX in inbox/ into data/daily/, then move it to inbox/processed/."""
    folder.mkdir(exist_ok=True)
    DAILY.mkdir(parents=True, exist_ok=True)
    done = folder / "processed"; done.mkdir(exist_ok=True)
    today = pd.Timestamp.now(tz="Asia/Karachi").strftime("%Y-%m-%d")
    n = 0
    files = [f for f in sorted(folder.iterdir()) if f.is_file() and f.name != ".gitkeep"]
    if verbose:
        print(f"inbox: looking in {folder} - {len(files)} file(s) found")
    for f in files:
        if f.name.lower().endswith((".lis", ".lis.z", ".z", ".zip")):
            try:
                parts = _read_lis_file(f)
                if not parts:
                    raise ValueError("no price rows found")
                for d, df in parts.items():
                    target = DAILY / f"{d}.csv"
                    if target.exists() and target.stat().st_size > 50:
                        old = pd.read_csv(target, index_col=0)
                        df = pd.concat([old[~old.index.isin(df.index)], df])
                    df.to_csv(target)
                f.replace(done / f.name)
                n += 1
                if verbose:
                    for d, df in parts.items():
                        print(f"inbox: imported PSX closing file {f.name} ({len(df)} symbols, dated {d})")
            except Exception as e:
                print(f"inbox: could not read {f.name}: {e}")
            continue
        if f.suffix.lower() in (".html", ".htm"):
            try:
                df = parse_price_table(f.read_text(encoding="utf-8", errors="ignore"))
                if df.empty or df["close"].isna().all():
                    raise ValueError("no price table found - save the Market Watch page (Ctrl+S, 'Webpage, HTML only')")
                m = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", f.name)
                day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else today
                df = df[df["close"] > 0][["open", "high", "low", "close", "volume"]]
                target = DAILY / f"{day}.csv"
                if target.exists() and target.stat().st_size > 50:
                    old = pd.read_csv(target, index_col=0)
                    df = pd.concat([old[~old.index.isin(df.index)], df])
                df.to_csv(target)
                f.replace(done / f.name)
                n += 1
                if verbose:
                    print(f"inbox: imported saved page {f.name} ({len(df)} symbols, dated {day})")
            except Exception as e:
                print(f"inbox: could not read {f.name}: {e}")
            continue
        if f.suffix.lower() not in (".csv", ".xlsx", ".xls"):
            if verbose:
                print(f"inbox: skipped {f.name} - supported: .csv .xlsx .xls, PSX closing file .lis/.lis.Z, or a saved Market Watch .html page")
            continue
        try:
            if f.suffix.lower() == ".csv":
                raw = pd.read_csv(f)
            elif is_wide_workbook(pd.ExcelFile(f).sheet_names):
                raw = parse_wide_workbook(f)
                if verbose:
                    print(f"inbox: {f.name} is a multi-sheet terminal workbook "
                          f"({raw['symbol'].nunique()} symbols, {raw['date'].nunique()} trading days, split/dividend-adjusted)")
            else:
                raw = pd.read_excel(f)
            parts = standardize_export(raw, f.name, today)
        except Exception as e:
            print(f"inbox: could not read {f.name}: {e}"); continue
        for d, df in parts.items():
            target = DAILY / f"{d}.csv"
            if target.exists() and target.stat().st_size > 50:
                old = pd.read_csv(target, index_col=0)
                df = pd.concat([old[~old.index.isin(df.index)], df])
            df[["open", "high", "low", "close", "volume"]].to_csv(target)
        f.replace(done / f.name)
        n += 1
        if verbose:
            print(f"inbox: imported {f.name} ({len(parts)} day(s))")
    return n


class History:
    """Local store of whole-market daily tables (data/daily/*.csv) -> per-symbol OHLCV."""

    def __init__(self, client: PSXClient | None = None):
        DAILY.mkdir(parents=True, exist_ok=True)
        self._panel: pd.DataFrame | None = None

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
        if "close" not in p.columns:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"], dtype=float)
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
    table = None
    for tb in soup.find_all("table"):   # first table with SYMBOL + a price column
        hs = {th.get_text(" ", strip=True).lower() for th in tb.find_all("th")}
        if hs & {"symbol", "scrip"} and hs & {"current", "close", "ldcp"}:
            table = tb; break
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
    volume: float | None = None
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
    snap.volume = _num(stats.get("Volume"))
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
