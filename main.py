from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
now_ist = datetime.now(IST)

# 1. DYNAMICALLY DETECT CURRENT MONTH & EXACT PREVIOUS CLOSED MONTH
current_year = now_ist.year
current_month = now_ist.month

if current_month == 1:
  target_closed_year = current_year - 1
  target_closed_month = 12
else:
  target_closed_year = current_year
  target_closed_month = current_month - 1

target_ym_str = f"{target_closed_year}-{target_closed_month:02d}"
current_tab_name = now_ist.strftime("%b_%Y").upper() + "_SIGNALS"
closed_month_label = datetime(
    target_closed_year, target_closed_month, 1
).strftime("%b %Y")
entry_month_label = now_ist.strftime("%b %Y")

print(f"=== CTD_SNIPER CALENDAR-LOCKED SCANNER ===")
print(f"📅 Current Running Month : {entry_month_label}")
print(f"🎯 Target Closed Month   : {closed_month_label} ({target_ym_str})")
print(f"📊 Target Sheet Tab      : {current_tab_name}\n")

# -------------------------------------------------------------
# 2. GOOGLE SHEETS CONNECTION & WATCHLIST LOAD
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
# 3. FRESH BREAKOUT SCANNER (STRICT CALENDAR & CROSSOVER LOCK)
# -------------------------------------------------------------
def scan_fresh_monthly_breakout(symbol, min_consolidation_months=3):
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    df = yf.download(
        ticker_symbol,
        start="2012-01-01",
        interval="1mo",
        auto_adjust=False,
        progress=False,
    )

    if df is None or df.empty or len(df) < 30:
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

    # LOCK 1: Find exact index of the target closed month (e.g., '2026-10')
    ym_series = df.index.strftime("%Y-%m")
    matching_indices = np.where(ym_series == target_ym_str)[0]

    if len(matching_indices) == 0:
      return None  # Stock didn't trade or missing data in target closed month

    i = matching_indices[0]
    if i < min_consolidation_months * 2 + 2:
      return None

    highs = df["High"].values
    closes = df["Close"].values

    df["Range_Ratio"] = ((df["High"] - df["Low"]) / df["Close"]) * 100
    ranges = df["Range_Ratio"].values

    curr_close = closes[i]  # Target closed month close (e.g., Oct close)
    prev_close = closes[
        i - 1
    ]  # Month before target close (e.g., Sep close)
    curr_range = ranges[i]

    # Find True Swing High prior to target closed month
    active_resistance = None
    resistance_index = -1

    for j in range(
        i - min_consolidation_months, min_consolidation_months - 1, -1
    ):
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

    # LOCK 2: STRICT FRESH CROSSOVER CHECK
    # 1. Target closed month (Oct) MUST close ABOVE resistance
    # 2. Previous month (Sep) MUST have closed AT OR BELOW resistance (so it wasn't already broken earlier!)
    if (
        active_resistance is not None
        and curr_close > active_resistance
        and prev_close <= active_resistance
    ):
      post_swing_ranges = ranges[resistance_index + 1 : i]
      if len(post_swing_ranges) > 0 and curr_range <= post_swing_ranges.max():
        tv_link = f'=HYPERLINK("https://www.tradingview.com/chart/?symbol=NSE:{symbol}", "{symbol} Chart")'

        print(
            f"🎯 [FRESH MATCH] {symbol} | {closed_month_label} Close:"
            f" {round(curr_close, 2)} > Swing High:"
            f" {round(active_resistance, 2)} (Prev Close:"
            f" {round(prev_close, 2)})"
        )

        return {
            "Symbol": symbol,
            "Scan_Date": now_ist.strftime("%d-%b-%Y"),
            "Breakout_Close_Month": closed_month_label,
            "Entry_Trading_Month": entry_month_label,
            "Trigger_Price": round(curr_close, 2),
            "Resistance_Broken": round(active_resistance, 2),
            "Stop_Loss (7%)": round(curr_close * 0.93, 2),
            "Take_Profit (14%)": round(curr_close * 1.14, 2),
            "TradingView": tv_link,
        }

    return None

  except Exception:
    return None


# -------------------------------------------------------------
# 4. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
fresh_signals = []
if len(STOCKS) > 0:
  print(
      f"🔍 Scanning {len(STOCKS)} stocks for fresh {closed_month_label}"
      " breakouts..."
  )
  for sym in STOCKS:
    res = scan_fresh_monthly_breakout(sym)
    if res:
      fresh_signals.append(res)

print(f"\nTotal fresh signals for {entry_month_label}: {len(fresh_signals)}")

if len(fresh_signals) > 0:
  df_res = pd.DataFrame(fresh_signals)
  try:
    ws = (
        sh.worksheet(current_tab_name)
        if current_tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(current_tab_name, 2000, 10)
    )
    ws.clear()
    ws.append_row([
        f"FRESH MONTHLY BREAKOUTS FOR {entry_month_label.upper()}"
        f" ({closed_month_label.upper()} CLOSE) |"
        f" {now_ist.strftime('%d-%b-%Y')}"
    ])
    ws.append_row([
        "Symbol",
        "Scan Date",
        "Breakout Close Month",
        "Entry Trading Month",
        "Trigger Price",
        "Resistance Broken",
        "Stop Loss (7%)",
        "Take Profit (14%)",
        "TradingView Link",
    ])
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(
        f"✅ Data successfully saved to '{current_tab_name}' tab in Google"
        " Sheet!"
    )
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print(
      f"⚠️ {closed_month_label} close par koi naya (fresh) breakout match nahi"
      " mila."
  )
  
