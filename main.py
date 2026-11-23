from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER FINAL SCANNER: DIRECT 2% MONTHLY BREAKOUT ===")

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
# 2. FIND VALID SWING RESISTANCE BEFORE INDEX `idx`
# -------------------------------------------------------------
def get_active_resistance(highs, idx, min_consolidation_months=3):
  search_range = range(
      idx - min_consolidation_months, min_consolidation_months - 1, -1
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
    for k in range(j + 1, idx):
      if highs[k] > highs[j]:
        ever_broken = True
        break
    if ever_broken:
      continue

    return float(highs[j])

  return None


# -------------------------------------------------------------
# 3. SCAN LATEST & LAST COMPLETED MONTH FOR DIRECT >= 2% BREAKOUT
# -------------------------------------------------------------
def scan_direct_2pct_signals(symbol, min_consolidation_months=3):
  found = []
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
      return found

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < 40:
      return found

    highs = df["High"].values
    closes = df["Close"].values
    dates = df.index
    n = len(df)

    # Check index n-1 (latest bar) and n-2 (last completed month)
    check_indices = [
        (idx, "LATEST_BAR" if idx == n - 1 else "LAST_CLOSED_MONTH")
        for idx in (n - 1, n - 2)
        if idx >= min_consolidation_months * 2 + 5
    ]

    for idx, bar_tag in check_indices:
      res = get_active_resistance(highs, idx, min_consolidation_months)
      if res is not None and res > 0:
        curr_close = float(closes[idx])
        breakout_pct = ((curr_close - res) / res) * 100.0

        if curr_close > res and breakout_pct >= 2.0:
          tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
          sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

          found.append({
              "Symbol": symbol,
              "Signal_Month": dates[idx].strftime("%b %Y"),
              "Bar_Status": bar_tag,
              "Resistance": round(res, 2),
              "Close_Price": round(curr_close, 2),
              "Breakout_%": round(breakout_pct, 2),
              "Ref_Entry_Zone": round(curr_close, 2),
              "TradingView_Chart": sheet_link_formula,
          })

    return found

  except Exception:
    return []


# -------------------------------------------------------------
# 4. RUN SCANNER & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
all_signals = []
if len(STOCKS) > 0:
  print("🚀 Running Final Direct >= 2% Monthly Breakout Scanner...")
  for sym in STOCKS:
    sigs = scan_direct_2pct_signals(sym)
    if sigs:
      all_signals.extend(sigs)

  print(f"\n=======================================================")
  print(f" FINAL DIRECT 2% BREAKOUT SIGNALS FOUND: {len(all_signals)}")
  print(f"=======================================================")

  tab_name = "LATEST_2PCT_SIGNALS"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 1000, 10)
    )
    ws.clear()
    ws.append_row([
        "FINAL DIRECT 2% MONTHLY BREAKOUT SIGNALS |"
        f" {datetime.now(IST).strftime('%d-%b-%Y %H:%M IST')}"
    ])
    ws.append_row([f"Total Signals Found: {len(all_signals)}"])
    ws.append_row([])
    ws.append_row([
        "Symbol",
        "Signal Month",
        "Bar Status",
        "Resistance",
        "Close Price",
        "Breakout %",
        "Ref Entry Price",
        "TradingView Link",
    ])

    if len(all_signals) > 0:
      df_out = pd.DataFrame(all_signals).sort_values(
          by=["Signal_Month", "Breakout_%"], ascending=[False, False]
      )
      ws.append_rows(df_out.values.tolist(), value_input_option="USER_ENTERED")
      for _, row in df_out.iterrows():
        print(
            f" {row['Symbol']:<14} | {row['Signal_Month']:<8} |"
            f" {row['Bar_Status']:<17} | Res: {row['Resistance']:<8} | Close:"
            f" {row['Close_Price']:<8} | +{row['Breakout_%']}%"
        )
    else:
      ws.append_row(["No fresh >= 2% breakout signals found."])
      print("⚠️ Koi naya >= 2% breakout signal nahi mila.")

    print(f"✅ Saved to '{tab_name}' tab in Google Sheet successfully!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Watchlist is empty.")
    
