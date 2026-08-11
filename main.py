import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V122.0: RED HAMMER VOLUME BREAKDOWN BACKTEST ENGINE ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 100_000         # Min 1 Lakh Daily Volume
MIN_AVG_TURNOVER_CR = 2.0        # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 25             # Holding window for trade

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Backtest

# ===== 1. READ WATCHLIST FROM GOOGLE SHEET =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")

    raw_stocks = ws_watchlist.col_values(1)
    STOCKS = []
    REJECT_KEYWORDS = ['LIQUID', 'ETF', 'CPSE', 'NETF', 'GILT', 'GOLD', 'SILVER']

    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not any(k in clean_s for k in REJECT_KEYWORDS):
                if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                    clean_s += ".NS"
                STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Total Valid Stocks Loaded: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# ===== 2. RED HAMMER VOLUME COMPARISON ENGINE =====
def backtest_red_hammer_volume_split(df_daily):
    trades = []
    df = df_daily.copy()

    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

    n = len(df)
    i = 30

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Step 1: Find Swing High Peak
            found_swing_high = False
            swing_high_idx = -1
            swing_high_price = 0.0

            for idx in range(i - 20, i - 2):
                if idx < 5: continue
                is_local_max = df['High'].iloc[idx] == df['High'].iloc[idx - 5 : idx + 6].max()
                if is_local_max:
                    found_swing_high = True
                    swing_high_idx = idx
                    swing_high_price = df['High'].iloc[idx]

            # Step 2: Check for RED HAMMER Candle
            if found_swing_high and (i - swing_high_idx >= 2):
                h_open = df['Open'].iloc[i]
                h_close = df['Close'].iloc[i]
                h_high = df['High'].iloc[i]
                h_low = df['Low'].iloc[i]

                h_vol = df['Volume'].iloc[i]
                h_vol_avg = df['Vol_Avg_20'].iloc[i]

                body = abs(h_close - h_open)
                upper_wick = h_high - max(h_open, h_close)
                lower_wick = min(h_open, h_close) - h_low

                is_red_hammer = h_close < h_open
                is_hammer = (lower_wick >= 2.0 * max(body, 0.01)) and (upper_wick <= 0.6 * max(body, 0.01))
                is_pullback = h_close < swing_high_price

                if is_red_hammer and is_hammer and is_pullback:
                    
                    # Category Classification based on Volume
                    vol_type = "HIGH_VOL_RED_HAMMER" if h_vol >= h_vol_avg else "LOW_VOL_RED_HAMMER"

                    past_10d_max = df['High'].iloc[max(0, i - 10) : i].max()
                    recent_min_low = df['Low'].iloc[swing_high_idx : i + 1].min()

                    # Step 3: Check Breakout of Past 10-Day Max Price within next 10 days
                    entry_found = False
                    entry_idx = -1
                    entry_price = 0.0

                    check_window = min(n - 1, i + 10)
                    for k in range(i + 1, check_window):
                        if df['High'].iloc[k] > past_10d_max:
                            entry_found = True
                            entry_idx = k
                            entry_price = past_10d_max
                            break

                    # Step 4: Trade Execution & Exit Simulation
                    if entry_found and entry_idx < n - MAX_HOLDING_DAYS:
                        stop_loss = round(recent_min_low * 0.99, 2)
                        risk = entry_price - stop_loss

                        if risk > 0 and 0.02 <= (risk / entry_price) <= 0.08:
                            target_price = round(entry_price + (risk * 2.0), 2) # 1:2 Risk-Reward
                            breakeven_trigger = entry_price + (risk * 1.0)

                            future_df = df.iloc[entry_idx + 1 : entry_idx + 1 + MAX_HOLDING_DAYS]
                            win = False
                            exit_price = entry_price
                            curr_sl = stop_loss

                            for _, f_row in future_df.iterrows():
                                if f_row['High'] >= breakeven_trigger:
                                    curr_sl = max(curr_sl, entry_price)

                                if f_row['High'] >= target_price:
                                    exit_price = target_price
                                    win = True
                                    break

                                if f_row['Low'] <= curr_sl:
                                    exit_price = curr_sl
                                    win = exit_price > entry_price
                                    break

                            if exit_price == entry_price and not future_df.empty:
                                exit_price = future_df['Close'].iloc[-1]
                                win = exit_price > entry_price

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({
                                "Vol_Type": vol_type,
                                "Win": win,
                                "PnL_%": pnl_pct
                            })

                            i = entry_idx + 6
                            continue
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V122.0 Red Hammer Volume Split Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        df_res = backtest_red_hammer_volume_split(df)
        if df_res is not None and not df_res.empty:
            all_trades.append(df_res)
    except Exception:
        pass

if all_trades:
    df_all = pd.concat(all_trades, ignore_index=True)
    total_tr = len(df_all)

    # Global Performance
    wins = df_all[df_all['Win'] == True]
    losses = df_all[df_all['Win'] == False]
    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    print("\n==================================================================")
    print("🏆 OVERALL RESULTS: RED HAMMER STRATEGY")
    print("==================================================================")
    print(f"Total Quality Executed Trades  : {total_tr}")
    print(f"Overall Win-Rate               : {round(win_rate, 2)}%")
    print(f"Overall Profit Factor          : {round(overall_pf, 2)}")
    print("==================================================================")

    # High Vol vs Low Vol Breakdown
    print("\n📊 RED HAMMER VOLUME BREAKDOWN:")
    print("------------------------------------------------------------------")
    for v_type in ['HIGH_VOL_RED_HAMMER', 'LOW_VOL_RED_HAMMER']:
        sub_df = df_all[df_all['Vol_Type'] == v_type]
        if not sub_df.empty:
            sub_tr = len(sub_df)
            sub_wins = sub_df[sub_df['Win'] == True]
            sub_losses = sub_df[sub_df['Win'] == False]
            
            sub_wr = (len(sub_wins) / sub_tr) * 100
            sub_gp = sub_wins['PnL_%'].sum()
            sub_gl = abs(sub_losses['PnL_%'].sum())
            sub_pf = sub_gp / sub_gl if sub_gl > 0 else 999.0

            print(f"🔹 {v_type} -> Trades: {sub_tr} | Win-Rate: {round(sub_wr, 2)}% | Profit Factor: {round(sub_pf, 2)}")
    print("==================================================================")

else:
    print("\nNo trades met the criteria in the backtest period.")
    
