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
    "=== V115.0: MOTHER CANDLE + SWING LOW CANDLE CLUBBED CONFIRMATION ENGINE"
    " ===",
    flush=True,
)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIGURATION =====
BACKTEST_YEARS = 2
TARGET_PCT = 0.10  # 10% Target
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


ws_watchlist = sh.worksheet("Watchlist")
ws_summary = get_or_create_sheet("Backtest_Summary")
ws_trades = get_or_create_sheet("High_Prob_Trade_Logs")


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


# ===== 🎯 CANDLE CLUBBING & SWING LOW CONFIRMATION AUDITOR 🎯 =====
def audit_swing_low_confirmations(df, swing_low_idx):
    if swing_low_idx + 1 >= len(df):
        return False, "No Data"

    # Single Candle Details at Swing Low
    c0_open = df.iloc[swing_low_idx]["Open"]
    c0_high = df.iloc[swing_low_idx]["High"]
    c0_low = df.iloc[swing_low_idx]["Low"]
    c0_close = df.iloc[swing_low_idx]["Close"]
    c0_range = max(0.001, c0_high - c0_low)
    c0_body = abs(c0_close - c0_open)
    c0_lwick = min(c0_open, c0_close) - c0_low

    # Single Candle Checks
    is_hammer = (c0_lwick / c0_range >= 0.40) and (c0_close >= c0_open)

    # 2-Day Candle Clubbing (Merge Swing Low Day + Next Day)
    c1_open = df.iloc[swing_low_idx + 1]["Open"]
    c1_high = df.iloc[swing_low_idx + 1]["High"]
    c1_low = df.iloc[swing_low_idx + 1]["Low"]
    c1_close = df.iloc[swing_low_idx + 1]["Close"]

    club_open = c0_open
    club_high = max(c0_high, c1_high)
    club_low = min(c0_low, c1_low)
    club_close = c1_close
    club_range = max(0.001, club_high - club_low)
    club_body = abs(club_close - club_open)
    club_lwick = min(club_open, club_close) - club_low

    # Combined Patterns
    is_clubbed_bullish_hammer = (club_lwick / club_range >= 0.35) and (
        club_close > club_open
    )
    is_engulfing = (
        (c0_close < c0_open)
        and (c1_close > c1_open)
        and (c1_close >= c0_open)
        and (c1_open <= c0_close)
    )
    is_tweezer_bottom = (
        abs(c0_low - c1_low) / c0_low <= 0.003
    ) and (c1_close > c1_open)

    # High Probability Match Condition
    if is_engulfing:
        return True, "Bullish Engulfing"
    elif is_clubbed_bullish_hammer:
        return True, "Clubbed Bullish Hammer"
    elif is_hammer:
        return True, "Single Hammer"
    elif is_tweezer_bottom:
        return True, "Tweezer Bottom"

    return False, "Weak / Fake Reversal"


# ===== 🎯 HIGH-PROBABILITY BACKTEST ENGINE 🎯 =====
def backtest_single_stock(df, stock_symbol):
    trades = []
    total_rows = len(df)

    if total_rows < 100:
        return trades

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

        # 2. Swing Low (Lowest point after Mother Candle)
        post_mother_zone = df.iloc[mother_idx:i]
        swing_low_idx_loc = post_mother_zone["Low"].idxmin()
        swing_low_idx = df.index.get_loc(swing_low_idx_loc)

        swing_low_price = df.iloc[swing_low_idx]["Low"]

        # 3. Volume Trendline Check
        vol_phase = df.iloc[mother_idx : swing_low_idx + 1]["Volume"]
        if len(vol_phase) >= 3:
            x = np.arange(len(vol_phase))
            slope, _ = np.polyfit(x, vol_phase.values, 1)
            is_vol_decreasing = slope < 0
        else:
            is_vol_decreasing = False

        # 4. Entry Trigger: Breakout above Mother High
        curr_close = df.iloc[i]["Close"]
        prev_close = df.iloc[i - 1]["Close"]

        is_breakout = (curr_close > mother_high) and (prev_close <= mother_high)

        if is_breakout and is_vol_decreasing:
            # 🎯 5. CHECK SWING LOW CANDLE CLUBBING & CONFIRMATION
            has_high_prob_confirmation, pattern_type = (
                audit_swing_low_confirmations(df, swing_low_idx)
            )

            entry_date = df.index[i].strftime("%Y-%m-%d")
            entry_price = round(mother_high, 2)
            stop_loss = round(swing_low_price, 2)
            target_price = round(entry_price * (1 + TARGET_PCT), 2)

            risk_pct = (entry_price - stop_loss) / entry_price

            if risk_pct > 0.18 or risk_pct <= 0:
                i += 1
                continue

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
                    "High_Prob_Confirmed": has_high_prob_confirmation,
                    "Swing_Low_Pattern": pattern_type,
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


# ===== MAIN BACKTEST EXECUTION =====
stocks = get_watchlist_stocks()
all_trades = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

print(
    f"\n=== BACKTESTING {len(stocks)} STOCKS WITH CANDLE CLUBBING & SWING LOW"
    " AUDIT ===",
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

        stock_trades = backtest_single_stock(stock_df, symbol_clean)
        all_trades.extend(stock_trades)

        time.sleep(0.1)
    except Exception as e:
        pass

if all_trades:
    df_results = pd.DataFrame(all_trades)

    # 1. Overall Results
    total_trades = len(df_results)
    total_wins = len(df_results[df_results["Result"] == "WIN"])

    # 2. High Probability Filtered Results (Where Swing Low Confirmation Was Present)
    high_prob_df = df_results[df_results["High_Prob_Confirmed"] == True]
    hp_total = len(high_prob_df)
    hp_wins = len(high_prob_df[high_prob_df["Result"] == "WIN"])
    hp_losses = len(high_prob_df[high_prob_df["Result"] == "LOSS"])
    hp_win_rate = round((hp_wins / hp_total) * 100, 2) if hp_total > 0 else 0

    print("\n==========================================")
    print("      📊 SWING LOW CONFIRMATION AUDIT     ")
    print("==========================================")
    print(f"Total Base Trades Tested     : {total_trades}")
    print(
        f"High-Probability Filtered Trades : {hp_total} (Filtered out weak SL"
        " trades)"
    )
    print(f"Target Hits (Wins)           : {hp_wins}")
    print(f"Stop Loss Hits (Losses)      : {hp_losses}")
    print(
        f"🎯 High-Probability Win Rate : {hp_win_rate}%  (vs 60.4% base win"
        " rate)"
    )
    print("==========================================\n")

    summary_metrics = [{
        "Total_Tested_Trades": total_trades,
        "Filtered_High_Prob_Trades": hp_total,
        "Wins": hp_wins,
        "Losses": hp_losses,
        "Base_Win_Rate": f"{round((total_wins/total_trades)*100, 2)}%",
        "High_Prob_Win_Rate": f"{hp_win_rate}%",
    }]

    upload_to_sheet(ws_summary, summary_metrics)
    upload_to_sheet(ws_trades, all_trades)
else:
    print("\nNo trades generated.")
    
