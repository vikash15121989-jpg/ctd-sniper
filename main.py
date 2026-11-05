from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER 2025-2026 BACKTEST (SL 7% & TP 14%) ===")

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
# 2. BACKTEST ENGINE FOR 2025-2026 (SL 7% / TP 14%)
# -------------------------------------------------------------
def backtest_2025_2026_custom(
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

    for i in range(min_consolidation_months + 3, n):
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
                "Resistance": round(resistance_level, 2),
                "Breakout_Month": entry_date.strftime("%b %Y"),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN (14% TARGET)" if is_win else "LOSS (7% SL)",
                "Final_PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_entry:
        entry_price = curr_open
        stop_loss = entry_price * (1 - stop_loss_pct)
        take_profit = entry_price * (1 + take_profit_pct)  # Fixed 14% Target
        entry_date = current_date
        max_price_reached = curr_high
        in_trade = True
        pending_entry = False
        continue

      # Find Unbroken Swing High
      active_resistance = None
      resistance_index = -1

      for j in range(
          i - min_consolidation_months, min_consolidation_months, -1
      ):
        is_valid_swing = True
        for k in range(j - min_consolidation_months, j):
          if highs[j] <= highs[k]:
            is_valid_swing = False
            break

        if is_valid_swing:
          ever_broken = False
          for k in range(j + 1, i):
            if highs[k] > highs[j]:
              ever_broken = True
              break
          if not ever_broken:
            active_resistance = highs[j]
            resistance_index = j
            break

      # Breakout Condition + Range Filter
      if active_resistance is not None and curr_close > active_resistance:
        post_swing_ranges = ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0:
          max_subsequent_range = post_swing_ranges.max()
          if curr_range <= max_subsequent_range:
            resistance_level = active_resistance
            pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT SUMMARY
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_2025_2026_custom(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  wins = len(df_res[df_res["Result"].str.contains("WIN")])
  total = len(df_res)
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["Final_PnL_%"].sum(), 2)

  print(f"\n========================================")
  print(f" PERFORMANCE SUMMARY (2025-2026) [7% SL / 14% TP]")
  print(f"========================================")
  print(f" Total Trades : {total}")
  print(f" Winning %    : {win_rate}% ({wins} Wins / {total - wins} Losses)")
  print(f" Total PnL %  : {total_pnl}%")
  print(f"========================================")

  try:
    ws = (
        sh.worksheet("SUMMARY_7_14")
        if "SUMMARY_7_14" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("SUMMARY_7_14", 2000, 10)
    )
    ws.clear()
    ws.append_row([
        "2025-2026 BACKTEST (7% SL & 14% TP) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Stock Symbol",
        "Resistance",
        "Breakout Month",
        "Entry Price",
        "Max Profit %",
        "Result",
        "Final PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(
        "✅ Results successfully saved to 'SUMMARY_7_14' tab in Google Sheet!"
    )
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ 2025 aur 2026 ke dauran is rule par koi trade nahi mili.")
  
