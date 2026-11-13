from datetime import datetime
import json
import os
import gspread
import numpy as np
import pandas as pd
import pytz
import yfinance as yf

IST = pytz.timezone("Asia/Kolkata")
print("=== CTD_SNIPER FIXED DUAL BACKTESTING ENGINE (2025-2026) ===")

# 1. LOAD WATCHLIST
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
  STOCKS = ["RELIANCE", "TCS", "INFY", "SBIN", "TATAMOTORS"]


def run_backtest_strict(logic_type="RECENT"):
  all_trades = []
  min_consolidation_months = 3

  for symbol in STOCKS:
    try:
      ticker_symbol = f"{symbol}.NS" if not symbol.endswith(".NS") else symbol
      df = yf.download(
          ticker_symbol,
          start="2022-01-01",
          interval="1mo",
          auto_adjust=False,
          progress=False,
      )
      if df is None or df.empty or len(df) < 20:
        continue
      if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
      df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)

      highs = df["High"].values
      lows = df["Low"].values
      closes = df["Close"].values
      dates = df.index
      n = len(df)

      start_idx = min_consolidation_months * 2 + 2
      end_idx = n - 1

      for i in range(start_idx, end_idx):
        signal_date = dates[i]
        if signal_date.year < 2025:
          continue

        curr_close = closes[i]
        prev_close = closes[i - 1]
        active_resistance = None

        if logic_type == "RECENT":
          # 1. IMMEDIATE / RECENT SWING HIGH (Scans backwards from i-1)
          for j in range(i - 1, min_consolidation_months - 1, -1):
            is_valid = True
            for k in range(j - min_consolidation_months, j):
              if highs[j] <= highs[k]:
                is_valid = False
                break
            if not is_valid:
              continue
            for k in range(j + 1, j + min_consolidation_months + 1):
              if highs[j] <= highs[k]:
                is_valid = False
                break
            if not is_valid:
              continue
            ever_broken = False
            for k in range(j + 1, i):
              if highs[k] > highs[j]:
                ever_broken = True
                break
            if ever_broken:
              continue
            active_resistance = highs[j]
            break

        else:
          # 2. MAJOR SWING HIGH (Scans within window i - min_consolidation)
          for j in range(
              i - min_consolidation_months, min_consolidation_months - 1, -1
          ):
            is_valid = True
            for k in range(j - min_consolidation_months, j):
              if highs[j] <= highs[k]:
                is_valid = False
                break
            if not is_valid:
              continue
            for k in range(j + 1, j + min_consolidation_months + 1):
              if highs[j] <= highs[k]:
                is_valid = False
                break
            if not is_valid:
              continue
            ever_broken = False
            for k in range(j + 1, i):
              if highs[k] > highs[j]:
                ever_broken = True
                break
            if ever_broken:
              continue
            active_resistance = highs[j]
            break

        if (
            active_resistance is not None
            and curr_close > active_resistance
            and prev_close <= active_resistance
        ):
          entry_price = curr_close
          stop_loss = entry_price * 0.93  # -7%
          take_profit = entry_price * 1.14  # +14%
          entry_month_date = dates[i + 1]

          trade_result = "OPEN"
          exit_date = None
          pnl_percent = 0.0

          for m in range(i + 1, n):
            m_high = highs[m]
            m_low = lows[m]
            hit_tp = m_high >= take_profit
            hit_sl = m_low <= stop_loss

            if hit_sl:
              trade_result = "LOSS"
              pnl_percent = -7.0
              exit_date = dates[m]
              break
            elif hit_tp:
              trade_result = "WIN"
              pnl_percent = 14.0
              exit_date = dates[m]
              break

          if trade_result != "OPEN":
            all_trades.append({
                "Symbol": symbol,
                "Signal_Month": signal_date.strftime("%b %Y"),
                "Entry_Month": entry_month_date.strftime("%b %Y"),
                "Trigger_Price": round(entry_price, 2),
                "Resistance": round(active_resistance, 2),
                "Result": trade_result,
                "Return_%": pnl_percent,
                "Exit_Month": (
                    exit_date.strftime("%b %Y") if exit_date else "Ongoing"
                ),
            })
    except Exception:
      continue

  return all_trades


# Run both distinct logics
for logic in ["RECENT", "MAJOR"]:
  print(f"\n🚀 Running Strict {logic} Swing Backtest...")
  trades = run_backtest_strict(logic)

  if len(trades) > 0:
    df_bt = pd.DataFrame(trades)
    total_trades = len(df_bt)
    wins = len(df_bt[df_bt["Result"] == "WIN"])
    losses = len(df_bt[df_bt["Result"] == "LOSS"])
    win_rate = (wins / total_trades) * 100 if total_trades > 0 else 0
    total_return = df_bt["Return_%"].sum()

    print(
        f"📊 {logic} RESULTS -> Trades: {total_trades} | Win Rate:"
        f" {win_rate:.2f}% | Return: {total_return:.2f}%"
    )

    tab_name = f"BT_STRICT_{logic}_2025"
    try:
      ws = (
          sh.worksheet(tab_name)
          if tab_name in [x.title for x in sh.worksheets()]
          else sh.add_worksheet(tab_name, 2000, 10)
      )
      ws.clear()
      ws.append_row([
          f"STRICT {logic} SWING BACKTEST (2025-2026) | Generated:"
          f" {datetime.now(IST).strftime('%d-%b-%Y')}"
      ])
      ws.append_row([
          f"Total Trades: {total_trades} | Wins: {wins} | Losses: {losses} | Win"
          f" Rate: {win_rate:.2f}% | Return: {total_return:.2f}%"
      ])
      ws.append_row([])
      ws.append_row([
          "Symbol",
          "Signal Month",
          "Entry Month",
          "Trigger Price",
          "Resistance",
          "Result",
          "Return %",
          "Exit Month",
      ])
      ws.append_rows(df_bt.values.tolist(), value_input_option="USER_ENTERED")
      print(f"✅ Saved correctly to tab '{tab_name}'")
    except Exception as e:
      print(f"Sheet error: {e}")
                  
