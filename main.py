from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER LIQUIDITY FILTER (>= 2 CR TURNOVER) + M-TO-D DZ BACKTEST ===")

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


def get_first_break_monthly_swings(df_m, lookback=3, max_age_days=730):
  """Finds recent monthly swing highs and records ONLY their FIRST breakout month."""
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)
  if n < (lookback * 2 + 2):
    return []

  last_dt = dates[-1]
  swings = []
  consumed_swing_indices = set()

  for j in range(lookback, n - lookback):
    if j in consumed_swing_indices:
      continue
    if (last_dt - dates[j]).days > max_age_days:
      continue

    val = float(highs[j])
    left_ok = all(val > float(highs[k]) for k in range(j - lookback, j))
    right_ok = all(
        val > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    )
    if not (left_ok and right_ok):
      continue

    for k in range(j + 1, n):
      if float(closes[k]) > val:
        consumed_swing_indices.add(j)
        next_break_dt = dates[k + 1] if (k + 1) < n else None
        swings.append({
            "swing_idx": j,
            "swing_date": dates[j],
            "level": val,
            "break_month_start": dates[k],
            "window_end": next_break_dt,
        })
        break

  return swings[-2:]


def find_post_break_demand_zone(df_d, start_dt, end_dt, level):
  """Finds a clean post-breakout base + bullish leg-out within this single break window."""
  if end_dt is not None:
    sub = df_d[(df_d.index >= start_dt) & (df_d.index < end_dt)]
  else:
    sub = df_d[df_d.index >= start_dt]

  if len(sub) < 5:
    return None, sub

  opens = sub["Open"].values
  highs = sub["High"].values
  lows = sub["Low"].values
  closes = sub["Close"].values

  if not any(float(closes[k]) > level for k in range(len(sub))):
    return None, sub

  for i in range(1, len(sub) - 2):
    if float(closes[i]) <= level:
      continue

    body_base = abs(float(closes[i]) - float(opens[i]))
    range_base = max(float(highs[i]) - float(lows[i]), 1e-9)
    next_open = float(opens[i + 1])
    next_close = float(closes[i + 1])
    next_body = next_close - next_open

    if (
        body_base <= 0.50 * range_base
        and next_close > next_open
        and next_body > 1.6 * max(body_base, 1e-9)
    ):
      dz_low = float(lows[i])
      dz_high = max(float(opens[i]), float(closes[i]), float(highs[i]))
      return (dz_low, dz_high, i + 1), sub

  return None, sub


def backtest_liquid_2cr(symbol, rr_ratio=2.0, min_turnover=20_000_000):
  """
    min_turnover = 20,000,000 INR (2 Crore) 20-day average daily turnover.
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

    # Quick check: skip stock entirely if its recent 20-day avg turnover is below 2 Cr
    recent_avg_to = float(df_d["Avg_Turnover_20D"].iloc[-1])
    if np.isnan(recent_avg_to) or recent_avg_to < min_turnover:
      return trades

    swings = get_first_break_monthly_swings(
        df_m, lookback=3, max_age_days=730
    )
    used_zones = set()

    for sw in swings:
      dz, sub = find_post_break_demand_zone(
          df_d, sw["break_month_start"], sw["window_end"], sw["level"]
      )
      if dz is None:
        continue

      dz_low, dz_high, formed_pos = dz
      zone_key = (round(dz_low, 2), round(dz_high, 2))
      if zone_key in used_zones:
        continue

      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      ema200 = sub["EMA200"].values
      avg_to = sub["Avg_Turnover_20D"].values
      dates = sub.index

      in_trade = False
      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      entry_to_cr = 0.0

      for k in range(max(formed_pos + 1, 1), len(sub)):
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
                "Avg_Turnover_Cr": round(entry_to_cr, 2),
                "Monthly_Swing": round(sw["level"], 2),
                "Break_Month": sw["break_month_start"].strftime("%Y-%m"),
                "DZ_Range": f"{round(dz_low,2)}-{round(dz_high,2)}",
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
                "Avg_Turnover_Cr": round(entry_to_cr, 2),
                "Monthly_Swing": round(sw["level"], 2),
                "Break_Month": sw["break_month_start"].strftime("%Y-%m"),
                "DZ_Range": f"{round(dz_low,2)}-{round(dz_high,2)}",
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

        if np.isnan(d_avg_to) or d_avg_to < min_turnover:
          continue

        above_200_ema = d_close > d_ema200
        touched_dz = (d_low <= dz_high) and (d_high >= dz_low)

        if above_200_ema and touched_dz:
          cand_sl = dz_low * 0.99
          risk_pts = d_close - cand_sl
          if risk_pts <= 0:
            continue

          used_zones.add(zone_key)
          entry_to_cr = d_avg_to / 10_000_000.0
          entry_price = d_close
          sl_price = cand_sl
          tp_price = entry_price + (rr_ratio * risk_pts)
          entry_dt = curr_dt
          in_trade = True

    return trades
  except Exception:
    return []


# -------------------------------------------------------------
# 2. RUN BACKTEST & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
for sym in STOCKS:
  t = backtest_liquid_2cr(sym, rr_ratio=2.0, min_turnover=20_000_000)
  if t:
    all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  losses = len(df_res[df_res["Result"] == "LOSS"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print("\n=======================================================")
  print(" LIQUID (>= 2 CR TURNOVER) + M-TO-D DZ RESULTS")
  print("=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("=======================================================")

  tab_name = "M_DZ_LIQUID_2CR_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 13)
    )
    ws.clear()
    ws.append_row([
        "LIQUID (>= 2 CR TURNOVER) + FIRST-BREAK M-TO-D DZ |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Avg Turnover (Cr)",
        "Monthly Swing",
        "Break Month",
        "DZ Range",
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
    
