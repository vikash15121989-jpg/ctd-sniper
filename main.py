import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V133.0: RELATIVE VOLUME EXPANSION & VOLATILITY CONTRACTION ENGINE ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 100_000         # Min 1 Lakh Daily Volume
MIN_AVG_TURNOVER_CR = 2.0        # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 20             # Tighter holding period

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


# ===== 2. STRATEGY ENGINE =====
def backtest_v133_engine(df_daily):
    trades = []
    df = df_daily.copy()

    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000
    df['SMA_200'] = df['Close'].rolling(200).mean()
    df['Candle_Range'] = df['High'] - df['Low']
    df['Range_Avg_10'] = df['Candle_Range'].rolling(10).mean()

    n = len(df)
    i = 200

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Baseline Trend Guardrail: Price above 200 SMA
            if df['Close'].iloc[i] >= df['SMA_200'].iloc[i]:
                
                # Mother Red Candle (Absorption Day)
                is_red = df['Close'].iloc[i] < df['Open'].iloc[i]
                is_high_range = df['Candle_Range'].iloc[i] >= (1.3 * df['Range_Avg_10'].iloc[i])
                is_high_volume = df['Volume'].iloc[i] >= (1.5 * df['Vol_Avg_20'].iloc[i])

                if is_red and is_high_range and is_high_volume:
                    mother_high = df['High'].iloc[i]
                    mother_low = df['Low'].iloc[i]

                    search_limit = min(n - 1, i + 12)

                    for k in range(i + 1, search_limit):
                        k_vol = df['Volume'].iloc[k]
                        k_vol_avg = df['Vol_Avg_20'].iloc[k]
                        k_close = df['Close'].iloc[k]

                        # Expansion Breakout: Close > Mother High AND Relative Volume Expansion >= 1.3x 20-day Average
                        if k_close > mother_high and k_vol >= (1.3 * k_vol_avg):

                            entry_price = k_close
                            stop_loss = round(mother_low * 0.99, 2)
                            risk = entry_price - stop_loss

                            if risk > 0 and 0.02 <= (risk / entry_price) <= 0.07:
                                target_price = round(entry_price + (risk * 2.0), 2)
                                be_trigger = round(entry_price + (risk * 1.2), 2)

                                future_df = df.iloc[k + 1 : k + 1 + MAX_HOLDING_DAYS]
                                win = False
                                exit_price = entry_price
                                curr_sl = stop_loss

                                for _, f_row in future_df.iterrows():
                                    if f_row['High'] >= be_trigger:
                                        curr_sl = max(curr_sl, entry_price) # Trailing SL to BE

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
                                trades.append({"Win": win, "PnL_%": pnl_pct})

                                i = k + MAX_HOLDING_DAYS
                                break
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V133.0 Engine Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        df_res = backtest_v133_engine(df)
        if df_res is not None and not df_res.empty:
            all_trades.append(df_res)
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
    print("🏆 OVERALL RESULTS: V133.0 RELATIVE VOLUME EXPANSION ENGINE")
    print("==================================================================")
    print(f"Total Quality Executed Trades  : {total_tr}")
    print(f"Overall Win-Rate               : {round(win_rate, 2)}%")
    print(f"Overall Profit Factor          : {round(overall_pf, 2)}")
    print("==================================================================")

else:
    print("\nNo trades met the criteria in the backtest period.")
    
