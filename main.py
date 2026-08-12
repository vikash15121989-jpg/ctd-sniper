import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== BACKTEST: 4-POINT ZIGZAG VOLATILITY CONTRACTION (VCP) ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 500_000          # 5 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 3.0        # ₹3 Crore Daily Turnover
MAX_HOLDING_DAYS = 20            # 20 Days Holding Limit

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


# ===== ZIGZAG PATTERN BACKTEST ENGINE =====
def backtest_zigzag_vcp(df):
    trades_A = []  # Variant A: Entry at 2nd Low (Bullish Candle)
    trades_B = []  # Variant B: Entry on 1st Swing High Breakout
    
    n = len(df)
    i = 30

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Find Swings in past 25 candles
            sub = df.iloc[i-25 : i+1]
            
            # Simple Swing Point Finder
            highs = sub['High'].values
            lows = sub['Low'].values
            
            # S1: First Swing High
            idx_s1 = np.argmax(highs[:10])
            s1 = highs[idx_s1]
            
            # L1: First Swing Low (after S1)
            if idx_s1 + 2 < 15:
                idx_l1 = idx_s1 + 1 + np.argmin(lows[idx_s1+1 : idx_s1+8])
                l1 = lows[idx_l1]
                
                # S2: Second Swing High (after L1)
                if idx_l1 + 2 < 20:
                    idx_s2 = idx_l1 + 1 + np.argmax(highs[idx_l1+1 : idx_l1+7])
                    s2 = highs[idx_s2]
                    
                    # L2: Second Swing Low (after S2)
                    if idx_s2 + 1 < len(lows):
                        idx_l2 = idx_s2 + 1 + np.argmin(lows[idx_s2+1:])
                        l2 = lows[idx_l2]
                        
                        # Calculate Contraction Differences
                        diff1 = s1 - l1  # First wave size
                        diff2 = s2 - l2  # Second wave size

                        # VCP Condition: diff2 < diff1 AND Higher Low (L2 > L1)
                        if diff1 > 0 and diff2 > 0 and diff2 < diff1 and l2 >= l1:
                            
                            entry_day = i
                            curr_close = df['Close'].iloc[i]
                            curr_open = df['Open'].iloc[i]
                            curr_low = df['Low'].iloc[i]
                            curr_high = df['High'].iloc[i]
                            
                            # Candle Bullish Rejection Check
                            c_range = curr_high - curr_low
                            is_bullish = False
                            if c_range > 0:
                                close_pos = (curr_close - curr_low) / c_range
                                is_bullish = (curr_close > curr_open) and (close_pos >= 0.50)

                            # --- VARIANT A: Entry near 2nd Low on Bullish Candle ---
                            if is_bullish and (curr_low <= l2 * 1.01):
                                entry_a = curr_close
                                sl_a = round(l2 * 0.995, 2)
                                risk_a = entry_a - sl_a
                                target_a = round(entry_a + (1.8 * risk_a), 2)

                                if risk_a > 0 and (risk_a / entry_a) <= 0.06:
                                    future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                                    win_a = False
                                    exit_price_a = entry_a

                                    for _, f_row in future_df.iterrows():
                                        if f_row['High'] >= target_a:
                                            exit_price_a = target_a
                                            win_a = True
                                            break
                                        if f_row['Low'] <= sl_a:
                                            exit_price_a = sl_a
                                            win_a = False
                                            break

                                    if exit_price_a == entry_a and not future_df.empty:
                                        exit_price_a = future_df['Close'].iloc[-1]
                                        win_a = exit_price_a > entry_a

                                    pnl_a = ((exit_price_a - entry_a) / entry_a) * 100
                                    trades_A.append({"Win": win_a, "PnL_%": pnl_a})

                            # --- VARIANT B: Entry on 1st Swing High (S1) Breakout ---
                            if curr_close > s1:
                                entry_b = curr_close
                                sl_b = round(l2 * 0.995, 2)
                                risk_b = entry_b - sl_b
                                target_b = round(entry_b + (1.8 * risk_b), 2)

                                if risk_b > 0 and (risk_b / entry_b) <= 0.08:
                                    future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                                    win_b = False
                                    exit_price_b = entry_b

                                    for _, f_row in future_df.iterrows():
                                        if f_row['High'] >= target_b:
                                            exit_price_b = target_b
                                            win_b = True
                                            break
                                        if f_row['Low'] <= sl_b:
                                            exit_price_b = sl_b
                                            win_b = False
                                            break

                                    if exit_price_b == entry_b and not future_df.empty:
                                        exit_price_b = future_df['Close'].iloc[-1]
                                        win_b = exit_price_b > entry_b

                                    pnl_b = ((exit_price_b - entry_b) / entry_b) * 100
                                    trades_B.append({"Win": win_b, "PnL_%": pnl_b})
                                    i += MAX_HOLDING_DAYS
        i += 1

    df_A = pd.DataFrame(trades_A) if trades_A else pd.DataFrame()
    df_B = pd.DataFrame(trades_B) if trades_B else pd.DataFrame()
    return df_A, df_B


# ===== MAIN EXECUTION =====
all_trades_A = []
all_trades_B = []

print("\nExecuting VCP ZigZag Backtest...", flush=True)

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

        res_A, res_B = backtest_zigzag_vcp(df)
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

print_results("VARIANT A (2nd Swing Low Bullish Entry)", all_trades_A)
print_results("VARIANT B (1st Swing High Breakout Entry)", all_trades_B)
