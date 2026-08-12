import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V132.0: MULTI-MA ALIGNED ABSORPTION & EXPANSION ENGINE (50 > 100 > 200) ===", flush=True)

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


# ===== 2. STRATEGY ENGINE WITH MULTI-MA ALIGNMENT =====
def backtest_v132_engine(df_daily):
    trades = []
    df = df_daily.copy()

    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000
    
    # Moving Averages Calculation
    df['SMA_50'] = df['Close'].rolling(50).mean()
    df['SMA_100'] = df['Close'].rolling(100).mean()
    df['SMA_200'] = df['Close'].rolling(200).mean()
    
    df['Candle_Range'] = df['High'] - df['Low']
    df['Range_Avg_10'] = df['Candle_Range'].rolling(10).mean()

    n = len(df)
    i = 200 # Ensure all SMAs are available

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Check
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Multi-MA Trend Alignment Filter: Close > SMA_50 > SMA_100 > SMA_200
            is_ma_aligned = (df['Close'].iloc[i] > df['SMA_50'].iloc[i]) and \
                            (df['SMA_50'].iloc[i] > df['SMA_100'].iloc[i]) and \
                            (df['SMA_100'].iloc[i] > df['SMA_200'].iloc[i])

            if is_ma_aligned:
                
                # Condition 1: High-Range High-Volume Red Candle
                is_red = df['Close'].iloc[i] < df['Open'].iloc[i]
                is_high_range = df['Candle_Range'].iloc[i] >= (1.3 * df['Range_Avg_10'].iloc[i])
                is_high_volume = df['Volume'].iloc[i] >= (1.5 * df['Vol_Avg_20'].iloc[i])

                if is_red and is_high_range and is_high_volume:
                    mother_high = df['High'].iloc[i]
                    mother_low = df['Low'].iloc[i]
                    mother_vol = df['Volume'].iloc[i]

                    search_limit = min(n - 1, i + 15)

                    for k in range(i + 1, search_limit):
                        k_vol = df['Volume'].iloc[k]
                        k_close = df['Close'].iloc[k]

                        # Breakout Confirmation: Close above Mother High
                        if k_close > mother_high:
                            b_type = "LOW_VOL_BREAKOUT" if k_vol < mother_vol else "HIGH_VOL_BREAKOUT"

                            entry_price = k_close
                            stop_loss = round(mother_low * 0.99, 2)
                            risk = entry_price - stop_loss

                            if risk > 0 and 0.02 <= (risk / entry_price) <= 0.08:
                                target_price = round(entry_price + (risk * 2.5), 2)
                                be_trigger = round(entry_price + (risk * 1.5), 2)

                                future_df = df.iloc[k + 1 : k + 1 + MAX_HOLDING_DAYS]
                                win = False
                                exit_price = entry_price
                                curr_sl = stop_loss

                                for _, f_row in future_df.iterrows():
                                    if f_row['High'] >= be_trigger:
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
                                trades.append({"Type": b_type, "Win": win, "PnL_%": pnl_pct})

                                i = k + MAX_HOLDING_DAYS # Cooldown to avoid duplicate trades
                                break
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V132.0 Multi-MA Aligned Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        df_res = backtest_v132_engine(df)
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
    print("🏆 OVERALL RESULTS: V132.0 MULTI-MA ALIGNED ENGINE (50 > 100 > 200)")
    print("==================================================================")
    print(f"Total Quality Executed Trades  : {total_tr}")
    print(f"Overall Win-Rate               : {round(win_rate, 2)}%")
    print(f"Overall Profit Factor          : {round(overall_pf, 2)}")
    print("==================================================================")

    print("\n📊 LOW-VOL vs HIGH-VOL BREAKDOWN (UNDER PERFECT TREND ALIGNMENT):")
    print("------------------------------------------------------------------")
    for t_type in ['LOW_VOL_BREAKOUT', 'HIGH_VOL_BREAKOUT']:
        sub_df = df_all[df_all['Type'] == t_type]
        if not sub_df.empty:
            sub_tr = len(sub_df)
            sub_wins = sub_df[sub_df['Win'] == True]
            sub_losses = sub_df[sub_df['Win'] == False]
            
            sub_wr = (len(sub_wins) / sub_tr) * 100
            sub_gp = sub_wins['PnL_%'].sum()
            sub_gl = abs(sub_losses['PnL_%'].sum())
            sub_pf = sub_gp / sub_gl if sub_gl > 0 else 999.0

            print(f"🔹 {t_type:<20} -> Trades: {sub_tr:<5} | Win-Rate: {round(sub_wr, 2)}% | Profit Factor: {round(sub_pf, 2)}")
    print("==================================================================")

else:
    print("\nNo trades met the criteria in the backtest period.")
    
