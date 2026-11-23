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
    "=== CTD_SNIPER MONTHLY SWING: >=3M LL/LH -> PREV HIGH BREAK -> MIN LOW"
    " INTACT -> 2% BREAKOUT ENTRY ==="
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


# -------------------------------------------------------------
# 2. BACKTEST LOGIC:
#    Swing High (j) -> >=3 LL/LH -> Prev High Break (b_idx)
#    -> Track min(lows[j+1 : b_idx+1]) -> Must stay intact until 2% close breakout
#    -> Enter next month Open (t+1)
# -------------------------------------------------------------
def backtest_swing_minlow_2pct(
    symbol,
    stop_loss_pct=0.07,
    take_profit_pct=0.14,
    min_pullback_months=3,
):
  trades = []
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
      return trades

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)

    highs = df["High"].values
    lows = df["Low"].values
    closes = df["Close"].values
    opens = df["Open"].values
    dates = df.index
    n = len(df)

    in_trade = False
    entry_price = 0.0
    stop_loss = 0.0
    take_profit = 0.0
    breakout_date = None
    entry_date = None
    swing_high_val = 0.0
    guard_min_low = 0.0
    max_price_reached = 0.0

    # Active candidate state for a single valid swing high
    # candidate = {
    #     "j": swing_idx,
    #     "swing_high": float,
    #     "prev_high_broken": bool,
    #     "min_low": float or None,
    #     "invalid": bool,
    # }
    candidate = None
    waiting_entry_idx = None
    start_idx = min_pullback_months + 3

    for i in range(start_idx, n):
      current_date = dates[i]
      curr_high = highs[i]
      curr_low = lows[i]
      curr_close = closes[i]
      curr_open = opens[i]

      # A. Monitor open trade SL / TP
      if in_trade:
        if curr_high > max_price_reached:
          max_price_reached = curr_high

        if curr_low <= stop_loss or curr_high >= take_profit:
          is_win = curr_high >= take_profit
          pnl_pct = (
              (take_profit - entry_price) / entry_price * 100
              if is_win
              else (stop_loss - entry_price) / entry_price * 100
          )
          max_profit_pct = (
              (max_price_reached - entry_price) / entry_price
          ) * 100

          if (
              (breakout_date and breakout_date.year in [2025, 2026])
              or (entry_date and entry_date.year in [2025, 2026])
              or (current_date.year in [2025, 2026])
          ):
            tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{symbol}"
            sheet_link_formula = f'=HYPERLINK("{tv_url}", "{symbol} Chart")'

            trades.append({
                "Symbol": symbol,
                "Breakout_Month": (
                    breakout_date.strftime("%b %Y") if breakout_date else ""
                ),
                "Entry_Month": (
                    entry_date.strftime("%b %Y") if entry_date else ""
                ),
                "Swing_High": round(swing_high_val, 2),
                "Guard_Min_Low": round(guard_min_low, 2),
                "Entry_Price": round(entry_price, 2),
                "Max_Profit_%": round(max_profit_pct, 2),
                "Result": "WIN" if is_win else "LOSS",
                "PnL_%": round(pnl_pct, 2),
                "TradingView_Chart": sheet_link_formula,
            })

          in_trade = False
          waiting_entry_idx = None
          candidate = None
        continue

      # B. Enter at next month's Open after valid 2% breakout
      if waiting_entry_idx is not None and i == waiting_entry_idx:
        entry_price = curr_open
        stop_loss = entry_price * (1 - stop_loss_pct)
        take_profit = entry_price * (1 + take_profit_pct)
        entry_date = current_date
        max_price_reached = curr_high
        in_trade = True
        waiting_entry_idx = None
        candidate = None
        continue

      # C. Detect a fresh Swing High followed by >= 3 months of LL & LH
      #    ending at previous month (i - 1) if no active candidate is running
      if waiting_entry_idx is None and candidate is None:
        j = i - 1 - min_pullback_months
        if j >= 1:
          is_swing_high = highs[j] > highs[j - 1] and highs[j] >= highs[j + 1]
          if is_swing_high:
            # Check consecutive LL & LH across months (j+1) .. (i-1)
            ll_lh_ok = True
            for k in range(j + 1, i):
              if not (highs[k] < highs[k - 1] and lows[k] < lows[k - 1]):
                ll_lh_ok = False
                break

            if ll_lh_ok:
              candidate = {
                  "j": j,
                  "swing_high": float(highs[j]),
                  "prev_high_broken": False,
                  "min_low": None,
                  "invalid": False,
              }

      # D. Evaluate active swing high candidate at month i
      if waiting_entry_idx is None and candidate is not None:
        j = candidate["j"]
        s_high = candidate["swing_high"]

        # Step 1: If previous month high hasn't broken yet after the 3+ LL/LH phase,
        # check if current month i breaks previous month's high
        if not candidate["prev_high_broken"]:
          if curr_high > highs[i - 1]:
            candidate["prev_high_broken"] = True
            # Minimum low from swing high (j+1) up to this break month (i)
            candidate["min_low"] = float(np.min(lows[j + 1 : i + 1]))

        # Step 2: Once previous high has broken, check if min_low is breached
        if candidate["prev_high_broken"] and not candidate["invalid"]:
          if curr_low < candidate["min_low"]:
            # Swing high becomes invalid if price goes below min_low before breakout
            candidate["invalid"] = True
            candidate = None
            continue

          # Check if current month gives >= 2% close breakout above swing high
          if curr_close >= s_high * 1.02 and i + 1 < n:
            swing_high_val = s_high
            guard_min_low = candidate["min_low"]
            breakout_date = current_date
            waiting_entry_idx = i + 1

    return trades

  except Exception:
    return []


