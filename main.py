import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== ADVANCED VPA ENGINE (DONCHIAN BREAKOUT + DYNAMIC TRAILING SL) ===", flush=True)

# ===== CONFIGURATION =====
MIN_TURNOVER_VALUE = 20_000_000   # ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 45              # Allow trend to run up to 45 trading days

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Data for Analysis

# ===== 1. FETCH WATCHLIST FROM GOOGLE SHEET =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")

    raw_stocks = ws_watchlist.col_values(1)
    STOCKS = []
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                clean_s += ".NS"
            STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Total Stocks Loaded from Google Sheet: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Google Sheet: {e}")
    exit(1)


# ===== 2. ADVANCED VPA + TRAILING SL ENGINE =====
def backtest_advanced_vpa(df_daily):
    trades = []
    df = df_daily.copy()

    # Turnover & Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['Vol_MA20'] = df['Volume'].rolling(20).mean()
    
    df['Donchian_High_20'] = df['High'].shift(1).rolling(20).max()
    df['SMA_50'] = df['Close'].rolling(50).mean()
    df['SMA_200'] = df['Close'].rolling(200).mean()

    # True Breakout: Liquid + Uptrend + 20-Day High Breakout + Ultra High Volume
    df['Is_Liquid'] = df['Turnover_MA20'] >= MIN_TURNOVER_VALUE
    df['In_Uptrend'] = (df['Close'] > df['SMA_50']) & (df['SMA_50'] > df['SMA_200'])
    df['Box_Breakout'] = df['Close'] > df['Donchian_High_20']
    df['Is_High_Vol'] = df['Volume'] > (df['Vol_MA20'] * 1.5)

    df['Signal'] = df['Is_Liquid'] & df['In_Uptrend'] & df['Box_Breakout'] & df['Is_High_Vol']

    n = len(df)
    i = 200

    while i < n - MAX_HOLDING_DAYS:
        if df['Signal'].iloc[i]:
            entry_price = df['Close'].iloc[i]
            initial_sl = df['Low'].iloc[i-3 : i+1].min() # 3-Day Swing Low
            risk = entry_price - initial_sl

            # Risk Cap check (Max 5% Risk per entry)
            if risk > 0 and (risk / entry_price) <= 0.05:
                trail_sl = initial_sl
                future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]

                win = False
                exit_price = entry_price

                for idx, row in future_df.iterrows():
                    # Trailing Stop Loss: Move SL up as price makes higher swing lows
                    current_sl_candidate = row['Low']
                    if row['Close'] > entry_price + (risk * 1.0):
                        trail_sl = max(trail_sl, row['SMA_50']) # Trail using 50-SMA in profit zone

                    if row['Low'] <= trail_sl:
                        exit_price = trail_sl
                        win = exit_price > entry_price
                        break

                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                trades.append({"Win": win, "PnL_%": pnl_pct})

                i += 10 # Skip 10 days to avoid duplicate signals on same breakout
                continue
        i += 1

    if not trades:
        return None

    df_trades = pd.DataFrame(trades)
    total_tr = len(df_trades)
    wins = df_trades[df_trades['Win'] == True]
    losses = df_trades[df_trades['Win'] == False]

    win_rate = (len(wins) / total_tr) * 100
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999

    return {
        "Trades": total_tr,
        "Win_Rate": win_rate,
        "Gross_Profit": gross_profit,
        "Gross_Loss": gross_loss,
        "Profit_Factor": profit_factor
    }


# ===== 3. EXECUTE BACKTEST =====
all_trades = 0
all_profit = 0.0
all_loss = 0.0
winrate_list = []

print("\nRunning Advanced Donchian Box Breakout + Dynamic Trailing SL Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_advanced_vpa(df)
        if res:
            all_trades += res["Trades"]
            all_profit += res["Gross_Profit"]
            all_loss += res["Gross_Loss"]
            winrate_list.append(res["Win_Rate"])
    except Exception:
        pass

if all_trades > 0:
    avg_winrate = np.mean(winrate_list)
    overall_pf = all_profit / all_loss if all_loss > 0 else 999

    print("\n==================================================================")
    print("🏆 FINAL RESULTS (DONCHIAN BREAKOUT + DYNAMIC TRAILING SL)")
    print("==================================================================")
    print(f"Total High Quality Trades Executed : {all_trades}")
    print(f"Average Win-Rate                    : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                       : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the criteria.")
    
