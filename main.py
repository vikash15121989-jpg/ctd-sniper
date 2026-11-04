from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER HISTORICAL SWING BREAKOUT BACKTESTER ===")

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
# 2. BACKTEST ENGINE FOR A SINGLE STOCK
# -------------------------------------------------------------
def backtest_historical_swing_breakout(
    symbol,
    min_consolidation_months=3,
    atr_period=14,
    stop_loss_pct=0.07,
    rr_target=3.0,
):
  try:
    ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
    # 10+ Years Monthly Data download
    df = yf.download(
        ticker_symbol, start="2010-01-01", interval="1mo", auto_adjust=False
    )

    if df is None or df.empty:
      return None

    if isinstance(df.columns, pd.MultiIndex):
      df.columns = df.columns.get_level_values(0)

    df.dropna(subset=["Open", "High", "Low", "Close"], inplace=True)
    highs = df["High"].values
    closes = df["Close"].values
    n = len(df)

    if n < (min_consolidation_months * 2 + atr_period):
      return None

    # ATR Calculation for Low ATR Filter
    tr1 = df["High"] - df["Low"]
    tr2 = np.abs(df["High"] - df["Close"].shift(1))
    tr3 = np.abs(df["Low"] - df["Close"].shift(1))
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(window=atr_period).mean()
    df["ATR_Pct"] = (df["ATR"] / df["Close"]) * 100
    median_atr_pct = df["ATR_Pct"].median()

    trades = []
    in_trade = False
    pending_entry = False
    entry_price = 0
    stop_loss = 0
    take_profit = 0
    entry_date = None

    # Main Simulation Loop
    for i in range(min_consolidation_months + atr_period, n):
      current_date = df.index[i].strftime("%Y-%m-%d")
      curr_close = closes[i]
      curr_high = highs[i]
      curr_low = df["Low"].iloc[i]
      curr_open = df["Open"].iloc[i]
      atr_p = df["ATR_Pct"].iloc[i]

      if in_trade:
        if curr_low <= stop_loss:
          pnl_pct = (stop_loss - entry_price) / entry_price * 100
          trades.append({
              "Symbol": symbol,
              "Result": "LOSS",
              "PnL_%": round(pnl_pct, 2),
          })
          in_trade = False
        elif curr_high >= take_profit:
          pnl_pct = (take_profit - entry_price) / entry_price * 100
          trades.append({
              "Symbol": symbol,
              "Result": "WIN",
              "PnL_%": round(pnl_pct, 2),
          })
          in_trade = False
        continue

      if pending_entry:
        entry_price = curr_open
        stop_loss = entry_price * (1 - stop_loss_pct)
        risk = entry_price - stop_loss
        take_profit = entry_price + (risk * rr_target)
        entry_date = current_date
        in_trade = True
        pending_entry = False
        continue

      # Find Historical Unbroken Swing High (Min 3 months left/right valid, max any duration)
      active_resistance = None
      for j in range(
          i - min_consolidation_months, min_consolidation_months, -1
      ):
        is_valid_swing = True
        for k in range(j - min_consolidation_months, j):
          if highs[j] <= highs[k]:
            is_valid_swing = False
            break

        if is_valid_swing:
          ever_broken = False
          for k in range(j + 1, i):
            if highs[k] > highs[j]:
              ever_broken = True
              break
          if not ever_broken:
            active_resistance = highs[j]
            break

      # Check Breakout Condition + Low ATR Filter
      if (
          active_resistance is not None
          and curr_close > active_resistance
          and pd.notna(atr_p)
          and atr_p <= median_atr_pct
      ):
        pending_entry = True

    if not trades:
      return None

    t_df = pd.DataFrame(trades)
    wins = len(t_df[t_df["Result"] == "WIN"])
    total = len(t_df)
    win_rate = round((wins / total) * 100, 2)
    total_pnl = round(t_df["PnL_%"].sum(), 2)

    return {
        "Symbol": symbol,
        "Total_Trades": total,
        "Wins": wins,
        "Losses": total - wins,
        "Win_Rate_%": win_rate,
        "Total_PnL_%": total_pnl,
    }

  except Exception as e:
    return None


# -------------------------------------------------------------
# 3. RUN ACROSS ALL WATCHLIST STOCKS
# -------------------------------------------------------------
summary_results = []

if len(STOCKS) > 0:
  for idx, sym in enumerate(STOCKS, 1):
    print(f"[{idx}/{len(STOCKS)}] Backtesting {sym}...")
    res = backtest_historical_swing_breakout(sym)
    if res:
      summary_results.append(res)

# -------------------------------------------------------------
# 4. EXPORT TO GOOGLE SHEETS (`BACKTEST_RESULTS` TAB)
# -------------------------------------------------------------
if len(summary_results) > 0:
  df_sum = pd.DataFrame(summary_results).sort_values(
      by="Total_PnL_%", ascending=False
  )
  rows_backtest = df_sum.values.tolist()

  try:
    ws_bt = (
        sh.worksheet("BACKTEST_RESULTS")
        if "BACKTEST_RESULTS" in [x.title for x in sh.worksheets()]
        else sh.add_worksheet("BACKTEST_RESULTS", 2000, 10)
    )
    ws_bt.clear()
    ws_bt.append_row([
        "HISTORICAL SWING BREAKOUT BACKTEST SUMMARY |"
        f" {datetime.now(IST).strftime('%d-%b-%Y %H:%M')}"
    ])
    ws_bt.append_row(
        ["Stock Symbol", "Total Trades", "Wins", "Losses", "Win Rate %", "Total PnL %"]
    )
    ws_bt.append_rows(rows_backtest, value_input_option="USER_ENTERED")
    print("✅ Sheet 'BACKTEST_RESULTS' updated successfully with backtest data!")
  except Exception as e:
    print(f"Error updating Google Sheet: {e}")
else:
  print("⚠️ No trades found across watchlist stocks to export.")
    
