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
    "=== CTD_SNIPER BACKTEST: M SWING -> D BREAK -> >=3% RUN -> RETEST SW"
    " HIGH -> NEXT OPEN ==="
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


def get_bullish_candle(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c
):
  """Returns bullish reversal candle tag on retest."""
  total_range = curr_h - curr_l
  if total_range <= 1e-9 or curr_c <= 0:
    return ""

  body = abs(curr_c - curr_o)
  lower_wick = min(curr_o, curr_c) - curr_l
  upper_wick = curr_h - max(curr_o, curr_c)
  is_green = curr_c > curr_o

  # 1. Bullish Engulfing
  if (
      prev_c < prev_o
      and is_green
      and min(curr_o, curr_c) <= min(prev_o, prev_c)
      and max(curr_o, curr_c) >= max(prev_o, prev_c)
  ):
    return "BULLISH_ENGULFING"

  # 2. Hammer
  if (
      curr_c >= curr_o
      and body > 1e-9
      and lower_wick >= 1.8 * body
      and upper_wick <= 0.4 * body
  ):
    return "HAMMER"

  # 3. Clean Bullish Green Candle
  if (
      is_green
      and (body / total_range) >= 0.45
      and ((curr_c - curr_l) / total_range) >= 0.65
  ):
    return "BULLISH_GREEN"

  return ""


def get_monthly_swing_highs(df_m, lookback=2, max_age_days=1095):
  """Finds monthly swing highs."""
  highs = df_m["High"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2):
    return []

  last_dt = dates[-1]
  swings = []

  for j in range(lookback, n - lookback):
    if (last_dt - dates[j]).days > max_age_days:
      continue

    val = float(highs[j])
    left_ok = all(val > float(highs[k]) for k in range(j - lookback, j))
    right_ok = all(
        val > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    )
    if left_ok and right_ok:
      swings.append({
          "swing_date": dates[j],
          "level": val,
      })

  return swings


