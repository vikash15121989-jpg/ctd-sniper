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
    "=== CTD_SNIPER MONTHLY OPEN STRATEGY BACKTEST (NO 2% FILTER + PEAK"
    " BEFORE SL) ==="
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


# -------------------------------------------------------------
# 2. MONTHLY BACKTEST ENGINE (NO 2% BUFFER + PEAK BEFORE SL)
# -------------------------------------------------------------
def backtest_monthly_no_2pct(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
    min_breakout_pct=0.0,
):
  trades = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty or len(df) < 30:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < min_consolidation_months * 2 + 4:
      return trades

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    in_trade = False
    pending_entry = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_date = None
    max_price_reached = 0.0

    for i in range(min_consolidation_months * 2 + 3, n):
      current_date = df.index[i]
      curr_close = float(closes[i])
      curr_high = float(highs[i])
      curr_low = float(lows[i])
      curr_open = float(opens[i])
      curr_range = float(ranges[i])

      if in_trade:
        # SL hit hone se pehle ka peak track karein
        if curr_low <= stop_loss:
          # Agar SL hit hone se pehle current candle ka high SL se upar tha
          peak_before_sl_pct = (
              ((max_price_reached - entry_price) / entry_price) * 100.0
          )
          max_profit_pct = peak_before_sl_pct
          pnl_pct = ((stop_loss - entry_price) / entry_price) * 100.0

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Peak_Before_SL_%": round(peak_before_sl_pct, 2),
                "Result": "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
          continue

        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_high >= take_profit:
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100.0
          pnl_pct = ((take_profit - entry_price) / entry_price) * 100.0

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Peak_Before_SL_%": "",
                "Result": "WIN",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_entry:
        entry_price = curr_open
        stop_loss = entry_price * (1.0 - stop_loss_pct)
        take_profit = entry_price * (1.0 + take_profit_pct)
        entry_date = current_date
        max_price_reached = curr_high
        in_trade = True
        pending_entry = False

        # Entry month mein hi SL/TP check
        if curr_low <= stop_loss:
          peak_before_sl_pct = (
              ((max_price_reached - entry_price) / entry_price) * 100.0
          )
          pnl_pct = ((stop_loss - entry_price) / entry_price) * 100.0
          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(peak_before_sl_pct, 2),
                "Peak_Before_SL_%": round(peak_before_sl_pct, 2),
                "Result": "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        elif curr_high >= take_profit:
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100.0
          pnl_pct = ((take_profit - entry_price) / entry_price) * 100.0
          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": symbol,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Peak_Before_SL_%": "",
                "Result": "WIN",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      # Find True Swing High
      active_resistance = None
      resistance_index = -1

      for j in range(
          i - min_consolidation_months, min_consolidation_months - 1, -1
      ):
        is_valid_swing = True
        for k in range(j - min_consolidation_months, j):
          if highs[j] <= highs[k]:
            is_valid_swing = False
            break
        if not is_valid_swing:
          continue

        for k in range(j + 1, j + min_consolidation_months + 1):
          if highs[j] <= highs[k]:
            is_valid_swing = False
            break
        if not is_valid_swing:
          continue

        ever_broken = False
        for k in range(j + 1, i):
          if highs[k] > highs[j]:
            ever_broken = True
            break
        if ever_broken:
          continue

        active_resistance = float(highs[j])
        resistance_index = j
        break

      # Breakout above Swing High (without strict >= 2% filter) + Post-Swing Range Filter
      if (
          active_resistance is not None
          and active_resistance > 0
          and curr_close > active_resistance * (1.0 + min_breakout_pct)
      ):
        post_swing_ranges = ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
          pending_entry = True

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_monthly_no_2pct(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  loss_df = df_res[df_res["Result"] == "LOSS"]
  losses = len(loss_df)
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  peak_vals = pd.to_numeric(loss_df["Peak_Before_SL_%"], errors="coerce").dropna()
  min_peak_before_sl = round(float(peak_vals.min()), 2) if len(peak_vals) > 0 else None
  avg_peak_before_sl = round(float(peak_vals.mean()), 2) if len(peak_vals) > 0 else None

  print(f"\n=======================================================")
  print(f" MONTHLY NO-2% BREAKOUT + RANGE RESULTS (2025-2026)")
  print(f"=======================================================")
  print(f" Total Trades       : {total}")
  print(f" Wins               : {wins} | Losses: {losses}")
  print(f" Win Rate           : {win_rate}%")
  print(f" Total PnL          : {total_pnl}%")
  print(f" Min Peak Before SL : {min_peak_before_sl}%")
  print(f" Avg Peak Before SL : {avg_peak_before_sl}%")
  print(f"=======================================================")

  tab_name = "MONTHLY_NO2PCT_RANGE_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 10)
    )
    ws.clear()
    ws.append_row([
        "MONTHLY NO-2% BREAKOUT + RANGE LOG (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Entry Date",
        "Entry Price",
        "Max Profit %",
        "Peak Before SL %",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
