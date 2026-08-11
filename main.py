import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== OPTIMIZED WYCKOFF PULLBACK + ASYMMETRIC RR BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_DAILY_TURNOVER = 20_000_000   # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 35             # Holding Period

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
    for s in raw_stocks:
        clean_s = s.strip().upper()
        if clean_s and clean_s not in ["STOCK", "SYMBOL", "NAME", "STOCKS"]:
            if not clean_s.endswith(".NS") and not clean_s.startswith("^"):
                clean_s += ".NS"
            STOCKS.append(clean_s)

    STOCKS = sorted(list(set(STOCKS)))
    print(f"✅ Total Stocks Loaded: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# ===== 2. STRATEGY ENGINE =====
def backtest_wyckoff_asymmetric(df_daily):
    trades = []
    df = df_daily.copy()

    # Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['SMA20'] = df['Close'].rolling(20).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_SMA20'] = df['Volume'].rolling(20).mean()

    n = len(df)
    i = 80

    while i < n - MAX_HOLDING_DAYS:
        if df['Turnover_MA20'].iloc[i] >= MIN_DAILY_TURNOVER:
            
            # Step 1: Wyckoff Base Consolidation (Tight Range)
            past_accum = df.iloc[i-45 : i-10]
            accum_high = past_accum['High'].max()
            accum_low = past_accum['Low'].min()
            accum_range_pct = (accum_high - accum_low) / accum_low

            if accum_range_pct <= 0.20: # Range Height <= 20%
                
                # Step 2: High Volume Breakout Check
                breakout_window = df.iloc[i-10 : i]
                has_breakout = False
                bo_vol = 0

                for idx, bo_row in breakout_window.iterrows():
                    if bo_row['Close'] > accum_high and bo_row['Volume'] >= df.loc[idx, 'Vol_SMA20'] * 1.8:
                        has_breakout = True
                        bo_vol = bo_row['Volume']
                        break

                if has_breakout:
                    curr_close = df['Close'].iloc[i]
                    sma20_val = df['SMA20'].iloc[i]
                    curr_vol = df['Volume'].iloc[i]

                    # Step 3: Mean Reversion Retest (Price near 20 MA, not overextended)
                    overextension = (curr_close - sma20_val) / sma20_val
                    near_20ma = 0.0 <= overextension <= 0.025
                    ultra_dry_vol = curr_vol <= bo_vol * 0.35
                    is_green = df['Close'].iloc[i] > df['Open'].iloc[i]

                    if near_20ma and ultra_dry_vol and is_green:
                        entry_price = curr_close
                        stop_loss = min(df['Low'].iloc[i-2 : i+1].min(), sma20_val * 0.985)
                        risk = entry_price - stop_loss

                        # Risk Cap: 1.5% to 4.5%
                        if risk > 0 and 0.015 <= (risk / entry_price) <= 0.045:
                            target_price = entry_price + (risk * 2.5) # 1:2.5 Target
                            breakeven_trigger = entry_price + (risk * 1.0)
                            
                            future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]

                            win = False
                            exit_price = entry_price
                            current_sl = stop_loss

                            for _, f_row in future_df.iterrows():
                                # Lock Breakeven at 1:1 Gain
                                if f_row['High'] >= breakeven_trigger:
                                    current_sl = max(current_sl, entry_price)

                                # Hit Target
                                if f_row['High'] >= target_price:
                                    exit_price = target_price
                                    win = True
                                    break
                                
                                # Hit Stop Loss / Breakeven
                                if f_row['Low'] <= current_sl:
                                    exit_price = current_sl
                                    win = exit_price > entry_price
                                    break
                                
                                # Exit on 20 SMA Closing Breakdown
                                if f_row['Close'] < f_row['SMA20']:
                                    exit_price = f_row['Close']
                                    win = exit_price > entry_price
                                    break

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({"Win": win, "PnL_%": pnl_pct})

                            i += 6
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

print("\nRunning Wyckoff Asymmetric Risk-Reward Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 150:
            continue

        res = backtest_wyckoff_asymmetric(df)
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
    print("🏆 RESULTS: WYCKOFF ASYMMETRIC REWARD ENGINE")
    print("==================================================================")
    print(f"Total Quality Executed Trades  : {all_trades}")
    print(f"Average Win-Rate               : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the exact Wyckoff criteria.")
    
