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
    "=== CTD_SNIPER 4-WAY OHLCV COMPARISON: ALAG-ALAG VS TEENO MILA KAR ==="
)

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & CLEAN WATCHLIST LOAD
# -------------------------------------------------------------
EXCLUDE_KEYWORDS = ("BEES", "ETF", "GOLD", "SILVER", "NIFTY", "SENSEX")

try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = []
  for s in raw:
    sym = s.strip().upper()
    if (
        sym
        and len(sym) > 2
        and sym not in ["STOCK", "SYMBOL"]
        and not any(k in sym for k in EXCLUDE_KEYWORDS)
    ):
      STOCKS.append(sym)
  print(f"✅ Loaded {len(STOCKS)} clean stocks from Watchlist.")
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


# -------------------------------------------------------------
# 2. EVALUATE ALL 4 VARIANTS ON PRE-LOADED DATAFRAME
# -------------------------------------------------------------
def run_variant_on_df(
    symbol,
    df,
    variant_name,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
):
  trades = []
  highs = df["High"].values
  lows = df["Low"].values
  closes = df["Close"].values
  opens = df["Open"].values
  volumes = df["Volume"].fillna(0).values
  dates = df.index
  n = len(df)

  ranges_pct = ((df["High"] - df["Low"]) / df["Close"]) * 100
  ranges = ranges_pct.values

  min_consolidation_months = 3
  vol_lookback = 6

  in_trade = False
  entry_price = 0.0
  stop_loss = 0.0
  take_profit = 0.0
  breakout_date = None
  entry_date = None
  resistance_level = 0.0
  vol_ratio_val = 0.0
  body_ratio_val = 0.0
  breakout_pct_val = 0.0
  max_price_reached = 0.0

  waiting_entry_idx = None
  start_idx = max(min_consolidation_months * 2 + 5, vol_lookback + 2)

  for i in range(start_idx, n):
    current_date = dates[i]
    curr_close = closes[i]
    curr_high = highs[i]
    curr_low = lows[i]
    curr_open = opens[i]
    curr_range = ranges[i]

    # A. Monitor open trade SL / TP
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
              "Breakout_%": round(breakout_pct_val, 2),
              "Vol_Ratio": round(vol_ratio_val, 2),
              "Body_%": round(body_ratio_val * 100, 1),
              "Entry_Price": round(entry_price, 2),
              "Max_Profit_%": round(max_profit_pct, 2),
              "Result": "WIN" if is_win else "LOSS",
              "PnL_%": round(pnl_pct, 2),
              "TradingView_Chart": sheet_link_formula,
          })

        in_trade = False
        waiting_entry_idx = None
      continue

    # B. Enter at next month's Open right after breakout
    if waiting_entry_idx is not None and i == waiting_entry_idx:
      entry_price = curr_open
      stop_loss = entry_price * (1 - stop_loss_pct)
      take_profit = entry_price * (1 + take_profit_pct)
      entry_date = current_date
      max_price_reached = curr_high
      in_trade = True
      waiting_entry_idx = None
      continue

    # C. Detect Major Swing Resistance & apply OHLCV variant filter
    if waiting_entry_idx is None:
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
        breakout_pct = (
            (curr_close - active_resistance) / active_resistance
        ) * 100

        avg_prev_vol = float(np.mean(volumes[i - vol_lookback : i]))
        vol_ratio = (volumes[i] / avg_prev_vol) if avg_prev_vol > 0 else 0.0

        candle_span = curr_high - curr_low
        body_ratio = (
            abs(curr_close - curr_open) / candle_span
            if (candle_span > 0 and curr_close > curr_open)
            else 0.0
        )

        cond_vol = breakout_pct >= 2.0 and vol_ratio >= 1.5
        cond_body = breakout_pct >= 2.0 and body_ratio >= 0.60
        cond_strength = breakout_pct >= 4.0
        cond_all_3 = (
            breakout_pct >= 4.0 and vol_ratio >= 1.5 and body_ratio >= 0.60
        )

        passed = False
        if variant_name == "1_VOL_SURGE_1.5X":
          passed = cond_vol
        elif variant_name == "2_STRONG_BODY_60PCT":
          passed = cond_body
        elif variant_name == "3_STRONG_CLOSE_4PCT":
          passed = cond_strength
        elif variant_name == "4_ALL_3_COMBINED":
          passed = cond_all_3

        if passed:
          post_swing_ranges = ranges[resistance_index + 1 : i]
          if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
            if i + 1 < n:
              resistance_level = active_resistance
              breakout_pct_val = breakout_pct
              vol_ratio_val = vol_ratio
              body_ratio_val = body_ratio
              breakout_date = current_date
              waiting_entry_idx = i + 1

  return trades


