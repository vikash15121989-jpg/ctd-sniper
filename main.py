import json
import os
import time
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print(
    "=== V120.0: MOTHER CANDLE + VOL TRENDLINE + SUPPORT ZONE REVERSAL"
    " BACKTEST ===",
    flush=True,
)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

BACKTEST_YEARS = 2
TARGET_PCT = 0.10  # 10% Target
ZONE_TOLERANCE = 0.02  # 2% Tolerance for Support Level Test

END_DATE = (datetime.now() + timedelta(days=1)).date()
START_DATE = END_DATE - timedelta(days=BACKTEST_YEARS * 365)

# ===== GOOGLE SHEETS SETUP =====
gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")


def get_or_create_sheet(title):
    try:
        return sh.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=title, rows="1000", cols="20")


ws_watchlist = sh.worksheet("Watchlist")
ws_summary = get_or_create_sheet("Support_Zone_Summary")
ws_trades = get_or_create_sheet("Support_Zone_Trades")


def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [
        s.strip().upper()
        for s in stocks
        if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]
    ]
    return [
        s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s
        for s in stocks
    ]


def flatten_yf_columns(df):
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(col).strip().capitalize() for col in df.columns]
    if "Close" not in df.columns and "Adj close" in df.columns:
        df["Close"] = df["Adj close"]
    df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    return df


def backtest_single_stock(df, stock_symbol):
    trades = []
    total_rows = len(df)

    if total_rows < 120:
        return trades

    i = 100
    while i < total_rows - 5:
        # 1. Mother Candle (Pichhle 60 days ka High)
        lookback_window = df.iloc[i - 60 : i]
        mother_idx_loc = lookback_window["High"].idxmax()
        mother_idx = df.index.get_loc(mother_idx_loc)

        if (i - mother_idx) < 5:
            i += 1
            continue

        mother_high = df.iloc[mother_idx]["High"]

        # 2. Swing Low (Mother Candle ke baad ka sabse lower price)
        post_mother_zone = df.iloc[mother_idx:i]
        swing_low_idx_loc = post_mother_zone["Low"].idxmin()
        swing_low_idx = df.index.get_loc(swing_low_idx_loc)
        swing_low_price = df.iloc[swing_low_idx]["Low"]

        # 🎯 3. PREVIOUS SUPPORT ZONE CHECK (Prev Swing High or Prev Swing Low)
        # Mother Candle se pehle ke 60 days me Key Levels dhundho
        history_window = df.iloc[
            max(0, mother_idx - 60) : max(1, mother_idx - 5)
        ]

        if len(history_window) < 10:
            i += 1
            continue

        prev_swing_high = history_window["High"].max()
        prev_swing_low = history_window["Low"].min()

        # Check agar Current Swing Low kisi Support Level (Prev High / Low) ke pass hai
        near_prev_high = (
            abs(swing_low_price - prev_swing_high) / prev_swing_high
        ) <= ZONE_TOLERANCE
        near_prev_low = (
            abs(swing_low_price - prev_swing_low) / prev_swing_low
        ) <= ZONE_TOLERANCE

        is_at_support_zone = near_prev_high or near_prev_low

        if not is_at_support_zone:
            i += 1
            continue

        # 4. Volume Trendline Check (Mother High se Swing Low tak)
        vol_phase = df.iloc[mother_idx : swing_low_idx + 1]["Volume"]
        if len(vol_phase) >= 3:
            slope, _ = np.polyfit(np.arange(len(vol_phase)), vol_phase.values, 1)
            is_vol_decreasing = slope < 0
        else:
            is_vol_decreasing = False

        if not is_vol_decreasing:
            i += 1
            continue

        # 5. Mother Candle Breakout Trigger
        curr_close = df.iloc[i]["Close"]
        prev_close = df.iloc[i - 1]["Close"]

        is_breakout = (curr_close > mother_high) and (prev_close <= mother_high)

        if is_breakout:
            entry_price = round(mother_high, 2)
            stop_loss = round(swing_low_price, 2)
            risk_pct = (entry_price - stop_loss) / entry_price

            if risk_pct > 0.18 or risk_pct <= 0.01:
                i += 1
                continue

            entry_date = df.index[i].strftime("%Y-%m-%d")
            target_price = round(entry_price * (1 + TARGET_PCT), 2)

            trade_result = None
            exit_date = None
            exit_price = None

            for j in range(i + 1, total_rows):
                day_high = df.iloc[j]["High"]
                day_low = df.iloc[j]["Low"]

                if day_high >= target_price:
                    trade_result = "WIN"
                    exit_price = target_price
                    exit_date = df.index[j].strftime("%Y-%m-%d")
                    break
                elif day_low <= stop_loss:
                    trade_result = "LOSS"
                    exit_price = stop_loss
                    exit_date = df.index[j].strftime("%Y-%m-%d")
                    break

            if trade_result:
                trades.append({
                    "Stock": stock_symbol,
                    "Entry_Date": entry_date,
                    "Entry_Price": entry_price,
                    "SL_Price": stop_loss,
                    "Target_Price": target_price,
                    "Exit_Date": exit_date,
                    "Exit_Price": exit_price,
                    "Result": trade_result,
                    "PnL_Pct": 10.0
                    if trade_result == "WIN"
                    else round(-risk_pct * 100, 2),
                    "Risk_Pct": round(risk_pct * 100, 2),
                })
                i = j
            else:
                i += 1
        else:
            i += 1

    return trades


def upload_to_sheet(ws, data_list):
    try:
        ws.batch_clear(["A:Z"])
        time.sleep(1)
        if data_list:
            df = pd.DataFrame(data_list)
            df_json = json.loads(df.to_json(orient="split"))
            ws.update(
                values=[df_json["columns"]] + df_json["data"], range_name="A1"
            )
    except Exception as e:
        print(f"Sheet Error: {str(e)}", flush=True)


# MAIN EXECUTION
stocks = get_watchlist_stocks()
all_trades = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        if any(k in symbol_clean for k in REJECT_KEYWORDS):
            continue
        stock_df = yf.download(
            stock, start=START_DATE, end=END_DATE, progress=False
        )
        stock_df = flatten_yf_columns(stock_df)
        if not stock_df.empty and len(stock_df) >= 120:
            all_trades.extend(backtest_single_stock(stock_df, symbol_clean))
    except Exception:
        pass

if all_trades:
    trades_df = pd.DataFrame(all_trades)
    total_trades = len(trades_df)
    wins = len(trades_df[trades_df["Result"] == "WIN"])
    losses = len(trades_df[trades_df["Result"] == "LOSS"])
    win_rate = round((wins / total_trades) * 100, 2)

    print("\n===========================================================")
    print("      🎯 SUPPORT ZONE REVERSAL BACKTEST RESULTS             ")
    print("===========================================================")
    print(f"Total Quality Trades : {total_trades}")
    print(f"Wins (10%+ Target)   : {wins} ({win_rate}%)")
    print(f"Losses (SL Hit)      : {losses} ({round(100-win_rate, 2)}%)")
    print("===========================================================\n")

    upload_to_sheet(
        ws_summary,
        [{"Total_Trades": total_trades, "Win_Rate_Pct": f"{win_rate}%"}],
    )
    upload_to_sheet(ws_trades, all_trades)
    
