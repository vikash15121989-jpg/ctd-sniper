from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print(
    "=== CTD_SNIPER MONTHLY SWING -> DAILY DEMAND ZONE (2025-2026 ONLY) ==="
)

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & WATCHLIST LOAD
# -------------------------------------------------------------
try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = [
      s.strip().upper()
      for s in raw
      if s and len(s.strip()) > 2 and s.upper() not in ["STOCK", "SYMBOL"]
  ]
  print(f"✅ Loaded {len(STOCKS)} stocks from Watchlist.")
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


def get_monthly_swing_highs(df_m, lookback=3):
  """Returns list of dicts with swing_date, level, break_month_start."""
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  swings = []
  n = len(df_m)

  for j in range(lookback, n - lookback):
    val = float(highs[j])
    left_ok = all(val > float(highs[k]) for k in range(j - lookback, j))
    right_ok = all(
        val > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    )
    if not (left_ok and right_ok):
      continue

    break_idx = None
    for k in range(j + 1, n):
      if float(closes[k]) > val:
        break_idx = k
        break

    if break_idx is not None:
      swings.append({
          "swing_date": dates[j],
          "level": val,
          "break_month_start": dates[break_idx],
      })
  return swings


def find_post_break_demand_zone(df_d, start_dt, level):
  """
    After daily price breaks above `level` from `start_dt` onward,
    finds a clean base candle followed by an impulsive bullish leg-out candle.
    Returns (dz_low, dz_high, zone_formed_pos_in_sub) or None.
    """
  sub = df_d[df_d.index >= start_dt]
  if len(sub) < 6:
    return None

  opens = sub["Open"].values
  highs = sub["High"].values
  lows = sub["Low"].values
  closes = sub["Close"].values

  if not any(float(closes[k]) > level for k in range(len(sub))):
    return None

  for i in range(1, len(sub) - 2):
    if float(closes[i]) <= level:
      continue

    body_base = abs(float(closes[i]) - float(opens[i]))
    range_base = max(float(highs[i]) - float(lows[i]), 1e-9)
    next_open = float(opens[i + 1])
    next_close = float(closes[i + 1])
    next_body = next_close - next_open

    if (
        body_base <= 0.55 * range_base
        and next_close > next_open
        and next_body > 1.5 * max(body_base, 1e-9)
    ):
      dz_low = float(lows[i])
      dz_high = max(float(opens[i]), float(closes[i]), float(highs[i]))
      return dz_low, dz_high, i + 1

  return None


def backtest_m_to_d_dz_25_26(symbol, sl_pct=0.07, tp_pct=0.14):
  trades = []
  try:
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df_m = yf.download(
        ticker, start="2015-01-01", interval="1mo", auto_adjust=False, progress=False
    )
    df_d = yf.download(
        ticker, start="2015-01-01", interval="1d", auto_adjust=False, progress=False
    )

    if df_m is None or df_d is None or df_m.empty or df_d.empty:
      return trades
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

    swings = get_monthly_swing_highs(df_m, lookback=3)
    used_zones = set()

    for sw in swings:
      dz = find_post_break_demand_zone(df_d, sw["break_month_start"], sw["level"])
      if dz is None:
        continue

      dz_low, dz_high, formed_pos = dz
      zone_key = (round(dz_low, 2), round(dz_high, 2))
      if zone_key in used_zones:
        continue

      sub = df_d[df_d.index >= sw["break_month_start"]]
      opens = sub["Open"].values
      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      dates = sub.index

      in_trade = False
      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None

      for k in range(formed_pos + 1, len(sub)):
        d_open = float(opens[k])
        d_high = float(highs[k])
        d_low = float(lows[k])
        d_close = float(closes[k])
        curr_dt = dates[k]

        if in_trade:
          # Conservative SL check first
          if d_low <= sl_price:
            pnl = ((sl_price - entry_price) / entry_price) * 100.0
            if entry_dt.year in [2025, 2026] or curr_dt.year in [2025, 2026]:
              trades.append({
                  "Symbol": symbol,
                  "Monthly_Swing": round(sw["level"], 2),
                  "DZ_Range": f"{round(dz_low,2)}-{round(dz_high,2)}",
                  "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
                  "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
                  "Entry_Price": round(entry_price, 2),
                  "Result": "LOSS",
                  "PnL_%": round(pnl, 2),
              })
            in_trade = False
            break

          if d_high >= tp_price:
            pnl = ((tp_price - entry_price) / entry_price) * 100.0
            if entry_dt.year in [2025, 2026] or curr_dt.year in [2025, 2026]:
              trades.append({
                  "Symbol": symbol,
                  "Monthly_Swing": round(sw["level"], 2),
                  "DZ_Range": f"{round(dz_low,2)}-{round(dz_high,2)}",
                  "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
                  "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
                  "Entry_Price": round(entry_price, 2),
                  "Result": "WIN",
                  "PnL_%": round(pnl, 2),
              })
            in_trade = False
            break
          continue

        touched_dz = (d_low <= dz_high) and (d_high >= dz_low)
        bullish_candle = d_close > d_open
        if touched_dz and bullish_candle:
          used_zones.add(zone_key)
          entry_price = d_close
          sl_price = min(dz_low * 0.99, entry_price * (1.0 - sl_pct))
          tp_price = entry_price * (1.0 + tp_pct)
          entry_dt = curr_dt
          in_trade = True

    return trades
  except Exception:
    return []


# -------------------------------------------------------------
# 2. RUN 2025-2026 FILTERED BACKTEST & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
for sym in STOCKS:
  t = backtest_m_to_d_dz_25_26(sym)
  if t:
    all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  losses = len(df_res[df_res["Result"] == "LOSS"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print("\n=======================================================")
  print(" MONTHLY SWING -> DAILY DEMAND ZONE RESULTS (2025-2026)")
  print("=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("=======================================================")

  tab_name = "MONTHLY_TO_DAILY_DZ_25_26"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 10)
    )
    ws.clear()
    ws.append_row([
        "MONTHLY SWING -> DAILY DZ BACKTEST (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Monthly Swing",
        "DZ Range",
        "Entry Date",
        "Exit Date",
        "Entry Price",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ 2025-2026 mein koi trade nahi mili.")
  
