from pathlib import Path
import numpy as np, pandas as pd, yaml
from psx_monitor.data import parse_company, eod_from_json
from psx_monitor.indicators import add_all, rsi
from psx_monitor.signals import swing_signals, investment_signals
from psx_monitor.backtest import backtest_symbol, summarize

ROOT = Path(__file__).resolve().parent.parent
C = yaml.safe_load((ROOT / "config.yaml").read_text())

def test_company_page_parse():
    s = parse_company("LUCK", (ROOT / "tests/fixtures/company_LUCK.html").read_text(encoding="utf8"))
    assert s.name == "Lucky Cement Limited" and s.price == 433.90
    assert s.pe_ttm == 12.94 and (s.low_52w, s.high_52w) == (339.0, 529.5)
    assert s.annual_eps["2026"] == 31.83 and s.quarterly_eps["Q1 2027"] == 11.69
    assert s.free_float_pct == 30.0 and s.shares == 1_465_000_000
    assert any("Financial Results" in t for _, t in s.announcements)

def test_eod_json():
    j = {"data": [[1731888000, 101.0, 5000, 100.0], [1731801600, 100.0, 4000, 99.0]]}
    df = eod_from_json(j)
    assert list(df.columns) == ["open", "close", "volume"] and df.index.is_monotonic_increasing

def test_rsi_bounds():
    s = pd.Series(np.cumsum(np.random.default_rng(1).normal(0, 1, 300)) + 100)
    r = rsi(s, 14).dropna()
    assert r.between(0, 100).all()

def synthetic(n=400, seed=0):
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.018, n)))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.004, n))
    v = rng.integers(400_000, 1_200_000, n).astype(float)
    v[-1] = 3_000_000; c[-1] = c[-25:-1].max() * 1.03  # force a breakout on the last bar
    idx = pd.bdate_range("2025-01-01", periods=n)
    return pd.DataFrame({"open": o, "close": c, "volume": v}, index=idx)

def test_signals_and_backtest_run():
    df = synthetic()
    sigs = swing_signals("TEST", add_all(df, C["swing"]), C["swing"], C["universe"])
    assert all(x.stop < x.entry < x.target for x in sigs if x.entry)
    t = pd.DataFrame(backtest_symbol("TEST", df, C["swing"], C["universe"]))
    if not t.empty:
        assert (t.days <= C["swing"]["max_hold_days"]).all()
        assert "ALL" in summarize(t).index

def test_zone_signal():
    out = investment_signals("AIRLINK", 103, None, C["investment"], None)
    assert out and "accumulate" in out[0].note

HIST_HTML = """<table id="historicalTable"><thead><tr><th>SYMBOL</th><th>LDCP</th><th>OPEN</th><th>HIGH</th><th>LOW</th>
<th>CLOSE</th><th>CHANGE</th><th>CHANGE (%)</th><th>VOLUME</th></tr></thead><tbody>
<tr data-type="equity"><td data-value="LUCK"><a href="/company/LUCK">LUCK</a></td><td>439.91</td><td>439.98</td><td>442.00</td>
<td>430.02</td><td>433.90</td><td>-6.01</td><td>(1.37%)</td><td>894,411</td></tr>
<tr data-type="debt"><td data-value="P01GIS">P01GIS</td><td>1</td><td>1</td><td>1</td><td>1</td><td>1</td><td>0</td><td>0</td><td>0</td></tr>
</tbody></table>"""

def test_price_table_and_history(tmp_path, monkeypatch):
    from psx_monitor import data as d
    df = d.parse_price_table(HIST_HTML)
    assert list(df.index) == ["LUCK"] and df.at["LUCK", "high"] == 442.0 and df.at["LUCK", "volume"] == 894411
    monkeypatch.setattr(d, "DAILY", tmp_path)
    for day in pd.bdate_range("2026-10-01", "2026-10-31"):
        df.to_csv(tmp_path / f"{day:%Y-%m-%d}.csv")
    s = d.History().symbol("LUCK")
    assert len(s) > 15 and set(s.columns) == {"open", "high", "low", "close", "volume"}

def test_inbox_import(tmp_path, monkeypatch):
    from psx_monitor import data as d
    monkeypatch.setattr(d, "DAILY", tmp_path / "daily")
    inbox = tmp_path / "inbox"; inbox.mkdir()
    # whole-market day file, terminal-style headers, date in filename
    (inbox / "marketwatch_2026-11-17.csv").write_text("Scrip,Open,High,Low,LTP,Volume\nLUCK,439.98,442,430.02,433.90,\"894,411\"\nMARI,600,610,595,605,120000\n")
    # per-symbol history file, dd/mm/yyyy dates
    (inbox / "PSO.csv").write_text("Date,Open,High,Low,Close,Volume\n14/11/2026,360,365,358,362,500000\n17/11/2026,362,366,360,363.7,450000\n")
    assert d.import_inbox(inbox, verbose=False) == 2
    day = pd.read_csv(tmp_path / "daily" / "2026-11-17.csv", index_col=0)
    assert set(day.index) == {"LUCK", "MARI", "PSO"} and day.at["LUCK", "volume"] == 894411
    assert (tmp_path / "daily" / "2026-11-14.csv").exists() and (inbox / "processed" / "PSO.csv").exists()


def test_value_trap_excluded():
    from psx_monitor.data import CompanySnapshot
    trap = CompanySnapshot("X", pe_ttm=3.0, quarterly_eps={"Q1 2027": 1.0, "Q3 2026": 1, "Q2 2026": 1, "Q1 2026": 2.0})
    good = CompanySnapshot("Y", pe_ttm=3.0, low_52w=95, quarterly_eps={"Q1 2027": 2.5, "Q3 2026": 1, "Q2 2026": 1, "Q1 2026": 2.0})
    assert investment_signals("X", 100, trap, C["investment"], 8.0) == []
    v = investment_signals("Y", 100, good, C["investment"], 8.0)
    assert v and "+25% YoY" in v[0].note and v[0].score > 0.6

def test_wide_workbook(tmp_path):
    from psx_monitor import data as d
    import datetime as dt
    days = [dt.datetime(2026, 11, 13), dt.datetime(2026, 11, 14), dt.datetime(2026, 11, 16)]  # 14th = Saturday
    def sheet(field, vals):
        rows = [[None, 1, 2], [None, "AAA", "BBB"], [None, field, field]]
        rows += [[day] + v for day, v in zip(days, vals)]
        return pd.DataFrame(rows)
    close = [[100, 50], [100, 50], [20, 51]]          # AAA 1:5 split on the 16th
    adj = [[20, 49], [20, 49], [20, 51]]              # back-adjusted, = close on last day
    vol = [[1000, 500], [None, None], [5000, 600]]    # Saturday has no volume
    f = tmp_path / "wb.xlsx"
    with pd.ExcelWriter(f) as w:
        for name, v in [("OPEN", close), ("HIGH", close), ("LOW", close), ("UNADJUSTED PRICE", close),
                        ("VOLUME", vol), ("Total Return Price", adj)]:
            sheet(name, v).to_excel(w, sheet_name=name, header=False, index=False)
    assert d.is_wide_workbook(pd.ExcelFile(f).sheet_names)
    long = d.parse_wide_workbook(f)
    assert long["date"].nunique() == 2                               # weekend dropped
    a = long[long.symbol == "AAA"].set_index("date")["close"]
    assert list(a.round(6)) == [20.0, 20.0]                          # split removed