def backtest_m_swing_d_break_3run_retest_next_open(
    symbol,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    min_run_pct=3.0,
    retest_tol_pct=1.0,
    first_retest_only=True,
):
  """
    1. Find monthly swing high (`sw_lvl`).
    2. On daily candles after `swing_date`, find when `sw_lvl` breaks (`close > sw_lvl`).
    3. Require at least `min_run_pct` run-up after break (`max_peak >= sw_lvl * (1 + min_run_pct/100)`).
    4. Pull back to retest `sw_lvl` (`low <= sw_lvl * (1 + tol)` and `close >= sw_lvl * (1 - tol)`).
    5. On a bullish candle at retest, enter at NEXT trading day's OPEN (`k + 1`).
    """
  trades = []
  try:
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df_m = yf.download(
        ticker,
        start="2018-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )
    df_d = yf.download(
        ticker,
        start="2018-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
    )

    if df_m is None or df_d is None or df_m.empty or df_d.empty:
      return trades
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    if len(df_d) < 205:
      return trades

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()
    df_d["Turnover"] = df_d["Close"] * df_d["Volume"]
    df_d["Avg_Turnover_20D"] = df_d["Turnover"].rolling(window=20).mean()

    swings = get_monthly_swing_highs(df_m, lookback=2, max_age_days=1095)
    if not swings:
      return trades

    for sw in swings:
      sw_lvl = sw["level"]
      run_req_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
      retest_high_lvl = sw_lvl * (1.0 + retest_tol_pct / 100.0)
      retest_low_lvl = sw_lvl * (1.0 - retest_tol_pct / 100.0)

      sub = df_d[df_d.index > sw["swing_date"]]
      if len(sub) < 4:
        continue

      opens = sub["Open"].values
      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      ema200 = sub["EMA200"].values
      avg_to = sub["Avg_Turnover_20D"].values
      dates = sub.index

      # Step 2: Find daily candle where sw_lvl breaks
      break_idx = None
      for idx_b in range(len(sub)):
        if float(closes[idx_b]) > sw_lvl:
          break_idx = idx_b
          break

      if break_idx is None or break_idx >= len(sub) - 2:
        continue

      in_trade = False
      pending_entry = False
      taken_for_swing = False
      run_peak = float(highs[break_idx])

      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      signal_name = ""
      run_pct_val = 0.0

      for k in range(break_idx + 1, len(sub)):
        d_open = float(opens[k])
        d_high = float(highs[k])
        d_low = float(lows[k])
        d_close = float(closes[k])
        d_ema200 = float(ema200[k])
        d_avg_to = float(avg_to[k])
        curr_dt = dates[k]

        if in_trade:
          if d_low <= sl_price:
            pnl = ((sl_price - entry_price) / entry_price) * 100.0
            trades.append({
                "Symbol": symbol,
                "Signal": signal_name,
                "Swing_Month": sw["swing_date"].strftime("%Y-%m"),
                "SW_High": round(sw_lvl, 2),
                "Break_Date": dates[break_idx].strftime("%Y-%m-%d"),
                "Run_%": round(run_pct_val, 2),
                "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
                "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
                "Entry_Price": round(entry_price, 2),
                "SL_Price": round(sl_price, 2),
                "TP_Price": round(tp_price, 2),
                "Result": "LOSS",
                "PnL_%": round(pnl, 2),
            })
            in_trade = False
            break

          if d_high >= tp_price:
            pnl = ((tp_price - entry_price) / entry_price) * 100.0
            trades.append({
                "Symbol": symbol,
                "Signal": signal_name,
                "Swing_Month": sw["swing_date"].strftime("%Y-%m"),
                "SW_High": round(sw_lvl, 2),
                "Break_Date": dates[break_idx].strftime("%Y-%m-%d"),
                "Run_%": round(run_pct_val, 2),
                "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
                "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
                "Entry_Price": round(entry_price, 2),
                "SL_Price": round(sl_price, 2),
                "TP_Price": round(tp_price, 2),
                "Result": "WIN",
                "PnL_%": round(pnl, 2),
            })
            in_trade = False
            break
          continue

        if pending_entry:
          entry_price = d_open
          risk_pts = entry_price - sl_price
          if entry_price > 0 and risk_pts > 0:
            tp_price = entry_price + (rr_ratio * risk_pts)
            entry_dt = curr_dt
            in_trade = True
            taken_for_swing = True
          pending_entry = False
          continue

        if first_retest_only and taken_for_swing:
          break

        prior_peak = run_peak
        run_peak = max(run_peak, d_high)

        # Step 3: Must have at least >= 3% run-up above sw_lvl after breakout
        if prior_peak < run_req_lvl:
          continue
        if np.isnan(d_avg_to) or d_avg_to < min_turnover:
          continue
        if d_close <= d_ema200:
          continue

        # Step 4: Price comes back to retest previous swing high
        retesting_sw_high = (d_low <= retest_high_lvl) and (
            d_close >= retest_low_lvl
        )
        if retesting_sw_high:
          bull_sig = get_bullish_candle(
              float(opens[k - 1]),
              float(highs[k - 1]),
              float(lows[k - 1]),
              float(closes[k - 1]),
              d_open,
              d_high,
              d_low,
              d_close,
          )
          if bull_sig and (k + 1) < len(sub):
            cand_sl = min(d_low, retest_low_lvl) * 0.99
            next_open = float(opens[k + 1])
            if next_open > cand_sl:
              signal_name = bull_sig
              run_pct_val = ((prior_peak - sw_lvl) / sw_lvl) * 100.0
              sl_price = cand_sl
              pending_entry = True

    return trades
  except Exception:
    return []


# -------------------------------------------------------------
# 2. RUN BACKTEST & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
for sym in STOCKS:
  t = backtest_m_swing_d_break_3run_retest_next_open(
      sym,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      min_run_pct=3.0,
      retest_tol_pct=1.0,
      first_retest_only=True,
  )
  if t:
    all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  losses = len(df_res[df_res["Result"] == "LOSS"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print("\n===========================================================")
  print(" BACKTEST: M SWING -> D BREAK -> >=3% RUN -> RETEST -> NEXT OPEN")
  print("===========================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("===========================================================")

  tab_name = "M_SWING_D_BREAK_3RUN_RETEST_NEXTOPEN_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 15)
    )
    ws.clear()
    ws.append_row([
        "BACKTEST: M SWING -> D BREAK -> >=3% RUN -> RETEST SW HIGH -> NEXT"
        f" OPEN | {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "Swing Month",
        "SW High",
        "Break Date",
        "Run %",
        "Entry Date",
        "Exit Date",
        "Entry Price",
        "SL Price",
        "TP Price",
        "Result",
        "PnL %",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
    
