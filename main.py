from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER DEEP WIN VS LOSS PATTERN DISCOVERY ===")

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
# 2. PATTERN DISCOVERY ENGINE (FINDING THE EXACT DIFFERENCE)
# -------------------------------------------------------------
def discover_winning_logic(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False
    )

    if df is None or df.empty or len(df) < 30:
      return []

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    trades = []
    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    resistance_level = 0
    max_price_reached = 0
    base_duration = 0
    breakout_range = 0
    base_avg_range = 0

    for i in range(min_consolidation_months * 2 + 3, n):
      current_date = df.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = df["Open"].iloc[i]
      curr_range = ranges[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          pnl_pct = (
              (take_profit - entry_price) / entry_price * 100
              if is_win
              else (stop_loss - entry_price) / entry_price * 100
          )
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": symbol,
                "Base_Months": base_duration,
                "Breakout_Range_%": round(breakout_range, 2),
                "Base_Avg_Range_%": round(base_avg_range, 2),
                "Range_Ratio": round(
                    breakout_range / (base_avg_range + 0.001), 2
                ),
                "Result": "WIN" if is_win else "LOSS",
            })
          in_trade = False
        continue

      if pending_entry:
        entry_price = curr_open
        stop_loss = entry_price * (1 - stop_loss_pct)
        take_profit = entry_price * (1 + take_profit_pct)
        entry_date = current_date
        max_price_reached = curr_high
        in_trade = True
        pending_entry = False
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

        active_resistance = highs[j]
        resistance_index = j
        break

      if active_resistance is not None and curr_close > active_resistance:
        post_swing_ranges = ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0:
          max_subsequent_range = post_swing_ranges.max()
          if curr_range <= max_subsequent_range:
            resistance_level = active_resistance
            base_duration = i - resistance_index
            base_avg_range = post_swing_ranges.mean()
            breakout_range = curr_range
            pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & PRINT COMPARATIVE METRICS
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = discover_winning_logic(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)

  print("\n==================================================")
  print(" DEEP PATTERN COMPARISON: WINS VS LOSSES")
  print("==================================================")
  wins_df = df_res[df_res["Result"] == "WIN"]
  loss_df = df_res[df_res["Result"] == "LOSS"]

  print(f"Total Wins Analysed  : {len(wins_df)}")
  print(f"Total Losses Analysed: {len(loss_df)}")
  print("--------------------------------------------------")
  print(
      "📊 Average Base Duration -> Wins:"
      f" {wins_df['Base_Months'].mean():.2f}m | Losses:"
      f" {loss_df['Base_Months'].mean():.2f}m"
  )
  print(
      "📊 Average Breakout Range -> Wins:"
      f" {wins_df['Breakout_Range_%'].mean():.2f}% | Losses:"
      f" {loss_df['Breakout_Range_%'].mean():.2f}%"
  )
  print(
      "📊 Average Base Range ----> Wins:"
      f" {wins_df['Base_Avg_Range_%'].mean():.2f}% | Losses:"
      f" {loss_df['Base_Avg_Range_%'].mean():.2f}%"
  )
  print(
      "📊 Range Ratio (Breakout/Base) -> Wins:"
      f" {wins_df['Range_Ratio'].mean():.2f} | Losses:"
      f" {loss_df['Range_Ratio'].mean():.2f}"
  )
  print("==================================================")

  try:
    ws = (
        sh.worksheet("LOGIC_DISCOVERY")
        if "LOGIC_DISCOVERY" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("LOGIC_DISCOVERY", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        "WIN VS LOSS LOGIC DISCOVERY REPORT |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Stock Symbol",
        "Base Months",
        "Breakout Range %",
        "Base Avg Range %",
        "Range Ratio",
        "Result",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print("✅ Detailed logic report saved to 'LOGIC_DISCOVERY' tab in sheet!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
  
