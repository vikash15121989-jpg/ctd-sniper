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
    "=== CTD_SNIPER MONTHLY SWING -> DAILY ULTRA-VOL BREAKOUT -> NEXT-DAY"
    " ENTRY ==="
)

# Parameters you can tune easily
MIN_CONSOLIDATION_MONTHS = 3
VOL_LOOKBACK_DAYS = 20
ULTRA_VOL_MULT = 2.5  # e.g., 2.5x of 20-day average daily volume
STOP_LOSS_PCT = 0.07
TAKE_PROFIT_PCT = 0.14

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


# -------------------------------------------------------------
# 2. FIND VALID UNBROKEN MONTHLY SWING HIGH BEFORE INDEX `idx`
# -------------------------------------------------------------
def get_monthly_swing_high(highs, idx, min_consolidation_months=3):
  start_j = min(idx - min_consolidation_months, len(highs) - 1)
  end_j = min_consolidation_months - 1

  for j in range(start_j, end_j, -1):
    is_valid_swing = True
    for k in range(j - min_consolidation_months, j):
      if highs[j] <= highs[k]:
        is_valid_swing = False
        break
    if not is_valid_swing:
      continue

    for k in range(j + 1, j + min_consolidation_months + 1):
      if k < len(highs) and highs[j] <= highs[k]:
        is_valid_swing = False
        break
    if not is_valid_swing:
      continue

    ever_broken = False
    for k in range(j + 1, idx):
      if highs[k] > highs[j]:
        ever_broken = True
        break
    if ever_broken:
      continue

    return float(highs[j]), j

  return None, -1


def build_monthly_resistance_map(df_monthly, min_consolidation_months=3):
  """Maps each 'YYYY-MM' to the active swing high available entering that month."""
  res_map = {}
  highs = df_monthly["High"].values
  m_len = len(df_monthly)

  for m_idx in range(min_consolidation_months * 2 + 2, m_len):
    ym = df_monthly.index[m_idx].strftime("%Y-%m")
    swing_lvl, swing_idx = get_monthly_swing_high(
        highs, m_idx, min_consolidation_months
    )
    if swing_lvl is not None and swing_lvl > 0:
      res_map[ym] = (swing_lvl, swing_idx)
  return res_map


# -------------------------------------------------------------
# 3. BACKTEST ENGINE: MONTHLY SWING -> DAILY ULTRA-VOL -> NEXT DAY OPEN
# -------------------------------------------------------------
def backtest_monthly_swing_daily_vol(
    symbol,
    min_consolidation_months=MIN_CONSOLIDATION_MONTHS,
    vol_lookback=VOL_LOOKBACK_DAYS,
    ultra_vol_mult=ULTRA_VOL_MULT,
    stop_loss_pct=STOP_LOSS_PCT,
    take_profit_pct=TAKE_PROFIT_PCT,
):
  trades = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol

    df_m = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )
    df_d = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )

    if df_m is None or df_m.empty or df_d is None or df_d.empty:
      return trades

    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(
        subset=["Open", "High", "Low", "Close", "Volume"], inplace=True
    )

    if (
        len(df_m) < min_consolidation_months * 2 + 4
        or len(df_d) < vol_lookback + 10
    ):
      return trades

    res_map = build_monthly_resistance_map(df_m, min_consolidation_months)
    if not res_map:
      return trades

    opens = df_d["Open"].values
    highs = df_d["High"].values
    lows = df_d["Low"].values
    closes = df_d["Close"].values
    vols = df_d["Volume"].values.astype(float)
    dates = df_d.index
    n = len(df_d)

    # Prior 20-day volume average (excluding current day `i`)
    vol_series = pd.Series(vols)
    avg_vol_20 = vol_series.shift(1).rolling(vol_lookback).mean().values

    in_trade = False
    pending_entry = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_date = None
    breakout_date_str = ""
    breakout_vol_ratio = 0.0
    swing_lvl_used = 0.0
    max_price_reached = 0.0
    used_swing_keys = set()

    for i in range(vol_lookback + 1, n):
      curr_dt = dates[i]
      curr_open = float(opens[i])
      curr_high = float(highs[i])
      curr_low = float(lows[i])
      curr_close = float(closes[i])
      curr_vol = float(vols[i])
      prev_avg_vol = avg_vol_20[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          pnl_pct = (
              ((take_profit - entry_price) / entry_price) * 100.0
              if is_win
              else ((stop_loss - entry_price) / entry_price) * 100.0
          )
          max_profit_pct = (
              ((max_price_reached - entry_price) / entry_price) * 100.0
          )

          if entry_date.year in [2025, 2026] or curr_dt.year in [2025, 2026]:
            trades.append({
                "Symbol": symbol,
                "Breakout_Date": breakout_date_str,
                "Entry_Date": entry_date.strftime("%Y-%m-%d"),
                "Swing_High": round(swing_lvl_used, 2),
                "Entry_Price": round(entry_price, 2),
                "Vol_Ratio": round(breakout_vol_ratio, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_entry:
        if curr_open <= 0:
          pending_entry = False
          continue
        entry_price = curr_open
        stop_loss = entry_price * (1.0 - stop_loss_pct)
        take_profit = entry_price * (1.0 + take_profit_pct)
        entry_date = curr_dt
        max_price_reached = curr_high
        in_trade = True
        pending_entry = False
        continue

      ym_key = curr_dt.strftime("%Y-%m")
      if ym_key not in res_map:
        continue

      swing_lvl, swing_idx = res_map[ym_key]
      swing_tag = f"{swing_idx}_{round(swing_lvl, 4)}"

      if (
          swing_tag not in used_swing_keys
          and prev_avg_vol is not None
          and not np.isnan(prev_avg_vol)
          and prev_avg_vol > 0
      ):
        vol_ratio = curr_vol / prev_avg_vol
        if curr_close > swing_lvl and vol_ratio >= ultra_vol_mult:
          pending_entry = True
          breakout_date_str = curr_dt.strftime("%Y-%m-%d")
          breakout_vol_ratio = vol_ratio
          swing_lvl_used = swing_lvl
          used_swing_keys.add(swing_tag)

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 4. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_monthly_swing_daily_vol(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print(f"\n=======================================================")
  print(f" MONTHLY SWING -> DAILY ULTRA-VOL RESULTS (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "MSWING_DVOL_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 12)
    )
    ws.clear()
    ws.append_row([
        "MONTHLY SWING -> DAILY ULTRA-VOL LOG (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Breakout Date",
        "Entry Date",
        "Swing High",
        "Entry Price",
        "Vol Ratio",
        "Max Profit %",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
