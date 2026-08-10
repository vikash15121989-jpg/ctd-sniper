import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== WEEKLY RESISTANCE + DAILY 20MA MOTHER CANDLE DRY VOLUME BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_DAILY_TURNOVER = 20_000_000   # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 40             # Holding period up to 40 trading days

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Data

# ===== 1. READ WATCHLIST FROM GOOGLE SHEET =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")

    raw_stocks = ws_watchlist.col_values(1)
    STOCKS = []
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                clean_s += ".NS"
            STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Total Stocks Loaded: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# ===== 2. STRATEGY ENGINE =====
def backtest_resistance_20ma_mother_candle(df_daily):
    trades = []
    df = df_daily.copy()

    # Calculate Daily Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['SMA20'] = df['Close'].rolling(20).mean()
    df['Vol_SMA20'] = df['Volume'].rolling(20).mean()

    # Resample Daily to Weekly to calculate Weekly Resistance
    df_w = df.resample('W').agg({'High': 'max', 'Close': 'last'}).dropna()
    if len(df_w) < 30:
        return None

    # Weekly Resistance = Rolling 30-week Maximum High
    df_w['Weekly_Resistance'] = df_w['High'].shift(1).rolling(30).max()

    # Map Weekly Resistance back to Daily Data
    df['Weekly_Resistance'] = df_w['Weekly_Resistance'].reindex(df.index, method='ffill')

    n = len(df)
    i = 200 # Start after sufficient indicator warm-up

    while i < n - MAX_HOLDING_DAYS:
        # Check Liquidity
        if df['Turnover_MA20'].iloc[i] >= MIN_DAILY_TURNOVER:
            weekly_res = df['Weekly_Resistance'].iloc[i]

            # Step 1: Check if Price is near/approaching Weekly Resistance
            if pd.notnull(weekly_res) and df['High'].iloc[i] >= weekly_res * 0.88:
                
                # Step 2: Check Daily 20 MA Proximity (Price within 3.5% of 20 SMA)
                curr_close = df['Close'].iloc[i]
                sma20_val = df['SMA20'].iloc[i]

                if abs(curr_close - sma20_val) / sma20_val <= 0.035:
                    
                    # Step 3: Identify High Volume Mother Candle near 20 MA
                    curr_vol = df['Volume'].iloc[i]
                    avg_vol = df['Vol_SMA20'].iloc[i]
                    is_green = df['Close'].iloc[i] > df['Open'].iloc[i]
                    is_high_vol = curr_vol >= avg_vol * 1.5

                    if is_green and is_high_vol:
                        mother_high = df['High'].iloc[i]
                        mother_low = df['Low'].iloc[i]
                        mother_vol = curr_vol

                        # Step 4: Look for Dry Volume in the next 1-4 days
                        dry_found = False
                        dry_lowest_point = mother_low

                        for d in range(1, 5):
                            if i + d < n:
                                next_vol = df['Volume'].iloc[i + d]
                                next_low = df['Low'].iloc[i + d]
                                dry_lowest_point = min(dry_lowest_point, next_low)

                                # Volume drops to < 60% of Mother Candle Volume
                                if next_vol <= mother_vol * 0.60:
                                    dry_found = True

                                # Step 5: Trigger Entry when Price Breaks Mother Candle High
                                trigger_close = df['Close'].iloc[i + d]
                                prev_close = df['Close'].iloc[i + d - 1]

                                if dry_found and trigger_close > mother_high and prev_close <= mother_high:
                                    entry_price = trigger_close
                                    stop_loss = dry_lowest_point
                                    risk = entry_price - stop_loss

                                    # Risk Cap Check (Max 7% per trade)
                                    if risk > 0 and (risk / entry_price) <= 0.07:
                                        future_df = df.iloc[i + d + 1 : i + d + 1 + MAX_HOLDING_DAYS]

                                        win = False
                                        exit_price = entry_price
                                        trail_sl = stop_loss

                                        for _, f_row in future_df.iterrows():
                                            # Trail Stop Loss once trade moves > 1.5R in profit
                                            if f_row['Close'] > entry_price + (risk * 1.5):
                                                trail_sl = max(trail_sl, f_row['Low'])

                                            if f_row['Low'] <= trail_sl:
                                                exit_price = trail_sl
                                                win = exit_price > entry_price
                                                break

                                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                                        trades.append({"Win": win, "PnL_%": pnl_pct})

                                        i += d + 5 # Skip forward to prevent duplicate triggers
                                        break
        i += 1

    if not trades:
        return None

    df_trades = pd.DataFrame(trades)
    total_tr = len(df_trades)
    wins = df_trades[df_trades['Win'] == True]
    losses = df_trades[df_trades['Win'] == False]

    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999

    return {
        "Trades": total_tr,
        "Win_Rate": win_rate,
        "Gross_Profit": gross_profit,
        "Gross_Loss": gross_loss,
        "Profit_Factor": profit_factor
    }


# ===== 3. EXECUTE BACKTEST =====
all_trades = 0
all_profit = 0.0
all_loss = 0.0
winrate_list = []

print("\nRunning Weekly Resistance + Daily 20MA Mother Candle Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_resistance_20ma_mother_candle(df)
        if res:
            all_trades += res["Trades"]
            all_profit += res["Gross_Profit"]
            all_loss += res["Gross_Loss"]
            winrate_list.append(res["Win_Rate"])
    except Exception:
        pass

if all_trades > 0:
    avg_winrate = np.mean(winrate_list)
    overall_pf = all_profit / all_loss if all_loss > 0 else 999

    print("\n==================================================================")
    print("🏆 RESULTS: WEEKLY RESISTANCE + DAILY 20MA MOTHER CANDLE")
    print("==================================================================")
    print(f"Total Quality Trades Executed   : {all_trades}")
    print(f"Average Win-Rate                : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                   : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the exact criteria.")
    
