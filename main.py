from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER MTF (MONTHLY SIGNAL -> WEEKLY ENTRY) OPTIMIZER ===")

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
# 2. WEEKLY SIMULATION HELPER FOR 4 ENTRY TYPES
# -------------------------------------------------------------
def simulate_weekly_trade(
    df_w, start_idx, stop_loss_pct=0.07, take_profit_pct=0.14
):
  if start_idx >= len(df_w):
    return None

  entry_price = df_w["Open"].iloc[start_idx]
  entry_date = df_w.index[start_idx]
  stop_loss = entry_price * (1 - stop_loss_pct)
  take_profit = entry_price * (1 + take_profit_pct)
  max_price = entry_price

  for w in range(start_idx, len(df_w)):
    w_high = df_w["High"].iloc[w]
    w_low = df_w["Low"].iloc[w]
    w_date = df_w.index[w]

    if w_high > max_price:
      max_price = w_high

    if w_low <= stop_loss or w_high >= take_profit:
      is_win = w_high >= take_profit
      pnl_pct = (
          (take_profit - entry_price) / entry_price * 100
          if is_win
          else (stop_loss - entry_price) / entry_price * 100
      )
      max_profit_pct = ((max_price - entry_price) / entry_price) * 100

      if entry_date.year in [2025, 2026] or w_date.year in [2025, 2026]:
        return {
            "Entry_Date": entry_date.strftime("%d-%b-%Y"),
            "Entry_Price": round(entry_price, 2),
            "Max_Profit_%": round(max_profit_pct, 2),
            "Result": "WIN" if is_win else "LOSS",
            "PnL_%": round(pnl_pct, 2),
        }
      return None
  return None


