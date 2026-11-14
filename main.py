from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER 3M DOWNTREND WINNERS VS LOSERS PATTERN ANALYZER ===")

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
# 2. PATTERN ANALYSIS ENGINE FOR 3M DOWNTREND TRADES
# -------------------------------------------------------------
def analyze_3m_downtrend_patterns(
    symbol, stop_loss_pct=0.07, take_profit_pct=0.14
):
  trade_features = []
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty or len(df) < 12:
      return trade_features

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if "Volume" not in df.columns:
      df["Volume"] = 100000

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    volumes = df["Volume"].values
    dates = df.index
    n = len(df)

    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None
    trigger_high = 0
    max_price_reached = 0

    # Storing breakout candle metrics for the active trade
    breakout_vol = 0
    breakout_body_pct = 0
    breakout_upper_wick_pct = 0
    breakout_range_pct = 0

    for i in range(3, n):
      current_date = dates[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
      curr_close = closes[i]
      curr_vol = volumes[i]

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trade_features.append({
                "Symbol": symbol,
                "Result": "WIN" if is_win else "LOSS",
                "Volume_Ratio": round(breakout_vol, 2),
                "Body_Pct": round(breakout_body_pct, 2),
                "Upper_Wick_Pct": round(breakout_upper_wick_pct, 2),
                "Range_Pct": round(breakout_range_pct, 2),
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

      # --- 3M DOWNTREND RULE ---
      is_3m_lower_highs = (
          (highs[i - 3] > highs[i - 2])
          and (highs[i - 2] > highs[i - 1])
      )
      is_3m_lower_lows = (
          (lows[i - 3] > lows[i - 2])
          and (lows[i - 2] > lows[i - 1])
      )

      if is_3m_lower_highs and is_3m_lower_lows:
        prev_high = highs[i - 1]
        prev_low = lows[i - 1]

        if curr_low >= prev_low and curr_high > prev_high:
          trigger_high = prev_high

          # Calculate Breakout Candle Metrics for Pattern Analysis
          avg_vol_3m = np.mean(volumes[i - 3 : i])
          breakout_vol = (curr_vol / avg_vol_3m) if avg_vol_3m > 0 else 1.0

          candle_range = curr_high - curr_low
          body_size = abs(curr_close - curr_open)
          breakout_body_pct = (
              (body_size / candle_range) * 100 if candle_range > 0 else 0
          )

          upper_wick = curr_high - max(curr_open, curr_close)
          breakout_upper_wick_pct = (
              (upper_wick / candle_range) * 100 if candle_range > 0 else 0
          )

          breakout_range_pct = (
              (candle_range / curr_close) * 100 if curr_close > 0 else 0
          )

          pending_entry = True

    return trade_features

  except Exception as e:
    return []


# -------------------------------------------------------------
# 3. RUN, COMPARE & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
all_features = []
if len(STOCKS) > 0:
  print("🚀 Analyzing Winners vs Losers Patterns for 3M Downtrend...")
  for sym in STOCKS:
    res = analyze_3m_downtrend_patterns(sym)
    if res:
      all_features.extend(res)

  if len(all_features) > 0:
    df_all = pd.DataFrame(all_features)
    wins_df = df_all[df_all["Result"] == "WIN"]
    losses_df = df_all[df_all["Result"] == "LOSS"]

    total_trades = len(df_all)
    total_wins = len(wins_df)
    total_losses = len(losses_df)
    win_rate = round((total_wins / total_trades) * 100, 2)

    # Averages comparison
    win_vol = round(wins_df["Volume_Ratio"].mean(), 2)
    loss_vol = round(losses_df["Volume_Ratio"].mean(), 2)

    win_body = round(wins_df["Body_Pct"].mean(), 2)
    loss_body = round(losses_df["Body_Pct"].mean(), 2)

    win_wick = round(wins_df["Upper_Wick_Pct"].mean(), 2)
    loss_wick = round(losses_df["Upper_Wick_Pct"].mean(), 2)

    win_range = round(wins_df["Range_Pct"].mean(), 2)
    loss_range = round(losses_df["Range_Pct"].mean(), 2)

    print(f"\n========================================")
    print(f" WINNERS VS LOSERS PATTERN COMPARISON")
    print(f"========================================")
    print(f" Total Trades Analyzed : {total_trades}")
    print(f" Wins : {total_wins} | Losses : {total_losses}")
    print(f" Baseline Win Rate     : {win_rate}%")
    print(f"----------------------------------------")
    print(f" 🟢 WINNERS AVERAGE PATTERN:")
    print(f"    - Volume Ratio   : {win_vol}x")
    print(f"    - Candle Body %  : {win_body}%")
    print(f"    - Upper Wick %   : {win_wick}%")
    print(f"    - Candle Range % : {win_range}%")
    print(f"----------------------------------------")
    print(f" 🔴 LOSERS AVERAGE PATTERN:")
    print(f"    - Volume Ratio   : {loss_vol}x")
    print(f"    - Candle Body %  : {loss_body}%")
    print(f"    - Upper Wick %   : {loss_wick}%")
    print(f"    - Candle Range % : {loss_range}%")
    print(f"========================================")

    tab_name = "BT_3M_PATTERN_ANALYSIS"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 10)
      )
      ws.clear()
      ws.append_row([
          f"3M DOWNTREND PATTERN ANALYSIS | {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws.append_row([
          f"Total Trades: {total_trades} | Wins: {total_wins} | Losses: {total_losses} | Win Rate: {win_rate}%"
      ])
      ws.append_row([])
      ws.append_row([
          "Metric",
          "Winners Average",
          "Losers Average",
          "What to Filter Out / Keep",
      ])
      ws.append_row([
          "Volume Ratio",
          win_vol,
          loss_vol,
          "Keep higher volume ratio",
      ])
      ws.append_row([
          "Candle Body %",
          win_body,
          loss_body,
          "Keep strong body candles",
      ])
      ws.append_row([
          "Upper Wick %",
          win_wick,
          loss_wick,
          "Filter out high upper wick rejection",
      ])
      ws.append_row([
          "Candle Range %",
          win_range,
          loss_range,
          "Monitor range size",
      ])
      ws.append_row([])
      ws.append_row([
          "Symbol",
          "Result",
          "Volume Ratio",
          "Body %",
          "Upper Wick %",
          "Range %",
      ])
      ws.append_rows(df_all.values.tolist(), value_input_option="USER_ENTERED")
      print(f"✅ Saved Pattern Analysis to '{tab_name}' tab successfully!")
    except Exception as e:
      print(f"Sheet update error: {e}")
  else:
    print("⚠️ Koi trade data nahi mila.")
else:
  print("⚠️ Watchlist is empty.")
  
