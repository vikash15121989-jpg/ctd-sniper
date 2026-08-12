import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== BACKTEST: ZIGZAG VCP + LIQUIDITY SWEEP AT 2ND LOW ===", flush=True)

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


# ===== ZIGZAG + SWEEP ENGINE =====
def backtest_zigzag_sweep(df):
    trades = []
    n = len(df)
    i = 30

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Sub-dataframe for last 25 days to detect 4 swing points
            sub = df.iloc[i-25 : i+1]
            highs = sub['High'].values
            lows = sub['Low'].values
            
            # 1. First Swing High (S1)
            idx_s1 = np.argmax(highs[:10])
            s1 = highs[idx_s1]
            
            # 2. First Swing Low (L1)
            if idx_s1 + 2 < 15:
                idx_l1 = idx_s1 + 1 + np.argmin(lows[idx_s1+1 : idx_s1+8])
                l1 = lows[idx_l1]
                
                # 3. Second Swing High (S2)
                if idx_l1 + 2 < 20:
                    idx_s2 = idx_l1 + 1 + np.argmax(highs[idx_l1+1 : idx_l1+7])
                    s2 = highs[idx_s2]
                    
                    diff1 = s1 - l1  # Wave 1 size
                    diff2 = s2 - df['Low'].iloc[i]  # Wave 2 size till current day

                    # VCP Condition: Wave 2 is smaller than Wave 1
                    if diff1 > 0 and diff2 > 0 and diff2 < diff1:
                        
                        curr_close = df['Close'].iloc[i]
                        curr_low = df['Low'].iloc[i]
                        curr_high = df['High'].iloc[i]
                        
                        # --- LIQUIDITY SWEEP AT L1 ---
                        swept_l1 = curr_low < l1            # Dips below 1st Swing Low
                        closed_above_l1 = curr_close > l1   # Reclaims 1st Swing Low
                        
                        # Candle Rejection (Lower Wick >= 40%)
                        c_range = curr_high - curr_low
                        strong_rejection = False
                        if c_range > 0:
                            close_pos = (curr_close - curr_low) / c_range
                            strong_rejection = close_pos >= 0.40

                        # Volume Check
                        good_vol = df['Volume'].iloc[i] >= (1.0 * df['Vol_Avg_20'].iloc[i])

                        if swept_l1 and closed_above_l1 and strong_rejection and good_vol:
                            entry = curr_close
                            sl = round(curr_low * 0.995, 2)
                            target = round(s2, 2)  # Target = 2nd Swing High
                            
                            risk = entry - sl
                            reward = target - entry

                            if risk > 0 and reward >= (1.3 * risk) and (risk / entry) <= 0.06:
                                future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                                win = False
                                exit_price = entry

                                for _, f_row in future_df.iterrows():
                                    if f_row['High'] >= target:
                                        exit_price = target
                                        win = True
                                        break
                                    if f_row['Low'] <= sl:
                                        exit_price = sl
                                        win = False
                                        break

                                if exit_price == entry and not future_df.empty:
                                    exit_price = future_df['Close'].iloc[-1]
                                    win = exit_price > entry

                                pnl_pct = ((exit_price - entry) / entry) * 100
                                trades.append({"Win": win, "PnL_%": pnl_pct})
                                i += MAX_HOLDING_DAYS
        i += 1

    return pd.DataFrame(trades) if trades else None


# ===== MAIN EXECUTION =====
all_trades = []

print("\nExecuting ZigZag + Sweep Backtest...", flush=True)

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

        res = backtest_zigzag_sweep(df)
        if res is not None and not res.empty:
            all_trades.append(res)

    except Exception:
        pass

if all_trades:
    df_all = pd.concat(all_trades, ignore_index=True)
    total_tr = len(df_all)
    wins = df_all[df_all['Win'] == True]
    losses = df_all[df_all['Win'] == False]
    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    print("\n==================================================")
    print("🏆 RESULTS: ZIGZAG VCP + SWEEP AT 2ND LOW")
    print("==================================================")
    print(f"Total Executed Trades          : {total_tr}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print(f"Average Profit per Win Trade   : +{round(wins['PnL_%'].mean(), 2)}%")
    print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
    print("==================================================")
else:
    print("\nNo trades executed for ZigZag Sweep Strategy.")
    
