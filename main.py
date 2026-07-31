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

print("=== V114.0: MOTHER CANDLE + VOLUME TRENDLINE BACKTEST ENGINE ===", flush=True)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIGURATION =====
BACKTEST_YEARS = 2  # 2 saal ka historical data backtest hoga
TARGET_PCT = 0.10  # Minimum 10% Move Target
MIN_AVG_VOLUME = 100000
MIN_AVG_TURNOVER_CR = 2

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


# Worksheets Setup
ws_watchlist = sh.worksheet("Watchlist")
ws_summary = get_or_create_sheet("Backtest_Summary")
ws_trades = get_or_create_sheet("Backtest_Trade_Logs")


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


# ===== 🎯 MOTHER CANDLE + VOLUME TRENDLINE BACKTEST LOGIC 🎯 =====
def backtest_single_stock(df, stock_symbol):
    trades = []
    total_rows = len(df)

    if total_rows < 100:
        return trades

    # Loop through historical daily candles
    i = 60
    while i < total_rows - 5:
        # 1. Mother Candle (Pichhle 60 dino ka Highest Swing High Point)
        lookback_window = df.iloc[i - 60 : i]
        mother_idx_loc = lookback_window["High"].idxmax()
        mother_idx = df.index.get_loc(mother_idx_loc)

        # Mother Candle aur aaj ke din ke beech kam se kam 5 dino ka gap hona chahiye
        if (i - mother_idx) < 5:
            i += 1
            continue

        mother_high = df.iloc[mother_idx]["High"]

        # 2. Swing Low (Mother Candle ke baad se lekar aaj tak ka Lowest Point)
        post_mother_zone = df.iloc[mother_idx:i]
        swing_low_idx_loc = post_mother_zone["Low"].idxmin()
        swing_low_idx = df.index.get_loc(swing_low_idx_loc)

        swing_low_price = df.iloc[swing_low_idx]["Low"]

        # 3. Volume Trendline Check (Mother Candle High Day se Swing Low Day tak)
        vol_phase = df.iloc[mother_idx : swing_low_idx + 1]["Volume"]

        if len(vol_phase) >= 3:
            # Linear Fit se Volume ki Slope nikalte hain
            x = np.arange(len(vol_phase))
            slope, _ = np.polyfit(x, vol_phase.values, 1)
            is_vol_decreasing = (
                slope < 0
            )  # Negative slope = Volume Trendline Decreasing
        else:
            is_vol_decreasing = False

        # 4. Entry Trigger: Price breaks above Mother Candle High
        curr_close = df.iloc[i]["Close"]
        prev_close = df.iloc[i - 1]["Close"]

        is_breakout = (curr_close > mother_high) and (prev_close <= mother_high)

        # Conditions Matched -> Take Trade
        if is_breakout and is_vol_decreasing:
            entry_date = df.index[i].strftime("%Y-%m-%d")
            entry_price = round(mother_high, 2)
            stop_loss = round(swing_low_price, 2)
            target_price = round(entry_price * (1 + TARGET_PCT), 2)  # 10% Target

            risk_pct = (entry_price - stop_loss) / entry_price

            # Unrealistic Stop loss filter (> 18% ya <= 0%)
            if risk_pct > 0.18 or risk_pct <= 0:
                i += 1
                continue

            # Forward Testing to check outcome (Target vs Stop Loss)
            trade_result = None
            exit_date = None
            exit_price = None

            for j in range(i + 1, total_rows):
                day_high = df.iloc[j]["High"]
                day_low = df.iloc[j]["Low"]

                # Target Hit First (+10% Move)
                if day_high >= target_price:
                    trade_result = "WIN"
                    exit_price = target_price
                    exit_date = df.index[j].strftime("%Y-%m-%d")
                    break
                # Stop Loss Hit First
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
                })
                i = j  # Skip forward to avoid duplicate entries on same rally
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


# ===== MAIN BACKTEST EXECUTION LOOP =====
stocks = get_watchlist_stocks()
all_trades = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

print(
    f"\n=== BACKTESTING {len(stocks)} STOCKS FROM GOOGLE SHEET WATCHLIST ===",
    flush=True,
)

for i, stock in enumerate(stocks):
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

        stock_df["Avg_Vol"] = stock_df["Volume"].rolling(window=20).mean()
        stock_df["Avg_Turnover"] = (
            stock_df["Close"] * stock_df["Volume"]
        ).rolling(window=20).mean() / 10000000

        curr_idx = len(stock_df) - 1
        avg_vol = stock_df.iloc[curr_idx]["Avg_Vol"]
        avg_turnover = stock_df.iloc[curr_idx]["Avg_Turnover"]

        if (
            pd.isna(avg_vol)
            or pd.isna(avg_turnover)
            or avg_vol < MIN_AVG_VOLUME
            or avg_turnover < MIN_AVG_TURNOVER_CR
        ):
            continue

        stock_trades = backtest_single_stock(stock_df, symbol_clean)
        all_trades.extend(stock_trades)

        time.sleep(0.1)
    except Exception as e:
        pass

# ===== CALCULATE BACKTEST STATISTICS =====
summary_metrics = []

if all_trades:
    df_results = pd.DataFrame(all_trades)

    total_trades = len(df_results)
    wins = len(df_results[df_results["Result"] == "WIN"])
    losses = len(df_results[df_results["Result"] == "LOSS"])

    win_rate = round((wins / total_trades) * 100, 2)
    loss_rate = round((losses / total_trades) * 100, 2)

    avg_win = round(
        df_results[df_results["Result"] == "WIN"]["PnL_Pct"].mean(), 2
    )
    avg_loss = round(
        df_results[df_results["Result"] == "LOSS"]["PnL_Pct"].mean(), 2
    )

    summary_metrics = [{
        "Total_Stocks_Tested": len(stocks),
        "Total_Trades_Generated": total_trades,
        "Successful_Trades (10%+ Target)": wins,
        "Failed_Trades (SL Hit)": losses,
        "Win_Rate_Pct": f"{win_rate}%",
        "Loss_Rate_Pct": f"{loss_rate}%",
        "Avg_Profit_Per_Win": f"+{avg_win}%",
        "Avg_Loss_Per_Trade": f"{avg_loss}%",
    }]

    print("\n==========================================")
    print("      📊 FINAL BACKTEST SUMMARY REPORT    ")
    print("==========================================")
    print(f"Total Trades         : {total_trades}")
    print(f"10%+ Target Hits     : {wins} ({win_rate}%)")
    print(f"Stop Loss Hits       : {losses} ({loss_rate}%)")
    print("==========================================\n")

    # Upload Detailed Logs & Summary to Google Sheet
    upload_to_sheet(ws_summary, summary_metrics)
    upload_to_sheet(ws_trades, all_trades)
else:
    print("\nNo trades generated based on the strategy criteria.")
    upload_to_sheet(
        ws_summary, [], default_msg="No Trades Met Strategy Criteria"
    )
    upload_to_sheet(ws_trades, [])

print("\n=== BACKTEST ENGINE EXECUTION COMPLETED ===", flush=True)
