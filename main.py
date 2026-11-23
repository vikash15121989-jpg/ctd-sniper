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
    "=== CTD_SNIPER MONTHLY SWING: 2% CLOSE -> NEXT MONTH LOW INTACT -> 3RD"
    " MONTH OPEN ENTRY ==="
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
# 2. BACKTEST LOGIC: BREAKOUT (i) -> LOW INTACT (i+1) -> ENTRY AT OPEN (i+2)
# -------------------------------------------------------------
def backtest_2m_confirm_entry(
    symbol,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
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

    if df is None or df.empty or len(df) < 40:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if "Volume" not in df.columns:
      df["Volume"] = 100000

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    dates = df.index
    n = len(df)

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values
    min_consolidation_months = 3

    in_trade = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    breakout_date = None
    entry_date = None
    resistance_level = 0.0
    breakout_candle_low = 0.0
    max_price_reached = 0.0

    # State machine for 2-month confirmation before entry:
    # waiting_confirm_idx = i + 1 (month after breakout i)
    # waiting_entry_idx   = i + 2 (month after confirmation i + 1)
    waiting_confirm_idx = None
    waiting_entry_idx = None

    start_idx = min_consolidation_months * 2 + 5

    # Need at least index n - 1, so loop up to n - 1
    for i in range(start_idx, n):
      current_date = dates[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
      curr_range = ranges[i]

      # A. If currently inside an open trade, monitor SL / TP
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

          if (
              (breakout_date and breakout_date.year in [2025, 2026])
              or (entry_date and entry_date.year in [2025, 2026])
              or (current_date.year in [2025, 2026])
          ):
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
            sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

            trades.append({
                "Symbol": symbol,
                "Breakout_Month": (
                    breakout_date.strftime("%b %Y") if breakout_date else ""
                ),
                "Entry_Month": (
                    entry_date.strftime("%b %Y") if entry_date else ""
                ),
                "Resistance": round(resistance_level, 2),
                "Breakout_Low": round(breakout_candle_low, 2),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
                "TradingView_Chart": sheet_link_formula,
            })

          in_trade = False
          waiting_confirm_idx = None
          waiting_entry_idx = None
        continue

      # B. Check confirmation month (e.g., March if breakout was in February)
      if waiting_confirm_idx is not None and i == waiting_confirm_idx:
        if curr_low >= breakout_candle_low:
          # Breakout candle's low remained intact in the confirmation month
          waiting_entry_idx = i + 1
        else:
          # Breakout low broke in the next month -> setup invalidated
          waiting_confirm_idx = None
          waiting_entry_idx = None
        continue

      # C. Check entry month (e.g., April if breakout was Feb and Mar low stayed intact)
      if waiting_entry_idx is not None and i == waiting_entry_idx:
        if i < n:
          entry_price = curr_open
          stop_loss = entry_price * (1 - stop_loss_pct)
          take_profit = entry_price * (1 + take_profit_pct)
          entry_date = current_date
          max_price_reached = curr_high
          in_trade = True
        waiting_confirm_idx = None
        waiting_entry_idx = None
        continue

      # D. Detect Major Swing Resistance & >= 2% Closing Breakout
      if waiting_confirm_idx is None and waiting_entry_idx is None:
        active_resistance = None
        resistance_index = -1
        search_range = range(
            i - min_consolidation_months, min_consolidation_months - 1, -1
        )

        for j in search_range:
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

        # Condition: Close >= Resistance * 1.02 (at least 2% above swing high)
        if (
            active_resistance is not None
            and curr_close > active_resistance
            and curr_close >= active_resistance * 1.02
        ):
          post_swing_ranges = ranges[resistance_index + 1 : i]
          if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
            if i + 2 < n:
              resistance_level = active_resistance
              breakout_candle_low = curr_low
              breakout_date = current_date
              waiting_confirm_idx = i + 1
              waiting_entry_idx = None

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  print("🚀 Running 2% Close -> Next Month Low Intact -> Entry Backtest...")
  for sym in STOCKS:
    t = backtest_2m_confirm_entry(sym)
    if t:
      all_trades.extend(t)

  if len(all_trades) > 0:
    df_res = pd.DataFrame(all_trades)
    wins = len(df_res[df_res["Result"] == "WIN"])
    total = len(df_res)
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0
    total_pnl = round(df_res["PnL_%"].sum(), 2)

    print(f"\n=======================================================")
    print(f" 2M CONFIRMATION MONTHLY SWING RESULTS (2025-2026)")
    print(f"=======================================================")
    print(f" Total Trades : {total}")
    print(f" Wins         : {wins} | Losses: {total - wins}")
    print(f" Win Rate     : {win_rate}%")
    print(f" Total PnL    : {total_pnl}%")
    print(f"=======================================================")

    tab_name = "BT_2M_CONFIRM_ENTRY_LOG"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 12)
      )
      ws.clear()
      ws.append_row([
          "2% CLOSE -> NEXT MONTH LOW INTACT -> 3RD MONTH OPEN ENTRY"
          f" (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws.append_row([
          f"Total Trades: {total} | Wins: {wins} | Win Rate: {win_rate}% |"
          f" Total PnL: {total_pnl}%"
      ])
      ws.append_row([])
      ws.append_row([
          "Symbol",
          "Breakout Month",
          "Entry Month",
          "Resistance",
          "Breakout Low",
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
    print("⚠️ Is criteria par koi trade nahi mili.")
else:
  print("⚠️ Watchlist is empty.")
  