# -------------------------------------------------------------
# 3. RUN & EXPORT TO GOOGLE SHEETS
# -------------------------------------------------------------
all_trades = []
if len(STOCKS) > 0:
  print(
      "🚀 Running >=3M LL/LH -> Prev High Break -> Min Low Intact -> 2%"
      " Breakout Backtest..."
  )
  for sym in STOCKS:
    t = backtest_swing_minlow_2pct(sym)
    if t:
      all_trades.extend(t)

  if len(all_trades) > 0:
    df_res = pd.DataFrame(all_trades)
    wins = len(df_res[df_res["Result"] == "WIN"])
    total = len(df_res)
    win_rate = round((wins / total) * 100, 2) if total > 0 else 0
    total_pnl = round(df_res["PnL_%"].sum(), 2)

    print(f"\n=======================================================")
    print(f" SWING MIN-LOW INTACT + 2% BREAKOUT LOG (2025-2026)")
    print(f"=======================================================")
    print(f" Total Trades : {total}")
    print(f" Wins         : {wins} | Losses: {total - wins}")
    print(f" Win Rate     : {win_rate}%")
    print(f" Total PnL    : {total_pnl}%")
    print(f"=======================================================")

    tab_name = "BT_SWING_MINLOW_2PCT_LOG"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 12)
      )
      ws.clear()
      ws.append_row([
          ">=3M LL/LH -> PREV HIGH BREAK -> MIN LOW INTACT -> 2% BREAKOUT"
          f" ENTRY (2025-2026) | {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws.append_row([
          f"Total Trades: {total} | Wins: {wins} | Win Rate: {win_rate}% |"
          f" Total PnL: {total_pnl}%"
      ])
      ws.append_row([])
      ws.append_row([
          "Symbol",
          "Breakout Month",
          "Entry Month",
          "Swing High",
          "Guard Min Low",
          "Entry Price",
          "Max Profit %",
          "Result",
          "PnL %",
          "TradingView Link",
      ])
      ws.append_rows(df_res.values.tolist(), value_input_option="USER_ENTERED")
      print(f"✅ Saved to '{tab_name}' tab in Google Sheet successfully!")
    except Exception as e:
      print(f"Sheet update error: {e}")
  else:
    print("⚠️ Is criteria par koi trade nahi mili.")
else:
  print("⚠️ Watchlist is empty.")
