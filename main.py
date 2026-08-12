import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V137.0: CUSTOM LIQUIDITY ENGINE (10 LAKH VOL | ₹5 CR TURNOVER) ===", flush=True)

# ===== EXACT LIQUIDITY CONFIGURATION =====
MIN_AVG_VOLUME = 1_000_000       # Exactly 10 Lakh Daily Avg Volume
MIN_AVG_TURNOVER_CR = 5.0        # Exactly ₹5 Crore Daily Turnover
MAX_HOLDING_DAYS = 25            

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


# ===== STRATEGY ENGINES =====

# LOGIC 1: LIQUIDITY SWEEP / SL HUNT
def backtest_logic_1_sweep(df):
    trades = []
    n = len(df)
    i = 20
    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            prev_10_low = df['Low'].iloc[i-10:i].min()
            
            swept_low = df['Low'].iloc[i] < prev_10_low
            closed_above = df['Close'].iloc[i] > prev_10_low
            high_vol = df['Volume'].iloc[i] >= (1.3 * df['Vol_Avg_20'].iloc[i])

            if swept_low and closed_above and high_vol:
                entry_price = df['Close'].iloc[i]
                stop_loss = round(df['Low'].iloc[i] * 0.995, 2)
                target_price = df['High'].iloc[i-10:i].max()
                
                risk = entry_price - stop_loss
                if risk > 0 and (risk / entry_price) <= 0.08:
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


# LOGIC 2: RELATIVE OUTPERFORMANCE
def backtest_logic_2_relative(df):
    trades = []
    n = len(df)
    i = 20
    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            three_day_drop = df['Close'].iloc[i-1] <= df['Close'].iloc[i-4] * 0.96
            stock_resilient = df['Close'].iloc[i] > df['Open'].iloc[i]
            vol_spike = df['Volume'].iloc[i] >= (1.5 * df['Vol_Avg_20'].iloc[i])

            if three_day_drop and stock_resilient and vol_spike:
                entry_price = df['Close'].iloc[i]
                stop_loss = round(df['Low'].iloc[i] * 0.99, 2)
                target_price = round(entry_price * 1.08, 2)
                
                risk = entry_price - stop_loss
                if risk > 0 and (risk / entry_price) <= 0.08:
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


# LOGIC 3: VOLATILITY CONTRACTION (VCP)
def backtest_logic_3_vcp(df):
    trades = []
    n = len(df)
    i = 20
    while i < n - MAX_HOLDING_DAYS:
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            above_ema = df['Close'].iloc[i] > df['EMA_50'].iloc[i]
            range_squeeze = df['Candle_Range'].iloc[i] <= (0.5 * df['Range_Avg_10'].iloc[i])
            volume_dryup = df['Volume'].iloc[i] <= (0.5 * df['Vol_Avg_20'].iloc[i])

            if above_ema and range_squeeze and volume_dryup:
                if i + 1 < n:
                    if df['Close'].iloc[i+1] > df['High'].iloc[i] and df['Volume'].iloc[i+1] > df['Vol_Avg_20'].iloc[i+1]:
                        entry_price = df['Close'].iloc[i+1]
                        stop_loss = round(df['Low'].iloc[i] * 0.99, 2)
                        risk = entry_price - stop_loss
                        target_price = round(entry_price + (2.5 * risk), 2)

                        if risk > 0 and (risk / entry_price) <= 0.08:
                            future_df = df.iloc[i + 2 : i + 2 + MAX_HOLDING_DAYS]
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
trades_l1, trades_l2, trades_l3 = [], [], []

print("\nExecuting backtest with 10 Lakh Vol & ₹5 Cr Turnover filters...", flush=True)

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
        df['Candle_Range'] = df['High'] - df['Low']
        df['Range_Avg_10'] = df['Candle_Range'].rolling(10).mean()
        df['EMA_50'] = df['Close'].ewm(span=50, adjust=False).mean()

        res1 = backtest_logic_1_sweep(df)
        res2 = backtest_logic_2_relative(df)
        res3 = backtest_logic_3_vcp(df)

        if res1 is not None: trades_l1.append(res1)
        if res2 is not None: trades_l2.append(res2)
        if res3 is not None: trades_l3.append(res3)

    except Exception:
        pass


def summarize_results(name, trade_list):
    if not trade_list:
        print(f"\n[{name}] No trades executed.")
        return
    df_all = pd.concat(trade_list, ignore_index=True)
    total_tr = len(df_all)
    wins = df_all[df_all['Win'] == True]
    losses = df_all[df_all['Win'] == False]
    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    overall_pf = gross_profit / gross_loss if gross_loss > 0 else 999.0

    print(f"\n📊 --- {name} ---")
    print(f"Total Trades   : {total_tr}")
    print(f"Win-Rate       : {round(win_rate, 2)}%")
    print(f"Profit Factor  : {round(overall_pf, 2)}")
    print(f"Avg Loss/Trade : {round(losses['PnL_%'].mean(), 2)}%" if not losses.empty else "0%")

print("\n==================================================================")
print("🏆 CUSTOM LIQUIDITY (10L Vol | ₹5Cr Turnover) RESULTS")
print("==================================================================")
summarize_results("LOGIC 1: LIQUIDITY SWEEP / SL HUNT", trades_l1)
summarize_results("LOGIC 2: RELATIVE OUTPERFORMANCE", trades_l2)
summarize_results("LOGIC 3: VOLATILITY CONTRACTION (VCP)", trades_l3)
print("==================================================================")
