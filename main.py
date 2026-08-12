import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== BACKTEST: WYCKOFF ACCUMULATION WITH VOLUME DRY-UP ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 300_000          # 3 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 1.0        # ₹1 Crore Daily Turnover
MAX_HOLDING_DAYS = 25            # Max Holding Period for Swing

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Backtest

# ===== READ WATCHLIST =====
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


# ===== WYCKOFF + VOLUME DRY-UP ENGINE =====
def backtest_wyckoff_volume_dryup(df):
    trades_A = []  # Entry A: Direct Breakout
    trades_B = []  # Entry B: Retest
    
    n = len(df)
    i = 40

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Step 1: Detect Range (Past 30 bars)
            lookback = df.iloc[i-35 : i-5]
            range_high = lookback['High'].max()
            range_low = lookback['Low'].min()
            range_size = range_high - range_low

            # Range Width Check (>= 5%)
            if range_size / range_low >= 0.05:
                
                # --- CONDITION: VOLUME DRY-UP IN RANGE ---
                range_avg_vol = lookback['Volume'].mean()
                overall_avg_vol = df['Vol_Avg_20'].iloc[i-35]
                vol_dryup = range_avg_vol <= (0.85 * overall_avg_vol)  # Volume dropped in range

                # Step 2: Spring / Bear Trap Check
                spring_zone = df.iloc[i-12 : i]
                has_spring = (spring_zone['Low'].min() < range_low) and (spring_zone['Close'].iloc[-1] > range_low)
                
                if vol_dryup and has_spring:
                    
                    # Step 3: ENTRY A - Direct Breakout (With Volume Expansion)
                    curr_vol = df['Volume'].iloc[i]
                    breakout_vol_surge = curr_vol >= (1.15 * df['Vol_Avg_20'].iloc[i])

                    if df['Close'].iloc[i] > range_high and df['Close'].iloc[i-1] <= range_high and breakout_vol_surge:
                        
                        entry_a = df['Close'].iloc[i]
                        sl_a = round(range_low, 2)
                        risk_a = entry_a - sl_a
                        target_a = round(entry_a + (2.0 * risk_a), 2)  # 1:2 RR

                        risk_pct_a = (risk_a / entry_a) * 100

                        if risk_a > 0 and risk_pct_a <= 8.5:
                            future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                            win_a = False
                            exit_a = entry_a

                            for _, f_row in future_df.iterrows():
                                if f_row['High'] >= target_a:
                                    exit_a = target_a
                                    win_a = True
                                    break
                                if f_row['Low'] <= sl_a:
                                    exit_a = sl_a
                                    win_a = False
                                    break

                            if exit_a == entry_a and not future_df.empty:
                                exit_a = future_df['Close'].iloc[-1]
                                win_a = exit_a > entry_a

                            pnl_a = ((exit_a - entry_a) / entry_a) * 100
                            trades_A.append({"Win": win_a, "PnL_%": pnl_a})

                        # Step 4: ENTRY B - Retest of Range High
                        future_10 = df.iloc[i + 1 : min(i + 11, n - MAX_HOLDING_DAYS)]
                        
                        for r_idx, r_row in future_10.iterrows():
                            if r_row['Low'] <= (range_high * 1.015) and r_row['Close'] >= range_high:
                                entry_b = r_row['Close']
                                sl_b = round(range_high * 0.97, 2)  # Tight SL (3% below Range High)
                                risk_b = entry_b - sl_b
                                target_b = round(entry_b + (2.5 * risk_b), 2)  # 1:2.5 RR

                                risk_pct_b = (risk_b / entry_b) * 100

                                if risk_b > 0 and risk_pct_b <= 6.0:
                                    retest_future = df.loc[r_idx + 1 : r_idx + MAX_HOLDING_DAYS]
                                    win_b = False
                                    exit_b = entry_b

                                    for _, f_row in retest_future.iterrows():
                                        if f_row['High'] >= target_b:
                                            exit_b = target_b
                                            win_b = True
                                            break
                                        if f_row['Low'] <= sl_b:
                                            exit_b = sl_b
                                            win_b = False
                                            break

                                    if exit_b == entry_b and not retest_future.empty:
                                        exit_b = retest_future['Close'].iloc[-1]
                                        win_b = exit_b > entry_b

                                    pnl_b = ((exit_b - entry_b) / entry_b) * 100
                                    trades_B.append({"Win": win_b, "PnL_%": pnl_b})
                                    break
                                
                        i += MAX_HOLDING_DAYS
        i += 1

    df_A = pd.DataFrame(trades_A) if trades_A else pd.DataFrame()
    df_B = pd.DataFrame(trades_B) if trades_B else pd.DataFrame()
    return df_A, df_B


# ===== MAIN EXECUTION =====
all_trades_A = []
all_trades_B = []

print("\nExecuting Wyckoff + Volume Dry-up Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 50:
            continue

        df['Turnover'] = df['Close'] * df['Volume']
        df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
        df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

        res_A, res_B = backtest_wyckoff_volume_dryup(df)
        if not res_A.empty:
            all_trades_A.append(res_A)
        if not res_B.empty:
            all_trades_B.append(res_B)

    except Exception:
        pass

def print_results(title, list_trades):
    if list_trades:
        df_all = pd.concat(list_trades, ignore_index=True)
        total_tr = len(df_all)
        wins = df_all[df_all['Win'] == True]
        losses = df_all[df_all['Win'] == False]
        win_rate = (len(wins) / total_tr) * 100
        gross_profit = wins['PnL_%'].sum()
        gross_loss = abs(losses['PnL_%'].sum())
        overall_pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

        print(f"\n==================================================")
        print(f"🏆 RESULTS: {title}")
        print(f"==================================================")
        print(f"Total Executed Trades          : {total_tr}")
        print(f"Win-Rate                       : {round(win_rate, 2)}%")
        print(f"Profit Factor                  : {round(overall_pf, 2)}")
        print(f"Average Profit per Win Trade   : +{round(wins['PnL_%'].mean(), 2)}%")
        print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
        print(f"==================================================")
    else:
        print(f"\nNo trades executed for {title}.")

print_results("ENTRY A (Direct Range Breakout + Dry-up Volume)", all_trades_A)
print_results("ENTRY B (Range Retest + Dry-up Volume)", all_trades_B)
