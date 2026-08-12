import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V140.0 TEST: LIQUIDITY SWEEP WITH 20 EMA FILTER ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 1_000_000       # 10 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 5.0        # ₹5 Crore Daily Turnover
MAX_HOLDING_DAYS = 20            # 20 Days Holding Window

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


# ===== STRATEGY ENGINE (20 EMA TEST) =====
def backtest_v140_20ema_sweep(df):
    trades = []
    n = len(df)
    i = 20 # Warm up for 20 EMA

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Check
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Trend Check: Close > 20 EMA (Short-term Trend Alignment)
            above_20_ema = df['Close'].iloc[i] > df['EMA_20'].iloc[i]
            
            # Sweep Logic: Low sweeps 10-day Low but Closes above it
            prev_10_low = df['Low'].iloc[i-10:i].min()
            swept_low = df['Low'].iloc[i] < prev_10_low
            closed_above = df['Close'].iloc[i] > prev_10_low
            
            # Volume Spike Check (1.3x 20-Day Avg)
            high_vol = df['Volume'].iloc[i] >= (1.3 * df['Vol_Avg_20'].iloc[i])

            if above_20_ema and swept_low and closed_above and high_vol:
                entry_price = df['Close'].iloc[i]
                stop_loss = round(df['Low'].iloc[i] * 0.995, 2)
                
                # Dynamic Target: Previous 10-Day High
                target_price = df['High'].iloc[i-10:i].max()
                
                risk = entry_price - stop_loss

                if risk > 0 and (risk / entry_price) <= 0.07: # Max 7% Risk
                    future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                    win = False
                    exit_price = entry_price

                    for _, f_row in future_df.iterrows():
                        if f_row['High'] >= target_price:
                            exit_price = target_price
                            win = True
                            break
                        if f_row['Low'] <= stop_loss:
                            exit_price = stop_loss
                            win = False
                            break

                    if exit_price == entry_price and not future_df.empty:
                        exit_price = future_df['Close'].iloc[-1]
                        win = exit_price > entry_price

                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    trades.append({"Win": win, "PnL_%": pnl_pct, "Risk_%": (risk / entry_price) * 100})
                    i += MAX_HOLDING_DAYS
        i += 1

    return pd.DataFrame(trades) if trades else None


# ===== MAIN EXECUTION =====
all_trades = []

print("\nExecuting V140.0 Backtest with 20 EMA Filter...", flush=True)

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
        df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()

        res = backtest_v140_20ema_sweep(df)
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

    print("\n==================================================================")
    print("🏆 V140.0 TEST RESULTS (20 EMA FILTER)")
    print("==================================================================")
    print(f"Total Executed Trades          : {total_tr}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print(f"Average Profit per Win Trade   : +{round(wins['PnL_%'].mean(), 2)}%")
    print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
    print("==================================================================")
else:
    print("\nNo trades executed in V140.0.")
    
