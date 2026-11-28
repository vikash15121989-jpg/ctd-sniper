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
    "=== CTD_SNIPER BACKTEST: DIRECT DAILY FVG (WITHOUT MONTHLY SWING,"
    " LIQUID STOCKS) ==="
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
  """Returns bullish reversal candle signal on pullback into FVG."""
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


def find_latest_daily_fvg(
    opens, highs, lows, closes, start_idx, end_idx, min_range_window=20
):
  """
    Finds the bullish green candle in [start_idx, end_idx) that has the LARGEST candle range
    (comparing against recent `min_range_window` daily candles), then builds FVG from:
    - fvg_low  = next candle's Low (idx + 1)
    - fvg_high = previous candle's High (idx - 1)
    Returns (fvg_low, fvg_high, idx) or None.
    """
  if end_idx - start_idx < 3:
    return None

  ranges = highs - lows
  best_idx = None
  best_range = -1.0

  for idx in range(max(1, start_idx), min(len(closes) - 1, end_idx)):
    if float(closes[idx]) <= float(opens[idx]):
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


def backtest_direct_daily_fvg(
    symbol,
    rr_ratio=2.0,
    min_turnover=20_000_000,
    fvg_tol_pct=1.0,
    fvg_lookback_bars=30,
    max_trades_per_stock=3,
):
  """
    Direct Daily Chart FVG (No Monthly Swing):
    Liquid stock (20D Avg Turnover >= min_turnover) + Close > EMA200
    -> Pullback near Daily Bullish FVG (+- fvg_tol_pct) + Bullish Reversal Candle
    -> Entry at close with 1:rr_ratio RR.
    """
  trades = []
  try:
    ticker = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df_d = yf.download(
        ticker,
        start="2018-01-01",
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )

    if df_d is None or df_d.empty:
      return trades
    if isinstance(df_d.columns, pd.MultiIndex):
      df_d.columns = df_d.columns.get_level_values(0)

    df_d.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    if len(df_d) < 210:
      return trades

    df_d["EMA200"] = df_d["Close"].ewm(span=200, adjust=False).mean()
    df_d["Turnover"] = df_d["Close"] * df_d["Volume"]
    df_d["Avg_Turnover_20D"] = df_d["Turnover"].rolling(window=20).mean()

    opens = df_d["Open"].values
    highs = df_d["High"].values
    lows = df_d["Low"].values
    closes = df_d["Close"].values
    ema200 = df_d["EMA200"].values
    avg_to = df_d["Avg_Turnover_20D"].values
    dates = df_d.index

    in_trade = False
    trade_count = 0
    last_fvg_idx_used = -1

    entry_price = 0.0
    sl_price = 0.0
    tp_price = 0.0
    entry_dt = None
    signal_name = ""
    fvg_zone_str = ""
    fvg_date_str = ""

    for k in range(202, len(df_d)):
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
              "FVG_Date": fvg_date_str,
              "FVG_Zone": fvg_zone_str,
              "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
              "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
              "Entry_Price": round(entry_price, 2),
              "SL_Price": round(sl_price, 2),
              "TP_Price": round(tp_price, 2),
              "Result": "LOSS",
              "PnL_%": round(pnl, 2),
          })
          in_trade = False
          trade_count += 1
          if trade_count >= max_trades_per_stock:
            break
          continue

        if d_high >= tp_price:
          pnl = ((tp_price - entry_price) / entry_price) * 100.0
          trades.append({
              "Symbol": symbol,
              "Signal": signal_name,
              "FVG_Date": fvg_date_str,
              "FVG_Zone": fvg_zone_str,
              "Entry_Date": entry_dt.strftime("%Y-%m-%d"),
              "Exit_Date": curr_dt.strftime("%Y-%m-%d"),
              "Entry_Price": round(entry_price, 2),
              "SL_Price": round(sl_price, 2),
              "TP_Price": round(tp_price, 2),
              "Result": "WIN",
              "PnL_%": round(pnl, 2),
          })
          in_trade = False
          trade_count += 1
          if trade_count >= max_trades_per_stock:
            break
          continue
        continue

      if np.isnan(d_avg_to) or d_avg_to < min_turnover:
        continue
      if d_close <= d_ema200:
        continue

      search_start = max(200, k - fvg_lookback_bars)
      fvg_info = find_latest_daily_fvg(
          opens, highs, lows, closes, start_idx=search_start, end_idx=k
      )
      if fvg_info is None:
        continue

      fvg_low, fvg_high, fvg_idx = fvg_info
      if fvg_idx == last_fvg_idx_used:
        continue

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
          cand_sl = min(d_low, fvg_low) * 0.99
          risk_pts = d_close - cand_sl
          if risk_pts <= 0:
            continue

          last_fvg_idx_used = fvg_idx
          signal_name = f"D_FVG_{bull_sig}"
          fvg_date_str = dates[fvg_idx].strftime("%Y-%m-%d")
          fvg_zone_str = f"{round(fvg_low, 2)}-{round(fvg_high, 2)}"
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
  t = backtest_direct_daily_fvg(
      sym,
      rr_ratio=2.0,
      min_turnover=20_000_000,
      fvg_tol_pct=1.0,
      fvg_lookback_bars=30,
      max_trades_per_stock=3,
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
  print(" BACKTEST: DIRECT DAILY FVG (WITHOUT SWING HIGH, LIQUID)")
  print("===========================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {losses}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print("===========================================================")

  tab_name = "DAILY_FVG_NO_SWING_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 4000, 14)
    )
    ws.clear()
    ws.append_row([
        "BACKTEST: DIRECT DAILY FVG (NO SWING HIGH, LIQUID STOCKS) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Signal",
        "FVG Date",
        "FVG Zone",
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
    
