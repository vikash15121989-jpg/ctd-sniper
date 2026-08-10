import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== PURE STOCK-SPECIFIC VPA (DRY VOLUME PULLBACK - NO NIFTY FILTER) ===", flush=True)

# ===== CONFIGURATION =====
MIN_TURNOVER_VALUE = 20_000_000   # Min ₹2 Crore Daily Trading Value
MAX_HOLDING_DAYS = 40              # Holding up to 40 Trading Days

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Data

# ===== 1. READ GOOGLE SHEET WATCHLIST =====
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
    print(f"✅ Total Stocks Loaded: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Google Sheet: {e}")
    exit(1)


# ===== 2. INDIVIDUAL STOCK RETEST ENGINE =====
def backtest_stock_specific_vpa(df_daily):
    trades = []
    df = df_daily.copy()

    # Stock Liquidity & Moving Averages
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['Vol_MA20'] = df['Volume'].rolling(20).mean()
    df['EMA_20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['SMA_200'] = df['Close'].rolling(200).mean()

    # Individual Stock Strength Rules (No Nifty Context)
    df['Is_Liquid'] = df['Turnover_MA20'] >= MIN_TURNOVER_VALUE
    df['Stock_Uptrend'] = (df['Close'] > df['EMA_20']) & (df['EMA_20'] > df['SMA_200'])
    
    # Dry Volume Condition: Volume drops 50% below 20-day Average
    df['Is_Dry_Volume'] = df['Volume'] <= (df['Vol_MA20'] * 0.50)
    
    # Price Consolidation / Pullback near 20-EMA
    df['Near_EMA20'] = (abs(df['Close'] - df['EMA_20']) / df['EMA_20']) <= 0.02

    # Signal: Individual Stock Strength + Low Volume Retest
    df['Signal'] = df['Is_Liquid'] & df['Stock_Uptrend'] & df['Is_Dry_Volume'] & df['Near_EMA20']

    n = len(df)
    i = 200

    while i < n - MAX_HOLDING_DAYS:
        if df['Signal'].iloc[i]:
            entry_price = df['Close'].iloc[i]
            stop_loss = df['Low'].iloc[i-2 : i+1].min() * 0.995 # Swing Low SL
            risk = entry_price - stop_loss

            # Strict Risk Cap: Entry only if Risk <= 2.5%
            if risk > 0 and (risk / entry_price) <= 0.025:
                future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]

                win = False
                exit_price = entry_price
                trail_sl = stop_loss

                for idx, row in future_df.iterrows():
                    # Dynamic Trailing with 20-EMA on Profit
                    if row['Close'] > entry_price + (risk * 1.5):
                        trail_sl = max(trail_sl, row['EMA_20'])

                    if row['Low'] <= trail_sl:
                        exit_price = trail_sl
                        win = exit_price > entry_price
                        break

                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                trades.append({"Win": win, "PnL_%": pnl_pct})

                i += 5 # Avoid clustered entries on same stock
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

print("\nExecuting Stock-Specific Dry Volume Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_stock_specific_vpa(df)
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
    print("🏆 STOCK-SPECIFIC DRY VOLUME RETEST RESULTS")
    print("==================================================================")
    print(f"Total Selected Trades Executed : {all_trades}")
    print(f"Average Win-Rate                : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                   : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the criteria.")
    
