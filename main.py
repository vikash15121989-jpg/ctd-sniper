from datetime import datetime, timedelta
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print(
    "=== CTD_SNIPER DYNAMIC SCANNER: CURRENT MONTH ENTRY <- PREV MONTH"
    " BREAKOUT ==="
)

# Set to None for live current month, or e.g. "2026-02" to test Feb -> Jan breakout
OVERRIDE_CURRENT_YM = None

now_ist = datetime.now(IST)
if OVERRIDE_CURRENT_YM:
  ref_dt = datetime.strptime(OVERRIDE_CURRENT_YM + "-01", "%Y-%m-%d")
else:
  ref_dt = now_ist.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

curr_entry_ym = ref_dt.strftime("%Y-%m")
curr_entry_label = ref_dt.strftime("%b %Y")

prev_month_dt = ref_dt - timedelta(days=1)
target_breakout_ym = prev_month_dt.strftime("%Y-%m")
target_breakout_label = prev_month_dt.strftime("%b %Y")

print(
    f"📅 Active Entry Month : {curr_entry_label} ({curr_entry_ym}) | Required"
    f" Breakout Month : {target_breakout_label} ({target_breakout_ym})"
)

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
# 3. SCAN ONLY STOCKS WITH BREAKOUT IN `target_breakout_ym`
# -------------------------------------------------------------
def scan_prev_month_breakout(symbol, min_consolidation_months=3):
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
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < min_consolidation_months * 2 + 6:
      return None

    highs = df["High"].values
    closes = df["Close"].values
    opens = df["Open"].values
    dates = df.index
    n = len(df)

    brk_idx = None
    for idx in range(min_consolidation_months * 2 + 5, n):
      if dates[idx].strftime("%Y-%m") == target_breakout_ym:
        brk_idx = idx
        break

    if brk_idx is None:
      return None

    res = get_active_resistance(highs, brk_idx, min_consolidation_months)
    if res is None or res <= 0:
      return None

    brk_close = float(closes[brk_idx])
    breakout_pct = ((brk_close - res) / res) * 100.0

    if brk_close > res and breakout_pct >= 2.0:
      curr_month_open = (
          float(opens[brk_idx + 1]) if (brk_idx + 1) < n else brk_close
      )
      tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
      sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

      return {
          "Symbol": symbol,
          "Breakout_Month": dates[brk_idx].strftime("%b %Y"),
          "Entry_Month": curr_entry_label,
          "Resistance": round(res, 2),
          "Breakout_Close": round(brk_close, 2),
          "Breakout_%": round(breakout_pct, 2),
          "Entry_Month_Open_Ref": round(curr_month_open, 2),
          "TradingView_Chart": sheet_link_formula,
      }

    return None

  except Exception:
    return None


# -------------------------------------------------------------
# 4. RUN SCANNER & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
matched_stocks = []
if len(STOCKS) > 0:
  print(
      f"🚀 Scanning {target_breakout_label} (>=2%) breakouts for"
      f" {curr_entry_label} entry..."
  )
  for sym in STOCKS:
    item = scan_prev_month_breakout(sym)
    if item:
      matched_stocks.append(item)

  print(f"\n=======================================================")
  print(f" {target_breakout_label} BREAKOUTS -> {curr_entry_label} ENTRY")
  print(f" Total Matched Stocks : {len(matched_stocks)}")
  print(f"=======================================================")

  tab_name = "PREV_MONTH_BREAKOUT_SIGNALS"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 1000, 10)
    )
    ws.clear()
    ws.append_row([
        f"{target_breakout_label} BREAKOUT (>=2%) -> {curr_entry_label} ENTRY"
        f" | {now_ist.strftime('%d-%b-%Y %H:%M IST')}"
    ])
    ws.append_row([f"Total Matched Stocks: {len(matched_stocks)}"])
    ws.append_row([])
    ws.append_row([
        "Symbol",
        "Breakout Month",
        "Entry Month",
        "Resistance",
        "Breakout Close",
        "Breakout %",
        "Entry Month Open Ref",
        "TradingView Link",
    ])

    if len(matched_stocks) > 0:
      df_out = pd.DataFrame(matched_stocks).sort_values(
          by="Breakout_%", ascending=False
      )
      ws.append_rows(df_out.values.tolist(), value_input_option="USER_ENTERED")
      for _, row in df_out.iterrows():
        print(
            f" {row['Symbol']:<14} | Res: {row['Resistance']:<8} | Close:"
            f" {row['Breakout_Close']:<8} | +{row['Breakout_%']}% | Open Ref:"
            f" {row['Entry_Month_Open_Ref']}"
        )
    else:
      ws.append_row([
          f"No stock had a >= 2% breakout in {target_breakout_label}."
      ])
      print(
          f"⚠️ {target_breakout_label} mein >= 2% breakout wala koi stock nahi"
          " mila."
      )

    print(f"✅ Saved to '{tab_name}' tab in Google Sheet successfully!")
  except Exception as e:
    print(f"Sheet update error: {e}")
else:
  print("⚠️ Watchlist is empty.")
  
