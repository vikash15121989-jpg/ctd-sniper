import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== ANNA COULLING VPA BACKTESTING: ALL WATCHLIST STOCKS ===", flush=True)

# ===== CONFIGURATION =====
RISK_REWARD_RATIO = 2.0  # 1:2 Risk to Reward
MAX_HOLDING_DAYS = 20    # Max 20 trading days hold limit

# Backtest Date Range (Pichle 2 Saal Ka Data)
END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=730)

# ===== 1. READ ALL STOCKS FROM GOOGLE SHEET WATCHLIST =====
try:
    gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
    gc = gspread.service_account_from_dict(gcp_json_creds)
    sh = gc.open("CTD_Sniper")
    ws_watchlist = sh.worksheet("Watchlist")

    # Column A se saare stock names padhna
    raw_stocks = ws_watchlist.col_values(1)
    
    # Symbols Clean and Add NSE Extension (.NS)
    STOCKS = []
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                clean_s += ".NS"
            STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS))) # Remove duplicates
    print(f"✅ Total Stocks Fetched from Google Sheet: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Google Sheet Watchlist: {e}")
    exit(1)


# ===== 2. BACKTEST CORE ENGINE =====
def backtest_strategy(df, strategy_name):
    trades = []
    df = df.copy()

    # VPA Calculations
    df['Vol_MA'] = df['Volume'].rolling(20).mean()
    df['Spread'] = df['High'] - df['Low']
    df['Spread_MA'] = df['Spread'].rolling(20).mean()

    df['Is_Wide_Green'] = (df['Close'] > df['Open']) & (df['Spread'] > df['Spread_MA'] * 1.2)
    df['Is_High_Vol'] = df['Volume'] > (df['Vol_MA'] * 1.5)
    df['Is_Low_Vol'] = df['Volume'] < (df['Vol_MA'] * 0.7)
    df['Is_Ultra_Vol'] = df['Volume'] > (df['Vol_MA'] * 2.25)

    df['Lower_Wick'] = np.minimum(df['Open'], df['Close']) - df['Low']
    df['Has_Lower_Wick'] = df['Lower_Wick'] > (df['Spread'] * 0.35)

    # Strategy Conditions
    if strategy_name == "Rule_1_Breakout_Retest":
        df['Signal'] = df['Is_Wide_Green'] & df['Is_High_Vol']

    elif strategy_name == "Rule_2_No_Supply_Test":
        df['Signal'] = (df['Close'] < df['Open']) & df['Is_Low_Vol'] & df['Has_Lower_Wick']

    elif strategy_name == "Rule_3_Stopping_Volume":
        df['Lowest_10'] = df['Low'].rolling(10).min()
        df['Signal'] = df['Is_Ultra_Vol'] & df['Has_Lower_Wick'] & (df['Low'] == df['Lowest_10'])

    # Trade Loop
    for i in range(30, len(df) - MAX_HOLDING_DAYS):
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
            trades.append({"Win": win, "PnL_%": pnl_pct, "Risk": risk})

    if not trades:
        return None

    df_trades = pd.DataFrame(trades)
    total_trades = len(df_trades)
    wins = df_trades[df_trades['Win'] == True]
    losses = df_trades[df_trades['Win'] == False]
    
    win_rate = (len(wins) / total_trades) * 100
    avg_pnl = df_trades['PnL_%'].mean()
    gross_profit = wins['PnL_%'].sum()
    gross_loss = abs(losses['PnL_%'].sum())

    return {
        "Total Trades": total_trades,
        "Win Rate": win_rate,
        "Avg PnL %": avg_pnl,
        "Gross Profit": gross_profit,
        "Gross Loss": gross_loss
    }


# ===== 3. RUN BACKTEST FOR ALL RULES ACROSS WATCHLIST =====
rules = ["Rule_1_Breakout_Retest", "Rule_2_No_Supply_Test", "Rule_3_Stopping_Volume"]
summary_results = []

print("\nRunning backtest across all Google Sheet stocks...", flush=True)

for rule in rules:
    total_rule_trades = 0
    all_gross_profit = 0.0
    all_gross_loss = 0.0
    winrates_list = []

    for stock in STOCKS:
        try:
            df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            
            if df.empty or len(df) < 50:
                continue

            res = backtest_strategy(df, rule)
            if res and res["Total Trades"] > 0:
                total_rule_trades += res["Total Trades"]
                all_gross_profit += res["Gross Profit"]
                all_gross_loss += res["Gross Loss"]
                winrates_list.append(res["Win Rate"])

        except Exception:
            pass

    if total_rule_trades > 0:
        avg_winrate = np.mean(winrates_list)
        profit_factor = all_gross_profit / all_gross_loss if all_gross_loss > 0 else 999

        summary_results.append({
            "VPA Strategy / Rule": rule,
            "Total Trades Executed": total_rule_trades,
            "Average Win-Rate": f"{round(avg_winrate, 1)}%",
            "Profit Factor": round(profit_factor, 2)
        })

# ===== 4. DISPLAY SUMMARY REPORT =====
df_summary = pd.DataFrame(summary_results)
print("\n==================================================================")
print(f"📊 BACKTEST RESULTS FOR ALL {len(STOCKS)} STOCKS FROM GOOGLE SHEET")
print("==================================================================")
print(df_summary.to_string(index=False))
