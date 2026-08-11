import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V115.0: RESISTANCE BREAKOUT & PULLBACK DRY-UP ENGINE ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 100_000         # Min 1 Lakh Daily Volume
MIN_AVG_TURNOVER_CR = 2.0        # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 25             # Holding Period for Swing

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


# ===== 2. RESISTANCE BREAKOUT & PULLBACK DRY-UP LOGIC =====
def backtest_breakout_pullback_engine(df_daily):
    trades = []
    df = df_daily.copy()

    # Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

    n = len(df)
    i = 60

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Check
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            live_close = df['Close'].iloc[i]
            live_open = df['Open'].iloc[i]
            live_vol = df['Volume'].iloc[i]
            prev_close = df['Close'].iloc[i - 1]
            avg_vol_now = df['Vol_Avg_20'].iloc[i]

            # Step 1: Find Resistance Breakout Candle in past 5-30 days
            # Look for ultra-high volume breakout
            found_anchor = False
            anchor_idx = -1
            resistance_level = 0.0

            for idx in range(i - 30, i - 3):
                if idx < 30: continue
                
                check_vol = df['Volume'].iloc[idx]
                avg_vol_then = df['Vol_Avg_20'].iloc[idx - 1]
                
                # Check prior resistance high (past 30 days before breakout)
                prior_high = df['High'].iloc[max(0, idx - 30):idx].max()
                
                # Rule 1: Breakout above resistance with Ultra High Volume (> 2.8x Avg Vol)
                if check_vol >= (avg_vol_then * 2.8) and df['Close'].iloc[idx] >= prior_high * 0.98:
                    found_anchor = True
                    anchor_idx = idx
                    resistance_level = prior_high

            if found_anchor:
                # Step 2: Check Pullback with Dry-up Volume
                pullback_days = i - anchor_idx
                if 2 <= pullback_days <= 15:
                    
                    pullback_zone = df.iloc[anchor_idx + 1 : i]
                    
                    # Support Rule: Pullback during squeeze shouldn't break resistance level by more than 4%
                    min_pullback_low = pullback_zone['Low'].min()
                    is_support_held = min_pullback_low >= (resistance_level * 0.96)
                    
                    # Volume Dry-up Rule: At least 50% days during pullback had volume below 20-day Avg
                    dry_days = sum(pullback_zone['Volume'] < pullback_zone['Vol_Avg_20'])
                    is_volume_dry = (dry_days / len(pullback_zone)) >= 0.40

                    if is_support_held and is_volume_dry:
                        # Step 3: Bullish Green Candle with High Volume (Trigger Today)
                        is_green_candle = (live_close > live_open) and (live_close > prev_close)
                        is_high_volume = live_vol >= (avg_vol_now * 1.5)
                        
                        # ENTRY SIGNAL
                        if is_green_candle and is_high_volume:
                            entry_price = live_close
                            stop_loss = round(min_pullback_low * 0.98, 2) # SL just below pullback low
                            risk = entry_price - stop_loss

                            # Strict Risk Filter (Risk between 2% to 7%)
                            if risk > 0 and 0.02 <= (risk / entry_price) <= 0.07:
                                target_price = round(entry_price + (risk * 2.0), 2) # 1:2 Risk-Reward
                                breakeven_trigger = entry_price + (risk * 1.0)
                                
                                future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                                win = False
                                exit_price = entry_price
                                curr_sl = stop_loss

                                for _, f_row in future_df.iterrows():
                                    # Trail SL to Breakeven at 1:1 R
                                    if f_row['High'] >= breakeven_trigger:
                                        curr_sl = max(curr_sl, entry_price)

                                    # Target Hit
                                    if f_row['High'] >= target_price:
                                        exit_price = target_price
                                        win = True
                                        break

                                    # Stop Loss Hit
                                    if f_row['Low'] <= curr_sl:
                                        exit_price = curr_sl
                                        win = exit_price > entry_price
                                        break

                                if exit_price == entry_price and not future_df.empty:
                                    exit_price = future_df['Close'].iloc[-1]
                                    win = exit_price > entry_price

                                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                                trades.append({"Win": win, "PnL_%": pnl_pct})

                                i += 8 # Skip to next swing
                                continue
        i += 1

    if not trades:
        return None

    return pd.DataFrame(trades)


# ===== 3. EXECUTE BACKTEST =====
all_trades = []

print("\nRunning V115.0 Resistance Breakout & Pullback Backtest Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        df_res = backtest_breakout_pullback_engine(df)
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
    print("🏆 RESULTS: V115.0 BREAKOUT & PULLBACK DRY-UP ENGINE")
    print("==================================================================")
    print(f"Total Quality Executed Trades  : {total_tr}")
    print(f"Average Win-Rate               : {round(win_rate, 2)}%")
    print(f"Profit Factor                  : {round(profit_factor, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the criteria in the backtest period.")
    
