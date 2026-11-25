from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER MONTHLY BASE ACCUMULATION BACKTEST (NO LOOKAHEAD) ===")

# Strategy Parameters
BASE_MONTHS = 4  # 4-month consolidation base window
MAX_BASE_WIDTH_PCT = 45.0  # Realistic monthly base height % (updated from 22%)
MIN_BREAK_BUFFER_PCT = 0.01  # Close >= Base_High * 1.01 to confirm breakout
STOP_LOSS_PCT = 0.07
TAKE_PROFIT_PCT = 0.14
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
# 2. CHECK ACCUMULATION BASE & BREAKOUT ON COMPLETED MONTH `sig_idx`
# -------------------------------------------------------------
def check_base_accumulation_breakout(opens, highs, lows, closes, sig_idx):
  start_b = sig_idx - BASE_MONTHS
  if start_b < 2:
    return False, None, None, None

  base_highs = highs[start_b:sig_idx]
  base_lows = lows[start_b:sig_idx]
  base_closes = closes[start_b:sig_idx]

  base_high = float(np.max(base_highs))
  base_low = float(np.min(base_lows))
  if base_low <= 0 or base_high <= base_low:
    return False, None, None, None

  base_width_pct = ((base_high - base_low) / base_low) * 100.0
  if base_width_pct > MAX_BASE_WIDTH_PCT:
    return False, None, None, None

  # Rule: Price dipped into lower 35% of the base range (tested demand)
  lower_zone = base_low + 0.35 * (base_high - base_low)
  dipped_in_base = np.any(base_lows <= lower_zone)
  if not dipped_in_base:
    return False, None, None, None

  # Rule: Accumulation hold (no distribution breakdown close below Base_Low)
  if np.any(base_closes < base_low * 0.98):
    return False, None, None, None

  # Rule: Prior trend check
  if start_b - 3 >= 0:
    earlier_close = float(closes[start_b - 3])
    if earlier_close > 0 and base_high < earlier_close * 0.95:
      return False, None, None, None

  # Rule: Completed signal month `sig_idx` closes cleanly above Base_High
  sig_close = float(closes[sig_idx])
  sig_low = float(lows[sig_idx])
  if sig_low < base_low * 0.98:
    return False, None, None, None

  if sig_close >= base_high * (1.0 + MIN_BREAK_BUFFER_PCT):
    return (
        True,
        round(base_high, 2),
        round(base_low, 2),
        round(base_width_pct, 2),
    )

  return False, None, None, None


