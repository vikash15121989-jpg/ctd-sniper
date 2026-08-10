import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== ANNA COULLING VPA BACKTEST (STRICT MIN TURNOVER ₹2 CRORE) ===", flush=True)

# ===== CONFIGURATION =====
MIN_TURNOVER_VALUE = 20_000_000   # Strictly ₹2 Crore Daily Turnover Filter
MAX_HOLDING_DAYS = 30              # Max 30 Trading Days Holding

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Historical Data

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


# ===== 2. LIQUIDITY + VPA RETEST ENGINE =====
def backtest_liquid_vpa(df_daily):
    trades = []
    df = df_daily.copy()

    # Daily Turnover (Trading Value = Close * Volume)
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()

    df['SMA_50'] = df['Close'].rolling(50).mean()
    df['SMA_200'] = df['Close'].rolling(200).mean()
    df['Vol_MA'] = df['Volume'].rolling(20).mean()
    df['Spread'] = df['High'] - df['Low']
    df['Spread_MA'] = df['Spread'].rolling(20).mean()

    # Filters: Liquid Stock (> ₹2 Cr) + Uptrend + High Volume Breakout
    df['Is_Liquid'] = df['Turnover_MA20'] >= MIN_TURNOVER_VALUE
    df['In_Uptrend'] = (df['Close'] > df['SMA_50']) & (df['SMA_50'] > df['SMA_200'])
    
    df['Is_Breakout'] = df['Is_Liquid'] & df['In_Uptrend'] & \
                        (df['Close'] > df['Open']) & \
                        (df['Spread'] > df['Spread_MA'] * 1.2) & \
                        (df['Volume'] > df['Vol_MA'] * 1.8)

    n = len(df)
    i = 200

    while i < n - MAX_HOLDING_DAYS - 10:
        if df['Is_Breakout'].iloc[i]:
            bo_high = df['High'].iloc[i]
            bo_low = df['Low'].iloc[i]
            bo_vol_ma = df['Vol_MA'].iloc[i]

            entry_found = False
            entry_idx = -1

            # Retest Phase (2 to 8 Days)
            for j in range(i + 1, min(i + 9, n - MAX_HOLDING_DAYS)):
                c_low = df['Low'].iloc[j]
                c_vol = df['Volume'].iloc[j]

                # Low-Volume Retest Near Breakout Level
                if (c_low <= bo_high * 1.015) and (c_low >= bo_low * 0.98) and (c_vol < bo_vol_ma * 0.75):
                    entry_found = True
                    entry_idx = j
                    break

            if entry_found and entry_idx != -1:
                entry_price = df['Close'].iloc[entry_idx]
                stop_loss = df['Low'].iloc[entry_idx - 3 : entry_idx + 1].min()
                risk = entry_price - stop_loss

                if risk > 0 and (risk / entry_price) <= 0.05: # Max 5% Risk per trade
                    target_price = entry_price + (risk * 2.0) # 1:2 Risk-Reward Target
                    future_df = df.iloc[entry_idx + 1 : entry_idx + 1 + MAX_HOLDING_DAYS]

                    win = False
                    exit_price = entry_price

                    for idx, row in future_df.iterrows():
                        if row['Low'] <= stop_loss:
                            exit_price = stop_loss
                            win = False
                            break
                        elif row['High'] >= target_price:
                            exit_price = target_price
                            win = True
                            break

                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    trades.append({"Win": win, "PnL_%": pnl_pct})

                i = entry_idx + 1
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

print("\nRunning Backtest on Liquid Stocks (> ₹2 Crore Turnover)...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_liquid_vpa(df)
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
    print("🏆 BACKTEST RESULTS (STRICT MIN TURNOVER ≥ ₹2 CRORE)")
    print("==================================================================")
    print(f"Total Liquid Trades Executed : {all_trades}")
    print(f"Average Win-Rate              : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                 : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo liquid trades met the criteria.")
    
