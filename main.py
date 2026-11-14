from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER STRICT STRUCTURAL SWING BACKTEST (HH-HL & LH-LL) ===")

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
# 2. BACKTEST ENGINE WITH STRICT STRUCTURAL SWING RULES
# -------------------------------------------------------------
def backtest_strict_structural(
    symbol,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  trades = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )

    if df is None or df.empty or len(df) < 40:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    dates = df.index
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values

    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    resistance_level = 0
    max_price_reached = 0

    start_idx = 10

    for i in range(start_idx, n):
      current_date = df.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
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
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
            sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

            trades.append({
                "Symbol": symbol,
                "Breakout_Month": entry_date.strftime("%b %Y"),
                "Resistance": round(resistance_level, 2),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
                "TradingView_Chart": sheet_link_formula,
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

      # STRICT STRUCTURAL SWING DETECTION
      active_resistance = None
      resistance_index = -1

      for j in range(i - 3, 2, -1):
        # Left 3 candles Higher High / Higher Low sequence check
        left_hh_hl = (
            highs[j - 3] < highs[j - 2]
            and highs[j - 2] < highs[j - 1]
            and highs[j - 1] < highs[j]
            and lows[j - 3] < lows[j - 2]
            and lows[j - 2] < lows[j - 1]
        )
        if not left_hh_hl:
          continue

        # Right 3 candles Lower High / Lower Low sequence check
        right_lh_ll = (
            j + 3 < i
            and highs[j] > highs[j + 1]
            and highs[j + 1] > highs[j + 2]
            and highs[j + 2] > highs[j + 3]
            and lows[j] > lows[j + 1]
            and lows[j + 1] > lows[j + 2]
            and lows[j + 2] > lows[j + 3]
        )
        if not right_lh_ll:
          continue

        # Check if resistance was ever broken before index i
        ever_broken = False
        for k in range(j + 4, i):
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
          if curr_range <= post_swing_ranges.max():
            resistance_level = active_resistance
            pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  print("🚀 Running Strict Structural Swing Backtest...")
  for sym in STOCKS:
    t = backtest_strict_structural(sym)
    if t:
      all_trades.extend(t)

  if len(all_trades) > 0:
    df_res = pd.DataFrame(all_trades)
    wins = len(df_res[df_res["Result"] == "WIN"])
    total = len(df_res)
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0
    total_pnl = round(df_res["PnL_%"].sum(), 2)

    print(f"\n========================================")
    print(f" STRICT STRUCTURAL RESULTS (2025-2026)")
    print(f"========================================")
    print(f" Total Trades : {total}")
    print(f" Wins         : {wins} | Losses: {total - wins}")
    print(f" Win Rate     : {win_rate}%")
    print(f" Total PnL    : {total_pnl}%")
    print(f"========================================")

    tab_name = "BT_STRUCTURAL_LOG"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 10)
      )
      ws.clear()
      ws.append_row([
          f"STRICT STRUCTURAL SWING BACKTEST (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws.append_row([
          f"Total Trades: {total} | Wins: {wins} | Win Rate: {win_rate}% | Total PnL: {total_pnl}%"
      ])
      ws.append_row([])
      ws.append_row([
          "Symbol",
          "Breakout Month",
          "Resistance",
          "Entry Price",
          "Max Profit %",
          "Result",
          "PnL %",
          "TradingView Link",
      ])
      ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
      print(f"✅ Saved to '{tab_name}' tab in Google Sheet successfully!")
    except Exception as e:
      print(f"Sheet update error: {e}")
  else:
    print("⚠️ Is strict structural criteria par koi trade nahi mili.")
else:
  print("⚠️ Watchlist is empty.")
  
