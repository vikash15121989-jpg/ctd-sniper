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
    "=== CTD_SNIPER BACKTEST: FIRST RECENT MONTHLY SWING (LOOKBACK=3) +"
    " PULLBACK NEAR FVG (LIQUID STOCKS) ==="
)

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & STOCK-ONLY WATCHLIST LOAD
# -------------------------------------------------------------
NON_STOCK_KEYWORDS = (
    "BEES",
    "ETF",
    "GOLD",
    "SILVER",
    "NIFTY",
    "LIQUID",
    "SENSEX",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NEXT50",
    "MON100",
    "MAFANG",
)


def is_equity_stock(symbol: str) -> bool:
  s = symbol.strip().upper()
  if not s or len(s) <= 2 or s in {"STOCK", "SYMBOL"}:
    return False
  if s.startswith(("^", "$")):
    return False
  clean_sym = s.replace(".NS", "").replace(".BO", "")
  if any(kw in clean_sym for kw in NON_STOCK_KEYWORDS):
    return False
  return True


try:
  gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
  sh = gc.open("CTD_Sniper")
  raw = sh.worksheet("Watchlist").col_values(1)
  STOCKS = list(
      dict.fromkeys([s.strip().upper() for s in raw if is_equity_stock(s)])
  )
  print(
      f"✅ Loaded {len(STOCKS)} equity stocks (filtered out"
      " ETFs/indices/invalid)."
  )
except Exception as e:
  print(f"❌ Google Sheet Connection Error: {e}")
  STOCKS = []


def get_bullish_signal(
    prev_o, prev_h, prev_l, prev_c, curr_o, curr_h, curr_l, curr_c
):
  """Returns bullish reversal candle signal on pullback."""
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

  # 2. Hammer / Strong Lower Wick
  if (
      curr_c >= curr_o
      and (body / curr_c) >= 0.003
      and lower_wick >= 1.8 * body
      and upper_wick <= 0.4 * body
  ):
    return "HAMMER"

  # 3. Bullish Green Candle
  if (
      is_green
      and (body / total_range) >= 0.45
      and ((curr_c - curr_l) / total_range) >= 0.65
  ):
    return "BULLISH_GREEN"

  return ""


def get_first_recent_monthly_swing(df_m, lookback=3):
  """
    Scans from right (most recent completed monthly candle) to left (older candles)
    and returns the VERY FIRST monthly swing high found using `lookback` bars on both sides.
    """
  highs = df_m["High"].values
  closes = df_m["Close"].values
  dates = df_m.index
  n = len(df_m)

  # Exclude the currently forming last bar (n - 1)
  start_idx = n - 2 - lookback
  if start_idx < lookback:
    return None

  for j in range(start_idx, lookback - 1, -1):
    val = float(highs[j])
    left_ok = all(val > float(highs[k]) for k in range(j - lookback, j))
    right_ok = all(
        val > float(highs[k]) for k in range(j + 1, j + lookback + 1)
    )
    if left_ok and right_ok:
      break_dt = dates[-1]
      for k in range(j + 1, n):
        if float(closes[k]) > val:
          break_dt = dates[k]
          break
      return {
          "swing_date": dates[j],
          "level": val,
          "break_month_start": break_dt,
      }

  return None


def find_largest_bullish_fvg(
    opens, highs, lows, closes, start_idx, end_idx, min_range_window=20
):
  """
    Finds the bullish green candle in [start_idx, end_idx) that has the LARGEST candle range
    (comparing against recent `min_range_window` candles), then builds FVG from:
    - fvg_low  = next candle's Low (idx + 1)
    - fvg_high = previous candle's High (idx - 1)
    Returns (fvg_low, fvg_high, fvg_idx) or None.
    """
  if end_idx - start_idx < 3:
    return None

  ranges = highs - lows
  best_idx = None
  best_range = -1.0

  for idx in range(max(1, start_idx), min(len(closes) - 1, end_idx)):
    is_bull_green = float(closes[idx]) > float(opens[idx])
    if not is_bull_green:
      continue

    win_start = max(0, idx - min_range_window)
    max_recent_range = float(np.max(ranges[win_start : idx + 1]))
    curr_range = float(ranges[idx])

    if curr_range >= max_recent_range and curr_range > best_range:
      fvg_low = float(lows[idx + 1])
      fvg_high = float(highs[idx - 1])
      if fvg_high > fvg_low:
        best_range = curr_range
        best_idx = idx

  if best_idx is None:
    return None

  return (
      float(lows[best_idx + 1]),
      float(highs[best_idx - 1]),
      best_idx,
  )


