from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER WINNERS VS LOSERS PATTERN ANALYZER ===")

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
# 2. DEEP PATTERN ANALYSIS BACKTEST ENGINE
# -------------------------------------------------------------
def analyze_trade_patterns(symbol, stop_loss_pct=0.07, take_profit_pct=0.14):
  trades_data = []
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
      return trades_data

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

    df["Range_Pct"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Pct"].values
    min_consolidation_months = 3

    for i in range(min_consolidation_months * 2 + 5, n):
      current_date = dates[i]
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_open = opens[i]
      curr_range = ranges[i]
      curr_vol = volumes[i]

      # Major Swing Resistance Detection
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

      if active_resistance is not None and curr_close > active_resistance:
        post_swing_ranges = ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
          
          # Calculate metrics for pattern study
          avg_vol_3m = np.mean(volumes[i - 3 : i])
          vol_ratio = round(curr_vol / avg_vol_3m, 2) if avg_vol_3m > 0 else 1.0
          
          body_size = abs(curr_close - curr_open)
          total_candle_range = curr_high - curr_low
          body_ratio = round((body_size / total_candle_range) * 100, 1) if total_candle_range > 0 else 0

          # Simulate Trade Outcome
          entry_price = curr_open
          stop_loss = entry_price * (1 - stop_loss_pct)
          take_profit = entry_price * (1 + take_profit_pct)
          max_price = curr_high
          trade_result = None
          pnl = 0

          for f_idx in range(i + 1, n):
            f_high = highs[f_idx]
            f_low = lows[f_idx]
            if f_high > max_price:
              max_price = f_high
            if f_low <= stop_loss or f_high >= take_profit:
              is_win = f_high >= take_profit
              pnl = (
                  ((take_profit - entry_price) / entry_price * 100)
                  if is_win
                  else ((stop_loss - entry_price) / entry_price * 100)
              )
              trade_result = "WIN" if is_win else "LOSS"
              break

          if trade_result and current_date.year in [2025, 2026]:
            trades_data.append({
                "Symbol": symbol,
                "Date": current_date.strftime("%b %Y"),
                "Result": trade_result,
                "Volume_Ratio": vol_ratio,
                "Candle_Body_%": body_ratio,
                "Breakout_Range_%": round(curr_range, 2),
                "PnL_%": round(pnl, 2)
            })

  except Exception as e:
    pass
  return trades_data


# -------------------------------------------------------------
# 3. EXECUTE & COMPARE PATTERNS
# -------------------------------------------------------------
all_analyzed_trades = []
if len(STOCKS) > 0:
  print("🔍 Analyzing Winners vs Losers patterns...")
  for sym in STOCKS:
    t_list = analyze_trade_patterns(sym)
    if t_list:
      all_analyzed_trades.extend(t_list)

  if len(all_analyzed_trades) > 0:
    df_all = pd.DataFrame(all_analyzed_trades)
    df_wins = df_all[df_all["Result"] == "WIN"]
    df_losses = df_all[df_all["Result"] == "LOSS"]

    print("\n==================================================")
    print(" 🔬 WINNERS VS LOSERS PATTERN INSIGHTS (2025-2026)")
    print("==================================================")
    print(f" Total Trades Analyzed : {len(df_all)}")
    print(f" Total Wins            : {len(df_wins)}")
    print(f" Total Losses          : {len(df_losses)}")
    print("--------------------------------------------------")
    print(f" 🟢 WINNERS AVERAGE PATTERN:")
    print(f"    - Avg Volume Surge Ratio : {df_wins['Volume_Ratio'].mean():.2f}x")
    print(f"    - Avg Candle Body %      : {df_wins['Candle_Body_%'].mean():.1f}%")
    print(f"    - Avg Breakout Range %   : {df_wins['Breakout_Range_%'].mean():.1f}%")
    print("--------------------------------------------------")
    print(f" 🔴 LOSERS AVERAGE PATTERN:")
    print(f"    - Avg Volume Surge Ratio : {df_losses['Volume_Ratio'].mean():.2f}x")
    print(f"    - Avg Candle Body %      : {df_losses['Candle_Body_%'].mean():.1f}%")
    print(f"    - Avg Breakout Range %   : {df_losses['Breakout_Range_%'].mean():.1f}%")
    print("==================================================")

    # Save to Google Sheets for detailed visual check
    tab_name = "BT_PATTERN_ANALYSIS"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 10)
      )
      ws.clear()
      ws.append_row([f"PATTERN ANALYSIS: WINNERS VS LOSERS | {datetime.now(IST).strftime('%d-%b-%Y')}"])
      ws.append_row(["Winners Avg Vol:", f"{df_wins['Volume_Ratio'].mean():.2f}x", "Losers Avg Vol:", f"{df_losses['Volume_Ratio'].mean():.2f}x"])
      ws.append_row([])
      ws.append_row(["Symbol", "Date", "Result", "Volume Ratio", "Candle Body %", "Breakout Range %", "PnL %"])
      ws.append_rows(df_all.values.tolist(), value_input_option="USER_ENTERED")
      print("✅ Pattern analysis saved to 'BT_PATTERN_ANALYSIS' tab in Google Sheet!")
    except Exception as e:
      print(f"Sheet error: {e}")
  else:
    print("⚠️ No trades found for pattern analysis.")
else:
  print("⚠️ Watchlist is empty.")
  