# -------------------------------------------------------------
# 3. DOWNLOAD ONCE & COMPARE ALL 4 SETUPS
# -------------------------------------------------------------
VARIANTS = [
    "1_VOL_SURGE_1.5X",
    "2_STRONG_BODY_60PCT",
    "3_STRONG_CLOSE_4PCT",
    "4_ALL_3_COMBINED",
]
results_map = {v: [] for v in VARIANTS}

if len(STOCKS) > 0:
  print("🚀 Downloading monthly data once & running all 4 OHLCV tests...")
  for sym in STOCKS:
    try:
      ticker_symbol = f"{sym}.NS" if not sym.endswith(".NS") else sym
      df = yf.download(
          ticker_symbol,
          start="2012-01-01",
          interval="1mo",
          auto_adjust=False,
          progress=False,
      )
      if df is None or df.empty or len(df) < 40:
        continue
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
      if "Volume" not in df.columns:
        df["Volume"] = 0

      for v in VARIANTS:
        t = run_variant_on_df(sym, df, v)
        if t:
          results_map[v].extend(t)
    except Exception:
      continue

  summary_rows = []
  best_variant = None
  best_pnl = -999999.0

  print("\n==================================================================")
  print(" 4-WAY OHLCV COMPARISON RESULTS (2025-2026)")
  print("==================================================================")
  for v in VARIANTS:
    t_list = results_map[v]
    total = len(t_list)
    if total > 0:
      df_v = pd.DataFrame(t_list)
      wins = len(df_v[df_v["Result"] == "WIN"])
      losses = total - wins
      win_rate = round((wins / total) * 100, 2)
      total_pnl = round(df_v["PnL_%"].sum(), 2)
      avg_pnl = round(total_pnl / total, 2)
    else:
      wins = losses = 0
      win_rate = total_pnl = avg_pnl = 0.0

    print(
        f" {v:<22} | Trades: {total:<4} | Wins: {wins:<3} | Win%:"
        f" {win_rate:<6}% | PnL: {total_pnl:<8}% | Avg/Trade: {avg_pnl}%"
    )
    summary_rows.append(
        [v, total, wins, losses, win_rate, total_pnl, avg_pnl]
    )

    if total > 0 and total_pnl > best_pnl:
      best_pnl = total_pnl
      best_variant = v

  print("==================================================================")
  print(f" 🏆 HIGHEST PNL SETUP: {best_variant} ({best_pnl}%)")
  print("==================================================================")

  # Export summary & best setup trade log to Google Sheets
  try:
    sum_tab = "BT_4WAY_OHLC_COMPARE"
    ws_sum = (
        sh.worksheet(sum_tab)
        if sum_tab in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(sum_tab, 100, 10)
    )
    ws_sum.clear()
    ws_sum.append_row([
        "4-WAY OHLCV FILTER COMPARISON (2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws_sum.append_row([])
    ws_sum.append_row([
        "Strategy Variant",
        "Total Trades",
        "Wins",
        "Losses",
        "Win Rate %",
        "Total PnL %",
        "Avg PnL / Trade %",
    ])
    ws_sum.append_rows(summary_rows, value_input_option="USER_ENTERED")
    print(f"✅ Comparison table saved to '{sum_tab}'!")

    if best_variant and len(results_map[best_variant]) > 0:
      best_tab = "BT_BEST_OHLC_LOG"
      df_best = pd.DataFrame(results_map[best_variant])
      ws_best = (
          sh.worksheet(best_tab)
          if best_tab in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(best_tab, 2000, 14)
      )
      ws_best.clear()
      ws_best.append_row([
          f"BEST OHLCV SETUP: {best_variant} | PnL: {best_pnl}% |"
          f" {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws_best.append_row([])
      ws_best.append_row([
          "Symbol",
          "Breakout Month",
          "Entry Month",
          "Resistance",
          "Breakout %",
          "Vol Ratio",
          "Body %",
          "Entry Price",
          "Max Profit %",
          "Result",
          "PnL %",
          "TradingView Link",
      ])
      ws_best.append_rows(
          df_best.values.tolist(), value_input_option="USER_ENTERED"
      )
      print(f"✅ Best setup trades saved to '{best_tab}'!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Watchlist is empty.")
    
