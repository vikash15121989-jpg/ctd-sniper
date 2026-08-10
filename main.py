import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== ENHANCED VPA BACKTEST ENGINE (WITH TREND & LIQUIDITY FILTERS) ===", flush=True)

# ===== CONFIGURATION =====
RISK_REWARD_RATIO = 2.0  # Target = 2x Risk
MAX_HOLDING_DAYS = 20    # Max holding period

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=730)

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


# ===== 2. STRATEGY WITH TREND & LIQUIDITY FILTERS =====
def backtest_filtered_vpa(df):
    trades = []
    df = df.copy()

    # Trend & Volume Indicators
    df['SMA_200'] = df['Close'].rolling(200).mean()
    df['Vol_MA'] = df['Volume'].rolling(20).mean()
    df['Turnover'] = df['Volume'] * df['Close']
    df['Turnover_MA'] = df['Turnover'].rolling(20).mean()
    df['Spread'] = df['High'] - df['Low']
    df['Spread_MA'] = df['Spread'].rolling(20).mean()

    # Strict Conditions
    df['In_Uptrend'] = df['Close'] > df['SMA_200']             # Trend Filter
    df['Has_Liquidity'] = df['Turnover_MA'] >= 1000000        # Liquidity Filter (> 10 Lakhs daily turnover)
    df['Is_Wide_Green'] = (df['Close'] > df['Open']) & (df['Spread'] > df['Spread_MA'] * 1.2)
    df['Is_High_Vol'] = df['Volume'] > (df['Vol_MA'] * 1.5)

    # Combined Setup: Uptrend + High Volume Breakout + Liquid Stock
    df['Signal'] = df['In_Uptrend'] & df['Has_Liquidity'] & df['Is_Wide_Green'] & df['Is_High_Vol']

    for i in range(200, len(df) - MAX_HOLDING_DAYS):
        if df['Signal'].iloc[i]:
            entry_price = df['Close'].iloc[i]
            stop_loss = df['Low'].iloc[i]
            risk = entry_price - stop_loss

            if risk <= 0:
                continue

            target_price = entry_price + (risk * RISK_REWARD_RATIO)
            future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
            
            win = False
            exit_price = entry_price
            
            for _, row in future_df.iterrows():
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

print("\nRunning Backtest with Trend (200 SMA) & Liquidity Filters...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        
        if df.empty or len(df) < 250:
            continue

        res = backtest_filtered_vpa(df)
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
    print("🎯 FILTERED VPA BACKTEST RESULT (TREND + LIQUIDITY COMBINED)")
    print("==================================================================")
    print(f"Total Trades Executed : {all_trades}")
    print(f"Average Win-Rate      : {round(avg_winrate, 2)}%")
    print(f"Profit Factor         : {round(overall_pf, 2)}")
    print("==================================================================")
    