# -------------------------------------------------------------
# 3. BACKTEST ENGINE (SIGNAL AT `i-1` CLOSE -> ENTRY AT `i` OPEN)
# -------------------------------------------------------------
def backtest_base_accumulation(symbol):
  trades = []
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

    if df is None or df.empty or len(df) < BASE_MONTHS + 8:
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    if len(df) < BASE_MONTHS + 8:
      return trades

    opens = df["Open"].values.astype(float)
    highs = df["High"].values.astype(float)
    lows = df["Low"].values.astype(float)
    closes = df["Close"].values.astype(float)
    n = len(df)

    in_trade = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    entry_date = None
    max_price_reached = 0.0
    pending_setup = None
    last_entry_idx = -99

    for i in range(BASE_MONTHS + 4, n):
      current_date = df.index[i]
      curr_open = float(opens[i])
      curr_high = float(highs[i])
      curr_low = float(lows[i])

      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          if curr_low <= stop_loss and curr_high >= take_profit:
            is_win = False
          else:
            is_win = curr_high >= take_profit

          pnl_pct = (
              ((take_profit - entry_price) / entry_price) * 100.0
              if is_win
              else ((stop_loss - entry_price) / entry_price) * 100.0
          )
          max_profit_pct = (
              ((max_price_reached - entry_price) / entry_price) * 100.0
          )

          if entry_date.year in [2025, 2026] or current_date.year in [
              2025,
              2026,
          ]:
            trades.append({
                "Symbol": clean_sym,
                "Signal_Month": sig_month_str,
                "Entry_Date": entry_date.strftime("%b %Y"),
                "Entry_Open": round(entry_price, 2),
                "Base_High": b_high,
                "Base_Width_%": b_width,
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
            })
          in_trade = False
        continue

      if pending_setup is not None:
        b_high, b_low, b_width, sig_month_str = pending_setup
        pending_setup = None

        if curr_open > 0 and (i - last_entry_idx) >= BASE_MONTHS:
          entry_price = curr_open
          stop_loss = entry_price * (1.0 - STOP_LOSS_PCT)
          take_profit = entry_price * (1.0 + TAKE_PROFIT_PCT)
          entry_date = current_date
          max_price_reached = curr_high
          in_trade = True
          last_entry_idx = i

          if curr_low <= stop_loss or curr_high >= take_profit:
            if curr_low <= stop_loss and curr_high >= take_profit:
              is_win = False
            else:
              is_win = curr_high >= take_profit

            pnl_pct = (
                ((take_profit - entry_price) / entry_price) * 100.0
                if is_win
                else ((stop_loss - entry_price) / entry_price) * 100.0
            )
            max_profit_pct = (
                ((max_price_reached - entry_price) / entry_price) * 100.0
            )

            if entry_date.year in [2025, 2026] or current_date.year in [
                2025,
                2026,
            ]:
              trades.append({
                  "Symbol": clean_sym,
                  "Signal_Month": sig_month_str,
                  "Entry_Date": entry_date.strftime("%b %Y"),
                  "Entry_Open": round(entry_price, 2),
                  "Base_High": b_high,
                  "Base_Width_%": b_width,
                  "Max_Profit_%": round(max_profit_pct, 2),
                  "Result": "WIN" if is_win else "LOSS",
                  "PnL_%": round(pnl_pct, 2),
              })
            in_trade = False
        continue

      if i + 1 < n and (i - last_entry_idx) >= BASE_MONTHS:
        ok, b_high, b_low, b_width = check_base_accumulation_breakout(
            opens, highs, lows, closes, i
        )
        if ok:
          pending_setup = (
              b_high,
              b_low,
              b_width,
              df.index[i].strftime("%b %Y"),
          )

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 4. RUN & EXPORT TO GOOGLE SHEET
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  for sym in STOCKS:
    t = backtest_base_accumulation(sym)
    if t:
      all_trades.extend(t)

if len(all_trades) > 0:
  df_res = pd.DataFrame(all_trades)
  df_res.replace([np.inf, -np.inf], np.nan, inplace=True)
  df_res = df_res.fillna("")

  total = len(df_res)
  wins = len(df_res[df_res["Result"] == "WIN"])
  win_rate = round((wins / total) * 100, 2)
  total_pnl = round(df_res["PnL_%"].sum(), 2)

  print(f"\n=======================================================")
  print(f" MONTHLY BASE ACCUMULATION RESULTS (2025-2026 | <=45% BASE)")
  print(f"=======================================================")
  print(f" Total Trades : {total}")
  print(f" Wins         : {wins} | Losses: {total - wins}")
  print(f" Win Rate     : {win_rate}%")
  print(f" Total PnL    : {total_pnl}%")
  print(f"=======================================================")

  tab_name = "MONTHLY_BASE_ACCUM_LOG"
  try:
    ws = (
        sh.worksheet(tab_name)
        if tab_name in [x.title for x in sh.worksheets()]
        else sh.add_worksheet(tab_name, 2000, 12)
    )
    ws.clear()
    ws.append_row([
        "MONTHLY BASE ACCUMULATION BACKTEST (<=45% BASE, 2025-2026) |"
        f" {datetime.now(IST).strftime('%d-%b-%Y')}"
    ])
    ws.append_row(list(df_res.columns))
    ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
    print(f"✅ Data successfully saved to '{tab_name}' tab in Google Sheet!")
  except Exception as e:
    print(f"❌ Sheet update error: {e}")
else:
  print("⚠️ Koi trade nahi mili.")
    
