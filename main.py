import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== DAILY MULTI-RESISTANCE 20MA VOLUME EXPANSION BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_DAILY_TURNOVER = 20_000_000   # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 40             # Positional Holding up to 40 Trading Days

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
def backtest_multi_resistance_setup(df_daily):
    trades = []
    df = df_daily.copy()

    # Calculate Daily Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['SMA20'] = df['Close'].rolling(20).mean()
    df['Vol_SMA20'] = df['Volume'].rolling(20).mean()
    df['Vol_Max10'] = df['Volume'].shift(1).rolling(10).max()
    df['Price_Max10'] = df['High'].shift(1).rolling(10).max()

    n = len(df)
    i = 100 # Start after sufficient historical data

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Check
        if df['Turnover_MA20'].iloc[i] >= MIN_DAILY_TURNOVER:
            
            # Step 1: Detect Multi-Resistance (Check if Highs hit a common level at least 2-3 times in last 60 days)
            recent_window = df.iloc[i-60 : i]
            peak_high = recent_window['High'].max()
            
            # Count touchpoints within 1.5% of peak high
            touchpoints = (recent_window['High'] >= peak_high * 0.985).sum()

            if touchpoints >= 2: # At least Multi-Resistance (2+ touches)
                
                # Step 2: Proximity to 20 SMA (Price within 3% of 20 SMA)
                curr_close = df['Close'].iloc[i]
                sma20_val = df['SMA20'].iloc[i]

                if abs(curr_close - sma20_val) / sma20_val <= 0.03:
                    
                    # Step 3: High Volume Mother Candle near 20 MA
                    curr_vol = df['Volume'].iloc[i]
                    avg_vol = df['Vol_SMA20'].iloc[i]
                    is_green = df['Close'].iloc[i] > df['Open'].iloc[i]

                    if is_green and curr_vol >= avg_vol * 1.5:
                        mother_vol = curr_vol
                        mother_low = df['Low'].iloc[i]

                        # Lookahead 2-6 days for Squeeze + Low Vol Green Candle + Volume Surge
                        for d in range(1, 6):
                            if i + d < n:
                                dry_vol = df['Volume'].iloc[i + d]
                                is_low_vol_green = (df['Close'].iloc[i + d] > df['Open'].iloc[i + d]) and (dry_vol < mother_vol * 0.50)

                                # Step 4 & 5: Check for Low Vol Green Candle followed by High Volume Surge (> 10-day Max Vol)
                                if is_low_vol_green and (i + d + 1 < n):
                                    surge_day = i + d + 1
                                    surge_vol = df['Volume'].iloc[surge_day]
                                    vol_max_10 = df['Vol_Max10'].iloc[surge_day]

                                    if surge_vol > vol_max_10:
                                        
                                        # Step 6: Breakout Entry above 10-Day Max High Price
                                        trigger_price = df['Price_Max10'].iloc[surge_day]
                                        surge_close = df['Close'].iloc[surge_day]

                                        if surge_close > trigger_price:
                                            entry_price = surge_close
                                            squeeze_low = df['Low'].iloc[i : surge_day].min()
                                            stop_loss = squeeze_low
                                            risk = entry_price - stop_loss

                                            # Risk Filter (Max 8% Risk Cap)
                                            if risk > 0 and (risk / entry_price) <= 0.08:
                                                future_df = df.iloc[surge_day + 1 : surge_day + 1 + MAX_HOLDING_DAYS]

                                                win = False
                                                exit_price = entry_price
                                                trail_sl = stop_loss

                                                for _, f_row in future_df.iterrows():
                                                    # Trailing Stop Loss when trade moves > 1.5R in profit
                                                    if f_row['Close'] > entry_price + (risk * 1.5):
                                                        trail_sl = max(trail_sl, f_row['Low'])

                                                    if f_row['Low'] <= trail_sl:
                                                        exit_price = trail_sl
                                                        win = exit_price > entry_price
                                                        break

                                                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                                                trades.append({"Win": win, "PnL_%": pnl_pct})

                                                i = surge_day + 5 # Move index forward
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

print("\nRunning Multi-Resistance Volume Expansion Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_multi_resistance_setup(df)
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
    print("🏆 RESULTS: DAILY MULTI-RESISTANCE VOLUME EXPANSION")
    print("==================================================================")
    print(f"Total Quality Trades Executed   : {all_trades}")
    print(f"Average Win-Rate                : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                   : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the exact criteria.")
                                                
