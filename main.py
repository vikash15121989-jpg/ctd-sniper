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
    "=== CTD_SNIPER LIVE SCANNER: >=2% CLOSE BREAKOUT + POST-SWING RANGE"
    " ==="
)

MIN_CONSOLIDATION_MONTHS = 3
MIN_BREAKOUT_PCT = 0.02  # 2% above swing high on close
STOP_LOSS_PCT = 0.07
TAKE_PROFIT_PCT = 0.14
NEAR_SWING_WATCH_PCT = 0.03
EXCLUDE_KEYWORDS = ("BEES", "ETF", "GOLD", "SILVER", "NIFTY", "SENSEX")

# -------------------------------------------------------------
# 1. GOOGLE SHEETS CONNECTION & CLEAN WATCHLIST LOAD
# -------------------------------------------------------------
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
# 2. FIND STRONG UNBROKEN SWING HIGH PRIOR TO `target_idx`
# -------------------------------------------------------------
def get_active_swing_high(highs, target_idx, min_consolidation_months=3):
  start_j = min(target_idx - min_consolidation_months, len(highs) - 1)
  end_j = min_consolidation_months - 1

  for j in range(start_j, end_j, -1):
    is_valid_swing = True
    for k in range(j - min_consolidation_months, j):
      if highs[j] <= highs[k]:
        is_valid_swing = False
        break
    if not is_valid_swing:
      continue

    for k in range(j + 1, j + min_consolidation_months + 1):
      if k < len(highs) and highs[j] <= highs[k]:
        is_valid_swing = False
        break
    if not is_valid_swing:
      continue

    ever_broken = False
    for k in range(j + 1, target_idx):
      if highs[k] > highs[j]:
        ever_broken = True
        break
    if ever_broken:
      continue

    return float(highs[j]), j

  return None, -1


def check_breakout_and_range(
    highs, lows, closes, ranges, target_idx, swing_level, swing_idx
):
  """Checks >=2% close breakout and range <= max post-swing range at `target_idx`."""
  if swing_level is None or swing_level <= 0 or swing_idx < 0:
    return False, None, None

  for k in range(swing_idx + 1, target_idx):
    if highs[k] > swing_level:
      return False, None, None

  target_close = float(closes[target_idx])
  target_range = float(ranges[target_idx])
  if np.isnan(target_close) or np.isnan(target_range):
    return False, None, None

  trigger_2pct_close = swing_level * (1.0 + MIN_BREAKOUT_PCT)
  if target_close < trigger_2pct_close:
    return False, round(trigger_2pct_close, 2), round(target_range, 2)

  post_swing_ranges = ranges[swing_idx + 1 : target_idx]
  post_swing_ranges = post_swing_ranges[~np.isnan(post_swing_ranges)]
  if len(post_swing_ranges) == 0:
    return False, round(trigger_2pct_close, 2), round(target_range, 2)

  max_post_range = float(np.max(post_swing_ranges))
  range_ok = target_range <= max_post_range

  return (
      range_ok,
      round(trigger_2pct_close, 2),
      round(target_range, 2),
  )


