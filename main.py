import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== BACKTEST: RELAXED HIGH-FREQUENCY LIQUIDITY SWEEP (V147.0) ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 300_000          # 3 Lakh Daily Avg Volume (Expanded Universe)
MIN_AVG_TURNOVER_CR = 1.0        # ₹1 Crore Daily Turnover
MAX_HOLDING_DAYS = 20            # 20 Days Holding Limit

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Historical Backtest

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


# ===== RELAXED SWEEP BACKTEST ENGINE =====
def backtest_relaxed_sweep(df):
    trades = []
    n = len(df)
    i = 30

    while i < n - MAX_HOLDING_DAYS:
        curr_vol_avg = df['Vol_Avg_20'].iloc[i]
        curr_turnover_avg = df['Turnover_Avg_20_Cr'].iloc[i]

        if curr_vol_avg >= MIN_AVG_VOLUME and curr_turnover_avg >= MIN_AVG_TURNOVER_CR:
            
            curr_close = df['Close'].iloc[i]
            curr_low = df['Low'].iloc[i]
            curr_high = df['High'].iloc[i]
            curr_vol = df['Volume'].iloc[i]
            ema_20 = df['EMA_20'].iloc[i]

            prev_10_low = df['Low'].iloc[i-10:i].min()
            prev_10_high = df['High'].iloc[i-10:i].max()

            above_ema = curr_close > ema_20
            swept_low = curr_low < prev_10_low
            closed_above_low = curr_close > prev_10_low

            # Candle Rejection (35% Lower Wick Rejection)
            c_range = curr_high - curr_low
            strong_rejection = False
            if c_range > 0:
                close_pos = (curr_close - curr_low) / c_range
                strong_rejection = close_pos >= 0.35

            # Volume Condition (0.95x Normal Vol)
            normal_vol = curr_vol >= (0.95 * curr_vol_avg)

            if above_ema and swept_low and closed_above_low and strong_rejection and normal_vol:
                entry = curr_close
                sl = round(curr_low * 0.995, 2)
                risk = entry - sl
                
                min_target = entry + (1.5 * risk)
                target = round(max(prev_10_high, min_target), 2)

                risk_pct = (risk / entry) * 100

                if risk > 0 and risk_pct <= 7.0:
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
                    trades.append({"Win": win, "PnL_%": pnl_pct, "Risk_%": risk_pct})
                    i += MAX_HOLDING_DAYS
        i += 1

    return pd.DataFrame(trades) if trades else None


# ===== MAIN EXECUTION =====
all_trades = []

print("\nExecuting Relaxed High-Frequency Sweep Backtest...", flush=True)

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

        res = backtest_relaxed_sweep(df)
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
    print("🏆 RESULTS: RELAXED HIGH-FREQUENCY SWEEP (V147.0)")
    print("==================================================")
    print(f"Total Executed Trades          : {total_tr}")
    print(f"Win-Rate                       : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print(f"Average Profit per Win Trade   : +{round(wins['PnL_%'].mean(), 2)}%")
    print(f"Average Loss per Losing Trade  : {round(losses['PnL_%'].mean(), 2)}%")
    print("==================================================")
else:
    print("\nNo trades executed for Relaxed Sweep Strategy.")
    
