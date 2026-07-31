import warnings
from datetime import datetime, timedelta
import json
import os
import time
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print(
    "=== V118.0: HIGH PROBABILITY BACKTEST ENGINE (TARGET 80%+ WIN RATE) ===",
    flush=True,
)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIGURATION =====
BACKTEST_YEARS = 2
TARGET_PCT = 0.10  # 10% Minimum Target
MAX_RISK_PCT = 0.08  # 🎯 Rule: Risk <= 8% (Strict SL Range)
MIN_VOL_MULTIPLIER = (
    2.0  # 🎯 Rule: Breakout Volume must be 2.0x of 20-Day Avg Vol
)

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
ws_summary = get_or_create_sheet("High_Prob_80_Summary")
ws_trades = get_or_create_sheet("High_Prob_80_Trades")


def get_watchlist_stocks():
    stocks = ws_watchlist.col_values(1)
    stocks = [
        s.strip().upper()
        for s in stocks
        if s.strip() and s.strip().upper() not in ["STOCK", "SYMBOL", "NAME"]
    ]
    stocks = [
        s + ".NS" if not s.endswith(".NS") and not s.startswith("^") else s
        for s in stocks
    ]
    return stocks


def flatten_yf_columns(df):
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(col).strip().capitalize() for col in df.columns]
    if "Close" not in df.columns:
        if "Adj close" in df.columns:
            df["Close"] = df["Adj close"]
        elif "Adj Close" in df.columns:
            df["Close"] = df["Adj Close"]
    df.dropna(subset=["Open", "High", "Low", "Close", "Volume"], inplace=True)
    return df


# ===== 🎯 HIGH PROBABILITY BACKTEST ENGINE 🎯 =====
def backtest_single_stock(df, stock_symbol):
    trades = []
    total_rows = len(df)

    if total_rows < 100:
        return trades

    # Add 20-Day Average Volume
    df["Avg_Vol_20"] = df["Volume"].rolling(window=20).mean()

    i = 60
    while i < total_rows - 5:
        # 1. Mother Candle (Peak High)
        lookback_window = df.iloc[i - 60 : i]
        mother_idx_loc = lookback_window["High"].idxmax()
        mother_idx = df.index.get_loc(mother_idx_loc)

        if (i - mother_idx) < 5:
            i += 1
            continue

        mother_high = df.iloc[mother_idx]["High"]

        # 2. Swing Low
        post_mother_zone = df.iloc[mother_idx:i]
        swing_low_idx_loc = post_mother_zone["Low"].idxmin()
        swing_low_idx = df.index.get_loc(swing_low_idx_loc)
        swing_low_price = df.iloc[swing_low_idx]["Low"]

        # Filter: Exclude Tweezer Bottom Trap
        if swing_low_idx + 1 < len(df):
            c0_l = df.iloc[swing_low_idx]["Low"]
            c1_l = df.iloc[swing_low_idx + 1]["Low"]
            if (abs(c0_l - c1_l) / c0_l <= 0.003) and (
                df.iloc[swing_low_idx + 1]["Close"]
                > df.iloc[swing_low_idx + 1]["Open"]
            ):
                i += 1
                continue

        # 3. Volume Trendline Check
        vol_phase = df.iloc[mother_idx : swing_low_idx + 1]["Volume"]
        if len(vol_phase) >= 3:
            x = np.arange(len(vol_phase))
            slope, _ = np.polyfit(x, vol_phase.values, 1)
            is_vol_decreasing = slope < 0
        else:
            is_vol_decreasing = False

        # Breakout Candle Metrics
        curr_close = df.iloc[i]["Close"]
        curr_open = df.iloc[i]["Open"]
        curr_high = df.iloc[i]["High"]
        curr_low = df.iloc[i]["Low"]
        curr_vol = df.iloc[i]["Volume"]
        avg_vol_20 = df.iloc[i]["Avg_Vol_20"]
        prev_close = df.iloc[i - 1]["Close"]

        # Breakout Conditions
        is_breakout = (curr_close > mother_high) and (prev_close <= mother_high)

        if is_breakout and is_vol_decreasing:
            entry_price = round(mother_high, 2)
            stop_loss = round(swing_low_price, 2)
            risk_pct = (entry_price - stop_loss) / entry_price

            # 🎯 STRICT 80%+ WIN RATE FILTERS 🎯
            # Filter A: Risk <= 8%
            if risk_pct > MAX_RISK_PCT or risk_pct <= 0.015:
                i += 1
                continue

            # Filter B: Volume Spike >= 2.0x Average Volume
            if pd.isna(avg_vol_20) or (curr_vol < MIN_VOL_MULTIPLIER * avg_vol_20):
                i += 1
                continue

            # Filter C: Strong Green Body (Close in top 25% of candle range)
            c_range = max(0.001, curr_high - curr_low)
            close_position = (curr_close - curr_low) / c_range
            if close_position < 0.75 or curr_close <= curr_open:
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
                pnl = (
                    10.0
                    if trade_result == "WIN"
                    else round(-risk_pct * 100, 2)
                )
                trades.append({
                    "Stock": stock_symbol,
                    "Entry_Date": entry_date,
                    "Entry_Price": entry_price,
                    "SL_Price": stop_loss,
                    "Target_Price": target_price,
                    "Exit_Date": exit_date,
                    "Exit_Price": exit_price,
                    "Result": trade_result,
                    "PnL_Pct": pnl,
                    "Risk_Pct": round(risk_pct * 100, 2),
                    "Vol_Spike_x": round(curr_vol / avg_vol_20, 2),
                })
                i = j
            else:
                i += 1
        else:
            i += 1

    return trades


