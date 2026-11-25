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
    "=== CTD_SNIPER RECENT SWING + DZ + 200 EMA + DYNAMIC 1:2 RR BACKTEST ==="
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


def is_bullish_engulfing(prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c):
  """Previous candle is bearish, current candle is bullish and engulfs previous body."""
  prev_bear = prev_c < prev_o
  curr_bull = curr_c > curr_o
  if not (prev_bear and curr_bull):
    return False
  prev_body_low = min(prev_o, prev_c)
  prev_body_high = max(prev_o, prev_c)
  curr_body_low = min(curr_o, curr_c)
  curr_body_high = max(curr_o, curr_c)
  return (curr_body_low <= prev_body_low) and (curr_body_high >= prev_body_high)


def is_hammer_candle(o, h, l, c):
  """Bullish/reversal hammer: lower shadow >= 2x body, small upper shadow."""
  total_range = h - l
  if total_range <= 1e-9:
    return False
  body = abs(c - o)
  if body <= 1e-9:
    return False
  lower_wick = min(o, c) - l
  upper_wick = h - max(o, c)
  return (
      (c >= o)
      and (lower_wick >= 2.0 * body)
      and (upper_wick <= 0.35 * body)
      and (body / total_range >= 0.15)
  )


def get_recent_monthly_swing_highs(df_m, lookback=3, max_age_days=730):
  """Returns recent unbroken-then-broken monthly swing highs within `max_age_days`."""
  highs = df_m["High"].values
  closes = df_m["Close"].values
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
    if not (left_ok and right_ok):
      continue

    break_idx = None
    for k in range(j + 1, n):
      if float(closes[k]) > val:
        break_idx = k
        break

    if break_idx is not None:
      swings.append({
          "swing_date": dates[j],
          "level": val,
          "break_month_start": dates[break_idx],
      })

  return swings[-2:]


def find_post_break_demand_zone(df_d, start_dt, level):
  """Finds a clean post-breakout base candle + impulsive bullish leg-out on Daily."""
  sub = df_d[df_d.index >= start_dt]
  if len(sub) < 6:
    return None

  opens = sub["Open"].values
  highs = sub["High"].values
  lows = sub["Low"].values
  closes = sub["Close"].values

  if not any(float(closes[k]) > level for k in range(len(sub))):
    return None

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
      return dz_low, dz_high, i + 1

  return None


def backtest_combined_200ema_1to2(symbol, rr_ratio=2.0):
  trades = []
  try:
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df_m = yf.download(
        ticker, start="2018-01-01", interval="1mo", auto_adjust=False, progress=False
    )
    df_d = yf.download(
        ticker, start="2018-01-01", interval="1d", auto_adjust=False, progress=False
    )

    if df_m is None or df_d is None or df_m.empty or df_d.empty:
      return trades
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df_d) < 205:
      return trades

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()

    swings = get_recent_monthly_swing_highs(
        df_m, lookback=3, max_age_days=730
    )
    used_zones = set()

    for sw in swings:
      dz = find_post_break_demand_zone(df_d, sw["break_month_start"], sw["level"])
      if dz is None:
        continue

      dz_low, dz_high, formed_pos = dz
      zone_key = (round(dz_low, 2), round(dz_high, 2))
      if zone_key in used_zones:
        continue

      sub = df_d[df_d.index >= sw["break_month_start"]]
      opens = sub["Open"].values
      highs = sub["High"].values
      lows = sub["Low"].values
      closes = sub["Close"].values
      ema200 = sub["EMA200"].values
      dates = sub.index

      in_trade = False
      entry_price = 0.0
      sl_price = 0.0
      tp_price = 0.0
      entry_dt = None
      pattern_used = ""

      for k in range(max(formed_pos + 1, 1), len(sub)):
        d_open = float(opens[k])
        d_high = float(highs[k])
        d_low = float(lows[k])
        d_close = float(closes[k])
        d_ema200 = float(ema200[k])
        curr_dt = dates[k]

        if in_trade:
          # Conservative SL check first
          if d_low <= sl_price:
            pnl = ((sl_price - entry_price) / entry_price) * 100.0
            trades.append({
                "Symbol": symbol,
                "Pattern": pattern_used,
                "Monthly_Swing": round(sw["level"], 2),
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
                "Pattern": pattern_used,
                "Monthly_Swing": round(sw["level"], 2),
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

        # 200 EMA trend filter + Pullback into Demand Zone + Engulfing / Hammer
        above_200_ema = d_close > d_ema200
        touched_dz = (d_low <= dz_high) and (d_high >= dz_low)
        if not (above_200_ema and touched_dz):
          continue

        engulf = is_bullish_engulfing(
            float(opens[k - 1]),
            float(highs[k - 1]),
            float(lows[k - 1]),
            float(closes[k - 1]),
            d_open,
            d_high,
            d_low,
            d_close,
        )
        hammer = is_hammer_candle(d_open, d_high, d_low, d_close)

        if engulf or hammer:
          cand_sl = dz_low * 0.99
          risk_pts = d_close - cand_sl
          if risk_pts <= 0:
            continue

          used_zones.add(zone_key)
          pattern_used = "ENGULFING" if engulf else "HAMMER"
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
  t = backtest_combined_200ema_1to2(sym, rr_ratio=2.0)
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
  print(" RECENT SWING + DZ + 200 EMA + DYNAMIC 1:2 RR RESULTS")
  print("=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("=======================================================")

  tab_name = "M_DZ_200EMA_1TO2_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 12)
    )
    ws.clear()
    ws.append_row([
        "RECENT SWING + DZ + 200 EMA + 1:2 RR |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Pattern",
        "Monthly Swing",
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
    
