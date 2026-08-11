import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V124.0: HIGH-VOL RED HAMMER PARTIAL PROFIT ENGINE ===", flush=True)

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


# ===== 2. PARTIAL PROFIT BOOKING STRATEGY ENGINE =====
def backtest_partial_profit_red_hammer(df_daily):
    trades = []
    df = df_daily.copy()

    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

    n = len(df)
    i = 30

    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            # Step 1: Swing High
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

            # Step 2: High Vol Red Hammer Filter
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
                is_high_volume = h_vol >= h_vol_avg

                if is_red_hammer and is_hammer and is_pullback and is_high_volume:
                    
                    past_10d_max = df['High'].iloc[max(0, i - 10) : i].max()
                    recent_min_low = df['Low'].iloc[swing_high_idx : i + 1].min()

                    # Step 3: Breakout with Volume >= 1.3x
                    entry_found = False
                    entry_idx = -1
                    entry_price = 0.0

                    check_window = min(n - 1, i + 10)
                    for k in range(i + 1, check_window):
                        break_vol = df['Volume'].iloc[k]
                        break_vol_avg = df['Vol_Avg_20'].iloc[k]

                        if df['High'].iloc[k] > past_10d_max and break_vol >= (break_vol_avg * 1.3):
                            entry_found = True
                            entry_idx = k
                            entry_price = past_10d_max
                            break

                    # Step 4: Partial Booking Simulation (50% at 1:1.5, 50% at 1:3)
                    if entry_found and entry_idx < n - MAX_HOLDING_DAYS:
                        stop_loss = round(recent_min_low * 0.99, 2)
                        risk = entry_price - stop_loss

                        if risk > 0 and 0.02 <= (risk / entry_price) <= 0.08:
                            t1_price = round(entry_price + (risk * 1.5), 2)
                            t2_price = round(entry_price + (risk * 3.0), 2)

                            future_df = df.iloc[entry_idx + 1 : entry_idx + 1 + MAX_HOLDING_DAYS]
                            
                            p1_booked = False
                            curr_sl = stop_loss
                            pnl_p1 = 0.0
                            pnl_p2 = 0.0

                            for _, f_row in future_df.iterrows():
                                # Target 1 Hit -> Book 50% and move SL to Breakeven
                                if not p1_booked and f_row['High'] >= t1_price:
                                    p1_booked = True
                                    pnl_p1 = ((t1_price - entry_price) / entry_price) * 100
                                    curr_sl = entry_price

                                # Target 2 Hit -> Book remaining 50%
                                if p1_booked and f_row['High'] >= t2_price:
                                    pnl_p2 = ((t2_price - entry_price) / entry_price) * 100
                                    break

                                # SL Hit
                                if f_row['Low'] <= curr_sl:
                                    if not p1_booked:
                                        pnl_p1 = ((curr_sl - entry_price) / entry_price) * 100
                                        pnl_p2 = pnl_p1
                                    else:
                                        pnl_p2 = ((curr_sl - entry_price) / entry_price) * 100
                                    break

                            if not p1_booked and pnl_p1 == 0.0 and not future_df.empty:
                                last_close = future_df['Close'].iloc[-1]
                                pnl_p1 = ((last_close - entry_price) / entry_price) * 100
                                pnl_p2 = pnl_p1

                            total_pnl = (0.5 * pnl_p1) + (0.5 * pnl_p2)
                            trades.append({"Win": total_pnl > 0, "PnL_%": total_pnl})

                            i = entry_idx + 6
                            continue
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V124.0 High-Vol Red Hammer Engine Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        df_res = backtest_partial_profit_red_hammer(df)
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
    print("🏆 RESULTS: V124.0 HIGH-VOL RED HAMMER ENGINE")
    print("==================================================================")
    print(f"Total Quality Executed Trades  : {total_tr}")
    print(f"Overall Win-Rate               : {round(win_rate, 2)}%")
    print(f"Overall Profit Factor          : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the criteria in the backtest period.")
    