def upload_to_sheet(ws, data_list, default_msg="No Data"):
    try:
        ws.batch_clear(["A:Z"])
        time.sleep(1)
        if data_list:
            df = pd.DataFrame(data_list)
            df_json = json.loads(df.to_json(orient="split"))
            values = [df_json["columns"]] + df_json["data"]
            ws.update(values=values, range_name="A1")
        else:
            ws.update(values=[[default_msg]], range_name="A1")
    except Exception as e:
        print(f"Sheet Error: {str(e)}", flush=True)


# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
all_trades = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

print(
    f"\n=== BACKTESTING {len(stocks)} STOCKS WITH HIGH-PROBABILITY FILTERS ===",
    flush=True,
)

for stock in stocks:
    try:
        symbol_clean = stock.replace(".NS", "")
        if any(keyword in symbol_clean for keyword in REJECT_KEYWORDS):
            continue

        stock_df = yf.download(
            stock, start=START_DATE, end=END_DATE, progress=False
        )
        stock_df = flatten_yf_columns(stock_df)

        if stock_df.empty or len(stock_df) < 60:
            continue

        stock_trades = backtest_single_stock(stock_df, symbol_clean)
        all_trades.extend(stock_trades)
        time.sleep(0.05)
    except Exception as e:
        pass

if all_trades:
    trades_df = pd.DataFrame(all_trades)

    total_trades = len(trades_df)
    wins = len(trades_df[trades_df["Result"] == "WIN"])
    losses = len(trades_df[trades_df["Result"] == "LOSS"])
    win_rate = round((wins / total_trades) * 100, 2)

    summary_metrics = [{
        "Total_High_Prob_Trades": total_trades,
        "10% Target Hits (Wins)": wins,
        "SL Hits (Losses)": losses,
        "Win_Rate_Pct": f"{win_rate}%",
    }]

    print("\n===========================================================")
    print("      🎯 HIGH PROBABILITY BACKTEST RESULTS (TARGET 80%+)    ")
    print("===========================================================")
    print(f"Total Quality Trades : {total_trades}")
    print(f"10%+ Target Hits     : {wins} ({win_rate}%)")
    print(f"Stop Loss Hits       : {losses} ({round(100-win_rate, 2)}%)")
    print("===========================================================\n")

    upload_to_sheet(ws_summary, summary_metrics)
    upload_to_sheet(ws_trades, all_trades)
    
