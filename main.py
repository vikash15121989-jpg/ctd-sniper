from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER HIGHER CLOSE / BREAKOUT BACKTEST ===")

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
# 2. BACKTEST ENGINE FOR BREAKOUT STRATEGY
# -------------------------------------------------------------
def backtest_breakout_strategy(
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

    if df is None or df.empty or len(df) < 20:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    n = len(df)

    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    trigger_ref = 0
    max_price_reached = 0

    start_idx = 5  # Need buffer for previous candles check

    for i in range(start_idx, n):
      current_date = df.index[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]

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
                "Trigger_High": round(trigger_ref, 2),
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

      # LOGIC CHECK:
      # 1. Previous month(s) were making lower lows and lower highs (downtrend / contraction context)
      # 2. Current month: Low did NOT break previous month's low (lows[i] > lows[i-1])
      # 3. Current month: Close broke above previous month's high (closes[i] > highs[i-1])
      prev_high = highs[i - 1]
      prev_low = lows[i - 1]

      is_downtrend_context = (highs[i - 2] < highs[i - 1]) or (
          lows[i - 2] < lows[i - 1]
      )  # Optional context check or strict breakdown check

      # Strict condition as requested:
      # "pichle mahine ka low break nahi hua, pichle mahine ka high break karke close diya"
      if curr_low >= prev_low and curr_close > prev_high:
        trigger_ref = prev_high
        pending_entry = True

    return trades

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  print("🚀 Running Breakout Reversal Backtest...")
  for sym in STOCKS:
    t = backtest_breakout_strategy(sym)
    if t:
      all_trades.extend(t)

  if len(all_trades) > 0:
    df_res = pd.DataFrame(all_trades)
    wins = len(df_res[df_res["Result"] == "WIN"])
    total = len(df_res)
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0
    total_pnl = round(df_res["PnL_%"].sum(), 2)

    print(f"\n========================================")
    print(f" BREAKOUT STRATEGY RESULTS (2025-2026)")
    print(f"========================================")
    print(f" Total Trades : {total}")
    print(f" Wins         : {wins} | Losses: {total - wins}")
    print(f" Win Rate     : {win_rate}%")
    print(f" Total PnL    : {total_pnl}%")
    print(f"========================================")

    tab_name = "BT_BREAKOUT_LOG"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 10)
      )
      ws.clear()
      ws.append_row([
          f"BREAKOUT REVERSAL BACKTEST (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws.append_row([
          f"Total Trades: {total} | Wins: {wins} | Win Rate: {win_rate}% | Total PnL: {total_pnl}%"
      ])
      ws.append_row([])
      ws.append_row([
          "Symbol",
          "Breakout Month",
          "Prev High Ref",
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
    print("⚠️ Is breakout criteria par koi trade nahi mili.")
else:
  print("⚠️ Watchlist is empty.")
  
