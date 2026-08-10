import os
import json
import warnings
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta
import gspread

warnings.filterwarnings("ignore")

print("=== WEEKLY BULLISH + DRY VOLUME RETEST SCANNER & BACKTEST ===", flush=True)

# CONFIGURATION
MIN_TURNOVER = 20_000_000  # Min ₹2 Crore Turnover
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Data

# 1. READ WATCHLIST FROM GOOGLE SHEET
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
    print(f"✅ Watchlist Loaded: {len(STOCKS)} Stocks", flush=True)

except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# 2. EXACT RETEST LOGIC ENGINE
def run_custom_retest_strategy(df_daily):
    # Resample to Weekly Data
    df_weekly = df_daily.resample('W').agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 'Volume': 'sum'
    }).dropna()

    if len(df_weekly) < 30:
        return None

    # Condition 1: Weekly Bullish Filter
    df_weekly['W_SMA20'] = df_weekly['Close'].rolling(20).mean()
    df_weekly['Weekly_Bullish'] = df_weekly['Close'] > df_weekly['W_SMA20']

    # Map Weekly Trend to Daily Data
    df = df_daily.copy()
    df['Weekly_Trend'] = df_weekly['Weekly_Bullish'].reindex(df.index, method='ffill')

    # Daily Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['Vol_MA20'] = df['Volume'].rolling(20).mean()
    
    # Expansion Day: Green Candle with High Volume (>1.5x Avg Vol)
    df['High_Vol_Expansion'] = (df['Close'] > df['Open']) & (df['Volume'] > df['Vol_MA20'] * 1.5)

    # Dry Volume Day: Volume < 60% of Avg Vol
    df['Is_Dry_Volume'] = df['Volume'] < (df['Vol_MA20'] * 0.60)

    # Reversal Bullish Candle (Green Close)
    df['Is_Bullish_Candle'] = df['Close'] > df['Open']

    trades = []
    n = len(df)
    i = 200

    while i < n - 30:
        # Step 1: Weekly Trend must be Bullish
        if df['Weekly_Trend'].iloc[i] and df['Turnover_MA20'].iloc[i] >= MIN_TURNOVER:
            
            # Step 2: Check if High-Volume Expansion occurred in last 10 days
            expansion_window = df.iloc[i-10 : i]
            if expansion_window['High_Vol_Expansion'].any():
                
                # High Volume Candle Reference Level (Support Zone)
                exp_idx = expansion_window[expansion_window['High_Vol_Expansion']].index[-1]
                support_level = df.loc[exp_idx, 'Low']

                # Step 3: Dry Volume Retest Check
                current_close = df['Close'].iloc[i]
                current_low = df['Low'].iloc[i]
                is_dry = df['Is_Dry_Volume'].iloc[i]
                is_bullish = df['Is_Bullish_Candle'].iloc[i]

                # Price near Support (within 2% range above Support)
                near_support = (current_low >= support_level * 0.98) and (current_close <= support_level * 1.03)

                # Step 4: Final Trigger Condition
                if is_dry and near_support and is_bullish:
                    entry_price = df['Close'].iloc[i]
                    
                    # Stop-loss = Lowest Point of Retest/Pullback Phase
                    stop_loss = df['Low'].iloc[i-3 : i+1].min()
                    risk = entry_price - stop_loss

                    if risk > 0 and (risk / entry_price) <= 0.035: # Max 3.5% Risk Cap
                        future_df = df.iloc[i + 1 : i + 1 + 30]

                        win = False
                        exit_price = entry_price
                        trail_sl = stop_loss

                        for idx, row in future_df.iterrows():
                            # Trail Stop Loss with EMA20 once trade moves 1.5R in profit
                            if row['Close'] > entry_price + (risk * 1.5):
                                trail_sl = max(trail_sl, row['Low'])

                            if row['Low'] <= trail_sl:
                                exit_price = trail_sl
                                win = exit_price > entry_price
                                break

                        pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                        trades.append({"Win": win, "PnL_%": pnl_pct})

                        i += 5 # Skip few days to avoid duplicate signals
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


# 3. RUN TESTING
all_trades = 0
all_profit = 0.0
all_loss = 0.0
winrate_list = []

print("\nRunning Backtest on Exact Setup...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = run_custom_retest_strategy(df)
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
    print("🏆 RESULTS: WEEKLY BULLISH + DAILY DRY VOLUME RETEST")
    print("==================================================================")
    print(f"Total Trades Executed           : {all_trades}")
    print(f"Average Win-Rate                : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                   : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the exact criteria.")
    
