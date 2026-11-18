# PSX Blue-Chip Monitor

Swing (3-10 day) and investment alerts for KSE-100 stocks, delivered to WhatsApp.

## Why it must run in Pakistan
The PSX Data Portal (`dps.psx.com.pk`) serves price data (`/timeseries`, `/indices`,
`/market-watch`) only to Pakistani IPs. Company pages (P/E, EPS, 52-week range,
announcements) work from anywhere. So the monitor runs on your PC (or a Pakistani VPS).

## What it sends (Mon-Fri, PKT)
| Time | Job | Content |
|---|---|---|
| 09:15 | `preopen` | macro lines + stocks within 3% of a 20-day-high breakout trigger |
| 09:45-15:15, every 30 min | `intraday` | only NEW triggers: live breakout on volume pace, +/-4% moves, your price zones |
| 15:50 | `close` | full KSE-100 scan: swing table (entry / stop / target), value flags, corporate events |

## Rules (defaults in `config.yaml`; validate with the backtest before trusting them)
- **Trend filter:** close > 50DMA > 200DMA. **Liquidity:** 20-day avg traded value >= Rs 50M.
- **BREAKOUT:** close above prior 20-day high on >= 1.5x 20-day avg volume.
- **PULLBACK:** RSI(2) <= 10 while above 200DMA (mean reversion, exits on close > 5DMA).
- **Risk:** stop 1.5x ATR, target 2R, time exit 10 sessions. PSX EOD has no high/low, so ATR uses an open/close proxy.
- **Exit flags:** RSI(14) >= 75, close below 50DMA on volume.
- **Investment:** P/E < 0.8x universe median, near 52-week low, your accumulate/trim zones;
  flags when the latest quarterly EPS is below the year-ago quarter, plus inventory-gain and one-off caveats for OMCs and MARI.

## Setup (Windows)
```
git clone https://github.com/aktrc291/ak.git C:\psx-monitor
cd C:\psx-monitor
pip install -r requirements.txt
copy .env.example .env      # then fill in CallMeBot phone + key
python -m psx_monitor test-whatsapp
python -m psx_monitor backtest --start 2023-11-01
python -m psx_monitor close --dry-run --force
powershell -ExecutionPolicy Bypass -File scripts\install_windows_tasks.ps1
```
Keep `data/macro.txt` updated (3 lines), or let a Claude scheduled task refresh it.

## Broker (Intermarket Securities / IMS)
Not used. IMS has no public read-only market-data API, and the monitor must never hold
your trading login. PSX's own portal gives the same exchange prices.

## Not investment advice
These are rule-based screens. Confirm them with your own analysis before you trade.
