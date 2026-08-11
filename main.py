import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V100.4: COMSYN ANCHOR & SQUEEZE BACKTEST ENGINE ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 100_000         # Min 1 Lakh Daily Volume
MIN_AVG_TURNOVER_CR = 5.0        # Min ₹5 Crore Daily Turnover
LOOKBACK_ULTRA_VOL = 50          # Day 0 Lookback
MAX_HOLDING_DAYS = 30             # Positional Holding Limit

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


# ===== 2. COMSYN BACKTEST ENGINE =====
def backtest_comsyn_engine(df_daily):
    trades = []
    df = df_daily.copy()

    # Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

    n = len(df)
    i = 60

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Filter
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            live_close = df['Close'].iloc[i]

            # Look for Anchor Candle (Day 0) in past 40 days
            found_anchor = False
            anchor_close = 0
            anchor_vol = 0
            anchor_idx = -1

            for idx in range(i - 40, i - 1):
                if idx < LOOKBACK_ULTRA_VOL: continue

                check_vol = df['Volume'].iloc[idx]
                check_close = df['Close'].iloc[idx]
                check_open = df['Open'].iloc[idx]

                past_50d = df.iloc[idx - LOOKBACK_ULTRA_VOL : idx]
                max_vol_50d = past_50d['Volume'].max()

                # 50-Day Max Volume + Green Candle
                if check_vol > max_vol_50d and check_close > check_open:
                    anchor_close = check_close
                    anchor_vol = check_vol
                    anchor_idx = idx
                    found_anchor = True

            if found_anchor:
                is_base_alive = True
                dry_up_days = 0
                prices_in_base = []

                for check_idx in range(anchor_idx + 1, i + 1):
                    f_close = df['Close'].iloc[check_idx]
                    f_vol = df['Volume'].iloc[check_idx]
                    prices_in_base.append(f_close)

                    # Stop Loss Level Check (Max 5% breakdown from Anchor Close)
                    if f_close < (anchor_close * 0.95):
                        is_base_alive = False
                        break

                    if f_vol < (anchor_vol * 0.25):
                        dry_up_days += 1

                if is_base_alive and len(prices_in_base) >= 2:
                    base_min = min(prices_in_base)
                    base_max = max(prices_in_base)
                    current_range_pct = ((base_max - base_min) / base_min) * 100

                    # Grading Logic
                    if current_range_pct <= 3.5 and dry_up_days >= 4:
                        grade = "A+"
                    elif current_range_pct <= 5.0 and dry_up_days >= 2:
                        grade = "A"
                    else:
                        grade = "B"

                    # Trade Execution
                    entry_price = live_close
                    stop_loss = round(anchor_close * 0.95, 2)
                    target_price = round(entry_price * 1.15, 2)

                    if entry_price > stop_loss:
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
                        trades.append({"Grade": grade, "Win": win, "PnL_%": pnl_pct})

                        i += 8 # Jump forward to avoid redundant signals
                        continue
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V100.4 COMSYN Backtest Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        df_res = backtest_comsyn_engine(df)
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
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0

    print("\n==================================================================")
    print("🏆 OVERALL RESULTS: V100.4 COMSYN ENGINE")
    print("==================================================================")
    print(f"Total Executed Quality Trades  : {total_tr}")
    print(f"Average Win-Rate               : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(profit_factor, 2)}")
    print("==================================================================")

    print("\n📊 GRADE-WISE BREAKDOWN:")
    for g in ['A+', 'A', 'B']:
        g_df = df_all[df_all['Grade'] == g]
        if not g_df.empty:
            g_wins = g_df[g_df['Win'] == True]
            g_wr = (len(g_wins) / len(g_df)) * 100
            print(f"Grade {g} -> Trades: {len(g_df)} | Win Rate: {round(g_wr, 2)}%")
else:
    print("\nNo trades met the COMSYN criteria in the backtest period.")
    