def backtest_first_swing_fvg(
    symbol,
    lookback=3,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    min_run_pct=3.0,
    zone_tol_pct=1.5,
    fvg_tol_pct=1.0,
    first_trigger_only=True,
):
  """
    First recent monthly swing (lookback=3) -> Daily breakout -> >= min_run_pct rally
    -> Pullback into flipped Swing_DZ (+- zone_tol_pct) near Bullish FVG (+- fvg_tol_pct)
    above 200 EMA & min_turnover -> Bullish reversal candle -> Entry at close with 1:rr_ratio RR.
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
        threads=False,
    )
    df_d = yf.download(
        ticker,
        start="2018-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    if df_m is None or df_d is None or df_m.empty or df_d.empty:
      return trades
    if isinstance(df_m.columns, pd.MultiIndex):
      df_m.columns = df_m.columns.get_level_values(0)
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_m.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    df_d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    if len(df_d) < 205 or len(df_m) < (lookback * 2 + 2):
      return trades

    sw = get_first_recent_monthly_swing(df_m, lookback=lookback)
    if sw is None:
      return trades

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()
    df_d["Turnover"] = df_d["Close"] * df_d["Volume"]
    df_d["Avg_Turnover_20D"] = df_d["Turnover"].rolling(window=20).mean()

    sw_lvl = sw["level"]
    run_target_lvl = sw_lvl * (1.0 + min_run_pct / 100.0)
    dz_high = sw_lvl * (1.0 + zone_tol_pct / 100.0)
    dz_low = sw_lvl * (1.0 - zone_tol_pct / 100.0)

    sub = df_d[df_d.index >= sw["break_month_start"]]
    if len(sub) < 4:
      return trades

    opens = sub["Open"].values
    highs = sub["High"].values
    lows = sub["Low"].values
    closes = sub["Close"].values
    ema200 = sub["EMA200"].values
    avg_to = sub["Avg_Turnover_20D"].values
    dates = sub.index

    in_trade = False
    taken_for_swing = False
    running_max_high = float(highs[0])

    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    entry_dt = None
    signal_name = ""
    run_up_at_entry = 0.0
    fvg_zone_str = ""

    for k in range(2, len(sub)):
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
              "Swing_Date": sw["swing_date"].strftime("%Y-%m"),
              "Swing_DZ": round(sw_lvl, 2),
              "FVG_Zone": fvg_zone_str,
              "Run_Up_%": round(run_up_at_entry, 2),
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
              "Swing_Date": sw["swing_date"].strftime("%Y-%m"),
              "Swing_DZ": round(sw_lvl, 2),
              "FVG_Zone": fvg_zone_str,
              "Run_Up_%": round(run_up_at_entry, 2),
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

      if first_trigger_only and taken_for_swing:
        break

      prior_max = running_max_high
      running_max_high = max(running_max_high, d_high)

      if np.isnan(d_avg_to) or d_avg_to < min_turnover:
        continue
      if d_close <= d_ema200:
        continue

      if prior_max < run_target_lvl or d_close >= prior_max:
        continue

      in_or_near_dz = (d_low <= dz_high) and (d_close >= dz_low)
      if not in_or_near_dz:
        continue

      fvg_info = find_largest_bullish_fvg(
          opens, highs, lows, closes, start_idx=0, end_idx=k
      )
      if fvg_info is None:
        continue

      fvg_low, fvg_high, _ = fvg_info
      near_fvg_low = fvg_low * (1.0 - fvg_tol_pct / 100.0)
      near_fvg_high = fvg_high * (1.0 + fvg_tol_pct / 100.0)
      near_fvg = (d_low <= near_fvg_high) and (d_high >= near_fvg_low)

      if near_fvg:
        bull_sig = get_bullish_signal(
            float(opens[k - 1]),
            float(highs[k - 1]),
            float(lows[k - 1]),
            float(closes[k - 1]),
            d_open,
            d_high,
            d_low,
            d_close,
        )
        if bull_sig:
          cand_sl = min(d_low, sw_lvl, fvg_low) * 0.99
          risk_pts = d_close - cand_sl
          if risk_pts <= 0:
            continue

          taken_for_swing = True
          signal_name = f"FVG_{bull_sig}"
          fvg_zone_str = f"{round(fvg_low, 2)}-{round(fvg_high, 2)}"
          run_up_at_entry = ((prior_max - sw_lvl) / sw_lvl) * 100.0
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
  t = backtest_first_swing_fvg(
      sym,
      lookback=3,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      min_run_pct=3.0,
      zone_tol_pct=1.5,
      fvg_tol_pct=1.0,
      first_trigger_only=True,
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
  print(" BACKTEST: FIRST RECENT MONTHLY SWING + NEAR FVG (LIQUID)")
  print("===========================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("===========================================================")

  tab_name = "M_SWING_FIRST_LIQ_FVG_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 3000, 15)
    )
    ws.clear()
    ws.append_row([
        "BACKTEST: FIRST RECENT MONTHLY SWING + NEAR FVG (LIQUID STOCKS) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "Swing Date",
        "Swing DZ",
        "FVG Zone",
        "Run Up %",
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
    