# -------------------------------------------------------------
# 3. MAIN MTF ENGINE (MONTHLY SIGNAL -> WEEKLY SEARCH)
# -------------------------------------------------------------
def backtest_mtf_all_setups(
    symbol,
    min_consolidation_months=3,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  results = {"SETUP_A_HIGH_CONT": [], "SETUP_B_RETEST": [], "SETUP_C_TIGHT_BO": [], "SETUP_D_DIRECT": []}
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol

    # Fetch Monthly and Weekly Data
    df_m = yf.download(
        ticker_symbol, start="2012-01-01", interval="1mo", auto_adjust=False, progress=False
    )
    df_w = yf.download(
        ticker_symbol, start="2023-01-01", interval="1wk", auto_adjust=False, progress=False
    )

    if df_m is None or df_m.empty or len(df_m) < 30 or df_w is None or df_w.empty:
      return results

    for df in [df_m, df_w]:
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

    df_m["Range_Pct"] = ((df_m["High"] - df_m["Low"]) / df_m["Close"]) * 100
    m_highs = df_m["High"].values
    m_closes = df_m["Close"].values
    m_ranges = df_m["Range_Pct"].values
    n_m = len(df_m)

    for i in range(min_consolidation_months * 2 + 3, n_m):
      curr_m_date = df_m.index[i]
      # Only evaluate signals relevant to 2024-end through 2026
      if curr_m_date.year < 2024:
        continue

      curr_close = m_closes[i]
      curr_high = m_highs[i]
      curr_range = m_ranges[i]

      # Find True Swing High on Monthly
      active_resistance = None
      resistance_index = -1

      for j in range(
          i - min_consolidation_months, min_consolidation_months - 1, -1
      ):
        is_valid_swing = True
        for k in range(j - min_consolidation_months, j):
          if m_highs[j] <= m_highs[k]:
            is_valid_swing = False
            break
        if not is_valid_swing:
          continue

        for k in range(j + 1, j + min_consolidation_months + 1):
          if m_highs[j] <= m_highs[k]:
            is_valid_swing = False
            break
        if not is_valid_swing:
          continue

        ever_broken = False
        for k in range(j + 1, i):
          if m_highs[k] > m_highs[j]:
            ever_broken = True
            break
        if ever_broken:
          continue

        active_resistance = m_highs[j]
        resistance_index = j
        break

      # Check Monthly Breakout Close + Range Filter
      if active_resistance is not None and curr_close > active_resistance:
        post_swing_ranges = m_ranges[resistance_index + 1 : i]
        if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
          # Monthly Breakout Confirmed at end of month `curr_m_date`
          month_end_dt = curr_m_date + pd.offsets.MonthEnd(1)

          # Shift to Weekly Chart after Month Close
          w_after = df_w[df_w.index > month_end_dt]
          if len(w_after) < 3:
            continue

          first_w_idx = df_w.index.get_loc(w_after.index[0])

          # ---------------------------------------------------------
          # SETUP D: Direct Next Week Open (Baseline)
          # ---------------------------------------------------------
          t_d = simulate_weekly_trade(df_w, first_w_idx, stop_loss_pct, take_profit_pct)
          if t_d:
            t_d["Symbol"] = symbol
            results["SETUP_D_DIRECT"].append(t_d)

          # Search next 10 weeks for Setups A, B, C
          search_limit = min(first_w_idx + 10, len(df_w) - 1)
          triggered_a = triggered_b = triggered_c = False

          for w in range(first_w_idx, search_limit):
            w_close = df_w["Close"].iloc[w]
            w_open = df_w["Open"].iloc[w]
            w_high = df_w["High"].iloc[w]
            w_low = df_w["Low"].iloc[w]

            # SETUP A: Weekly Close above Monthly Breakout Candle High
            if not triggered_a and w_close > curr_high:
              t_a = simulate_weekly_trade(df_w, w + 1, stop_loss_pct, take_profit_pct)
              if t_a:
                t_a["Symbol"] = symbol
                results["SETUP_A_HIGH_CONT"].append(t_a)
              triggered_a = True

            # SETUP B: Weekly Retest of Monthly Resistance (±3% zone) + Green Close
            if not triggered_b:
              if w_low <= active_resistance * 1.03 and w_close >= active_resistance * 0.98 and w_close > w_open:
                t_b = simulate_weekly_trade(df_w, w + 1, stop_loss_pct, take_profit_pct)
                if t_b:
                  t_b["Symbol"] = symbol
                  results["SETUP_B_RETEST"].append(t_b)
                triggered_b = True

            # SETUP C: Weekly Tight/Inside Candle Breakout
            if not triggered_c and w > first_w_idx:
              prev_high = df_w["High"].iloc[w - 1]
              prev_low = df_w["Low"].iloc[w - 1]
              prev_range_pct = ((prev_high - prev_low) / prev_low) * 100
              # Previous week was tight (< 6% range) and current week breaks its high
              if prev_range_pct < 6.0 and w_close > prev_high:
                t_c = simulate_weekly_trade(df_w, w + 1, stop_loss_pct, take_profit_pct)
                if t_c:
                  t_c["Symbol"] = symbol
                  results["SETUP_C_TIGHT_BO"].append(t_c)
                triggered_c = True

    return results

  except Exception as e:
    return results


# -------------------------------------------------------------
# 4. RUN & COMPARE ALL 4 WEEKLY ENTRY SETUPS
# -------------------------------------------------------------
master_results = {"SETUP_A_HIGH_CONT": [], "SETUP_B_RETEST": [], "SETUP_C_TIGHT_BO": [], "SETUP_D_DIRECT": []}

if len(STOCKS) > 0:
  for sym in STOCKS:
    res = backtest_mtf_all_setups(sym)
    for k in master_results:
      if res[k]:
        master_results[k].extend(res[k])

print("\n============================================================")
print(" MTF WEEKLY ENTRY CRITERIA COMPARISON (2025-2026)")
print("============================================================")

summary_rows = []
for setup_name, trades_list in master_results.items():
  if len(trades_list) > 0:
    df_s = pd.DataFrame(trades_list)
    total = len(df_s)
    wins = len(df_s[df_s["Result"] == "WIN"])
    win_rate = round((wins / total) * 100, 2)
    total_pnl = round(df_s["PnL_%"].sum(), 2)
    print(f"📌 {setup_name:<20} | Trades: {total:<4} | Win%: {win_rate:<6}% | Total PnL: {total_pnl}%")
    summary_rows.append([setup_name, total, wins, total - wins, f"{win_rate}%", f"{total_pnl}%"])
  else:
    print(f"📌 {setup_name:<20} | Trades: 0")
    summary_rows.append([setup_name, 0, 0, 0, "0%", "0%"])
print("============================================================")

try:
  ws = (
      sh.worksheet("MTF_WEEKLY_COMPARISON")
      if "MTF_WEEKLY_COMPARISON" in [x.title for x in sh.worksheets()]
      else sh.add_worksheet("MTF_WEEKLY_COMPARISON", 2000, 10)
  )
  ws.clear()
  ws.append_row([f"MTF MONTHLY->WEEKLY COMPARISON | {datetime.now(IST).strftime('%d-%b-%Y')}"])
  ws.append_row(["Setup Name", "Total Trades", "Wins", "Losses", "Winning %", "Total PnL %"])
  ws.append_rows(summary_rows, value_input_option="USER_ENTERED")
  print("✅ Comparison summary saved to 'MTF_WEEKLY_COMPARISON' tab in Google Sheet!")
except Exception as e:
  print(f"Sheet update error: {e}")
