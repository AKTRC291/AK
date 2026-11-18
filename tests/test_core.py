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
