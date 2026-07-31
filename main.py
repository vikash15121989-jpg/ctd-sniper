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
    "=== V116.0: MOTHER CANDLE + SWING LOW PATTERN BREAKDOWN AUDITOR ===",
    flush=True,
)
print(f"Run Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

# ===== CONFIGURATION =====
BACKTEST_YEARS = 2
TARGET_PCT = 0.10  # 10% Minimum Target
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
ws_breakdown = get_or_create_sheet("Pattern_Wise_Breakdown")
ws_trades = get_or_create_sheet("All_Trade_Logs_With_Patterns")


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


# ===== 🎯 EXACT SWING LOW PATTERN CLASSIFIER (SINGLE + CLUBBED) 🎯 =====
def classify_swing_low_pattern(df, swing_low_idx):
    if swing_low_idx + 1 >= len(df):
        return "Unknown / Edge Case"

    # Single Candle Details at Swing Low
    c0_open = df.iloc[swing_low_idx]["Open"]
    c0_high = df.iloc[swing_low_idx]["High"]
    c0_low = df.iloc[swing_low_idx]["Low"]
    c0_close = df.iloc[swing_low_idx]["Close"]
    c0_vol = df.iloc[swing_low_idx]["Volume"]

    c0_range = max(0.001, c0_high - c0_low)
    c0_body = abs(c0_close - c0_open)
    c0_lwick = min(c0_open, c0_close) - c0_low
    c0_uwick = c0_high - max(c0_open, c0_close)

    # Next Day Candle (for 2-Day Clubbing)
    c1_open = df.iloc[swing_low_idx + 1]["Open"]
    c1_high = df.iloc[swing_low_idx + 1]["High"]
    c1_low = df.iloc[swing_low_idx + 1]["Low"]
    c1_close = df.iloc[swing_low_idx + 1]["Close"]

    # 2-Day Clubbed Candle Math
    club_open = c0_open
    club_high = max(c0_high, c1_high)
    club_low = min(c0_low, c1_low)
    club_close = c1_close
    club_range = max(0.001, club_high - club_low)
    club_body = abs(club_close - club_open)
    club_lwick = min(club_open, club_close) - club_low
    club_uwick = club_high - max(club_open, club_close)

    # Historical avg vol for volume dry test
    lookback_vol = df.iloc[max(0, swing_low_idx - 20) : swing_low_idx][
        "Volume"
    ].mean()
    is_vol_dry = c0_vol < lookback_vol

    # --- CLASSIFICATION LOGIC ---

    # 1. Bullish Engulfing
    if (
        (c0_close < c0_open)
        and (c1_close > c1_open)
        and (c1_close >= c0_open)
        and (c1_open <= c0_close)
    ):
        return "1. Bullish Engulfing (2-Day Club)"

    # 2. Clubbed Bullish Hammer
    if (club_lwick / club_range >= 0.40) and (club_close > club_open):
        return "2. Clubbed Bullish Hammer (2-Day Club)"

    # 3. Single Hammer / Pinbar
    if (c0_lwick / c0_range >= 0.45) and (c0_uwick / c0_range <= 0.20):
        return "3. Single Day Hammer / Pinbar"

    # 4. Tweezer Bottom
    if (abs(c0_low - c1_low) / c0_low <= 0.003) and (c1_close > c1_open):
        return "4. Tweezer Bottom (Double Low Reversal)"

    # 5. Shooting Star / Upper Wick Rejection (Weakness)
    if (c0_uwick / c0_range >= 0.45) or (club_uwick / club_range >= 0.45):
        return "5. Shooting Star / Upper Wick Rejection"

    # 6. Full Red Body / Strong Bearish Close
    if (c0_close < c0_open) and (c0_body / c0_range >= 0.60):
        return "6. Full Red Body (No Reversal Support)"

    # 7. Volume Dry + Small Body (Doji / Spinning Top)
    if is_vol_dry and (c0_body / c0_range <= 0.30):
        return "7. Volume Dry + Small Body Doji"

    return "8. Normal / Mixed Candle Structure"


# ===== 🎯 BACKTEST EXECUTION =====
def backtest_single_stock(df, stock_symbol):
    trades = []
    total_rows = len(df)

    if total_rows < 100:
        return trades

    i = 60
    while i < total_rows - 5:
        lookback_window = df.iloc[i - 60 : i]
        mother_idx_loc = lookback_window["High"].idxmax()
        mother_idx = df.index.get_loc(mother_idx_loc)

        if (i - mother_idx) < 5:
            i += 1
            continue

        mother_high = df.iloc[mother_idx]["High"]

        post_mother_zone = df.iloc[mother_idx:i]
        swing_low_idx_loc = post_mother_zone["Low"].idxmin()
        swing_low_idx = df.index.get_loc(swing_low_idx_loc)
        swing_low_price = df.iloc[swing_low_idx]["Low"]

        # Volume Trendline Check
        vol_phase = df.iloc[mother_idx : swing_low_idx + 1]["Volume"]
        if len(vol_phase) >= 3:
            x = np.arange(len(vol_phase))
            slope, _ = np.polyfit(x, vol_phase.values, 1)
            is_vol_decreasing = slope < 0
        else:
            is_vol_decreasing = False

        curr_close = df.iloc[i]["Close"]
        prev_close = df.iloc[i - 1]["Close"]

        is_breakout = (curr_close > mother_high) and (prev_close <= mother_high)

        if is_breakout and is_vol_decreasing:
            # Classify Pattern
            pattern_type = classify_swing_low_pattern(df, swing_low_idx)

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
                    "Swing_Low_Pattern": pattern_type,
                })
                i = j
            else:
                i += 1
        else:
            i += 1

    return trades


def upload_to_sheet(ws, df_data):
    try:
        ws.batch_clear(["A:Z"])
        time.sleep(1)
        if not df_data.empty:
            df_json = json.loads(df_data.to_json(orient="split"))
            values = [df_json["columns"]] + df_json["data"]
            ws.update(values=values, range_name="A1")
    except Exception as e:
        print(f"Sheet Error: {str(e)}", flush=True)


# ===== MAIN EXECUTION =====
stocks = get_watchlist_stocks()
all_trades = []
REJECT_KEYWORDS = ["LIQUID", "ETF", "CPSE", "NETF", "GILT", "GOLD", "SILVER"]

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

    # 🎯 PATTERN-WISE BREAKDOWN TABLE GENERATION 🎯
    breakdown_list = []
    for pattern, group in trades_df.groupby("Swing_Low_Pattern"):
        tot = len(group)
        w = len(group[group["Result"] == "WIN"])
        l = len(group[group["Result"] == "LOSS"])
        wr = round((w / tot) * 100, 2)

        tag = "🏆 HIGH WINNER SETUP" if wr >= 70 else ("🔴 LOSS MAKER SETUP" if wr < 45 else "🟢 MODERATE SETUP")

        breakdown_list.append({
            "Swing_Low_Pattern": pattern,
            "Total_Trades": tot,
            "Wins": w,
            "Losses": l,
            "Win_Rate_Pct": f"{wr}%",
            "Setup_Tag": tag,
        })

    breakdown_df = pd.DataFrame(breakdown_list).sort_values(
        by="Wins", ascending=False
    )

    print("\n===========================================================")
    print("      📊 PATTERN-WISE WIN vs LOSS DETAILED BREAKDOWN       ")
    print("===========================================================")
    print(breakdown_df.to_string(index=False))
    print("===========================================================\n")

    # Upload to Google Sheet
    upload_to_sheet(ws_breakdown, breakdown_df)
    upload_to_sheet(ws_trades, trades_df)
                    