# -------------------------------------------------------------
# 3. LIVE SCAN: PREV COMPLETED MONTH BREAK -> CURR MONTH ENTRY
# -------------------------------------------------------------
def scan_live_2pct_range(symbol):
  try:
    clean_sym = symbol.replace(".NS", "").strip().upper()
    ticker_symbol = f"{clean_sym}.NS"

    df = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty or len(df) < MIN_CONSOLIDATION_MONTHS * 2 + 5:
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < MIN_CONSOLIDATION_MONTHS * 2 + 5:
      return None

    opens = df["Open"].values.astype(float)
    highs = df["High"].values.astype(float)
    lows = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    ranges = np.where(closes > 0, ((highs - lows) / closes) * 100.0, np.nan)
    n = len(df)

    curr_idx = n - 1  # Current active month
    prev_idx = n - 2  # Last completed month

    curr_month_str = df.index[curr_idx].strftime("%b %Y")
    prev_month_str = df.index[prev_idx].strftime("%b %Y")
    curr_open = float(opens[curr_idx])
    curr_close = float(closes[curr_idx])

    tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{clean_sym}", "TV Chart")'

    # Case 1: Broke out in previous completed month (prev_idx) -> Entry in current month (curr_idx)
    prev_swing, prev_swing_idx = get_active_swing_high(
        highs, prev_idx, MIN_CONSOLIDATION_MONTHS
    )
    if prev_swing is not None:
      prev_qualified, trig_2pct, prev_rng = check_breakout_and_range(
          highs, lows, closes, ranges, prev_idx, prev_swing, prev_swing_idx
      )
      if prev_qualified and curr_open > 0:
        sl_price = curr_open * (1.0 - STOP_LOSS_PCT)
        tp_price = curr_open * (1.0 + TAKE_PROFIT_PCT)
        post_slice = ranges[prev_swing_idx + 1 : prev_idx]
        post_slice = post_slice[~np.isnan(post_slice)]
        post_max_rng = float(np.max(post_slice)) if len(post_slice) > 0 else 0.0
        return {
            "Symbol": clean_sym,
            "Active_Month": curr_month_str,
            "Breakout_Month": prev_month_str,
            "Swing_Month": df.index[prev_swing_idx].strftime("%b %Y"),
            "Status": "ENTRY_THIS_MONTH",
            "Swing_High": round(prev_swing, 2),
            "Close_Target_2pct": trig_2pct,
            "Curr_Open_Entry": round(curr_open, 2),
            "Curr_Close": round(curr_close, 2),
            "Break_Range_%": prev_rng,
            "Max_Post_Range_%": round(post_max_rng, 2),
            "SL_7pct": round(sl_price, 2),
            "TP_14pct": round(tp_price, 2),
            "Chart": tv_link,
        }

    # Case 2: Breaking out right now in current month (curr_idx) -> Next-month entry candidate
    curr_swing, curr_swing_idx = get_active_swing_high(
        highs, curr_idx, MIN_CONSOLIDATION_MONTHS
    )
    if curr_swing is not None:
      curr_qualified, trig_2pct, curr_rng = check_breakout_and_range(
          highs, lows, closes, ranges, curr_idx, curr_swing, curr_swing_idx
      )
      post_swing_slice = ranges[curr_swing_idx + 1 : curr_idx]
      post_swing_slice = post_swing_slice[~np.isnan(post_swing_slice)]
      max_post_rng = (
          round(float(np.max(post_swing_slice)), 2)
          if len(post_swing_slice) > 0
          else ""
      )

      if curr_qualified:
        return {
            "Symbol": clean_sym,
            "Active_Month": curr_month_str,
            "Breakout_Month": curr_month_str,
            "Swing_Month": df.index[curr_swing_idx].strftime("%b %Y"),
            "Status": "BREAK_CURR_MONTH_NEXT_ENTRY",
            "Swing_High": round(curr_swing, 2),
            "Close_Target_2pct": trig_2pct,
            "Curr_Open_Entry": round(curr_open, 2),
            "Curr_Close": round(curr_close, 2),
            "Break_Range_%": curr_rng,
            "Max_Post_Range_%": max_post_rng,
            "SL_7pct": "",
            "TP_14pct": "",
            "Chart": tv_link,
        }

      # Case 3: Near active swing high watch
      if curr_close >= curr_swing * (1.0 - NEAR_SWING_WATCH_PCT):
        curr_rng_val = ranges[curr_idx]
        return {
            "Symbol": clean_sym,
            "Active_Month": curr_month_str,
            "Breakout_Month": "-",
            "Swing_Month": df.index[curr_swing_idx].strftime("%b %Y"),
            "Status": "NEAR_SWING_WATCH",
            "Swing_High": round(curr_swing, 2),
            "Close_Target_2pct": round(curr_swing * (1.0 + MIN_BREAKOUT_PCT), 2),
            "Curr_Open_Entry": round(curr_open, 2),
            "Curr_Close": round(curr_close, 2),
            "Break_Range_%": (
                round(float(curr_rng_val), 2) if not np.isnan(curr_rng_val) else ""
            ),
            "Max_Post_Range_%": max_post_rng,
            "SL_7pct": "",
            "TP_14pct": "",
            "Chart": tv_link,
        }

    return None

  except Exception:
    return None


# -------------------------------------------------------------
# 4. RUN LIVE SCAN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
signals = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    res = scan_live_2pct_range(sym)
    if res:
      signals.append(res)

if len(signals) > 0:
  df_out = pd.DataFrame(signals)
  priority_map = {
      "ENTRY_THIS_MONTH": 1,
      "BREAK_CURR_MONTH_NEXT_ENTRY": 2,
      "NEAR_SWING_WATCH": 3,
  }
  df_out["Priority"] = df_out["Status"].map(priority_map).fillna(9)
  df_out.sort_values(
      by=["Priority", "Symbol"], ascending=[True, True], inplace=True
  )
  df_out.drop(columns=["Priority"], inplace=True)

  # Clean any NaN / Inf values before sending to Google Sheets JSON API
  df_out.replace([np.inf, -np.inf], np.nan, inplace=True)
  df_out = df_out.fillna("")

  entry_df = df_out[df_out["Status"] == "ENTRY_THIS_MONTH"]
  b_count = len(df_out[df_out["Status"] == "BREAK_CURR_MONTH_NEXT_ENTRY"])
  print(
      f"\n✅ Total Signals: {len(df_out)} | ENTRY_THIS_MONTH: {len(entry_df)}"
      f" | BREAK_CURR_MONTH_NEXT_ENTRY: {b_count}"
  )
  if len(entry_df) > 0:
    print("🔥 ENTRY_THIS_MONTH Stocks:")
    for _, r in entry_df.iterrows():
      print(
          f"   -> {r['Symbol']} | Entry Open: {r['Curr_Open_Entry']} | SL:"
          f" {r['SL_7pct']} | TP: {r['TP_14pct']}"
      )

  tab_name = "SWING_2PCT_LIVE_SIGNALS"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 1000, 16)
    )
    ws.clear()
    ws.append_row([
        "SWING >=2% BREAKOUT + RANGE LIVE SCANNER |"
        f" {datetime.now(IST).strftime('%d-%b-%Y %H:%M IST')}"
    ])
    ws.append_row(list(df_out.columns))
    ws.append_rows(df_out.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Signals exported to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Filhaal koi active entry ya breakout signal nahi mila.")
    
