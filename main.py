import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== V112.2: PURE VOL-ACCUMULATION ENGINE BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_AVG_VOLUME = 100_000         # Min 1 Lakh Daily Volume
MIN_AVG_TURNOVER_CR = 2.0        # Min ₹2 Crore Daily Turnover
MAX_HOLDING_DAYS = 30             # Positional Holding Window

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


# ===== 2. BACKTEST ENGINE FOR V112.2 SETUP =====
def backtest_v112_engine(df_daily):
    trades = []
    df = df_daily.copy()

    # Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Vol_Avg_20'] = df['Volume'].rolling(20).mean()
    df['Turnover_Avg_20_Cr'] = df['Turnover'].rolling(20).mean() / 10_000_000

    n = len(df)
    i = 60  # Require minimum 60 bars for initial window

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Check
        if df['Vol_Avg_20'].iloc[i] >= MIN_AVG_VOLUME and df['Turnover_Avg_20_Cr'].iloc[i] >= MIN_AVG_TURNOVER_CR:
            
            live_close = df['Close'].iloc[i]
            live_open = df['Open'].iloc[i]
            live_high = df['High'].iloc[i]
            live_low = df['Low'].iloc[i]
            live_vol = df['Volume'].iloc[i]
            prev_close = df['Close'].iloc[i - 1]

            # 1. Look for valid Anchor Candle in past 40 days
            possible_anchors = []
            for idx in range(i - 40, i - 1):
                if idx < 20: continue
                check_vol = df['Volume'].iloc[idx]
                check_close = df['Close'].iloc[idx]
                check_open = df['Open'].iloc[idx]
                avg_vol_then = df['Vol_Avg_20'].iloc[idx - 1]

                if pd.isna(avg_vol_then) or avg_vol_then == 0: continue

                # Anchor Condition: 3x Volume + Green Candle
                if check_vol > (avg_vol_then * 3.0) and check_close > check_open:
                    possible_anchors.append(idx)

            if possible_anchors:
                anchor_idx = possible_anchors[-1] # Best fresh anchor
                
                # Pre-Anchor Support (Swing Low 20 days prior to anchor)
                pre_anchor_zone = df['Low'].iloc[max(0, anchor_idx - 20) : anchor_idx]
                pre_anchor_support = pre_anchor_zone.min() if not pre_anchor_zone.empty else df['Low'].iloc[anchor_idx]

                # Support Safety & Volume Dry-up check post-anchor
                is_support_safe = True
                dry_up_days = 0
                total_base_days = i - anchor_idx

                for check_idx in range(anchor_idx + 1, i + 1):
                    f_close = df['Close'].iloc[check_idx]
                    f_vol = df['Volume'].iloc[check_idx]
                    avg_vol_day = df['Vol_Avg_20'].iloc[check_idx]

                    if f_close < pre_anchor_support:
                        is_support_safe = False
                        break

                    if not pd.isna(avg_vol_day) and f_vol < avg_vol_day:
                        dry_up_days += 1

                if is_support_safe and total_base_days >= 2:
                    # Trigger Conditions (Ready Today)
                    past_10d_vol = df['Volume'].iloc[i - 11 : i]
                    max_vol_10d = past_10d_vol.max() if not past_10d_vol.empty else 0

                    is_vol_breakout = live_vol > max_vol_10d
                    is_price_up = (live_close > prev_close) and (live_close >= live_open)
                    mid_point = (live_high + live_low) / 2.0
                    is_close_near_high = live_close > mid_point

                    # ENTRY SIGNAL TRIGGERED
                    if is_vol_breakout and is_price_up and is_close_near_high:
                        entry_price = live_close
                        stop_loss = round(pre_anchor_support, 2)
                        target_price = round(entry_price * 1.15, 2) # +15% Fixed Target
                        risk = entry_price - stop_loss

                        if risk > 0 and (risk / entry_price) <= 0.12: # Max 12% Risk Cap
                            future_df = df.iloc[i + 1 : i + 1 + MAX_HOLDING_DAYS]
                            win = False
                            exit_price = entry_price

                            for _, f_row in future_df.iterrows():
                                # Target Hit (+15%)
                                if f_row['High'] >= target_price:
                                    exit_price = target_price
                                    win = True
                                    break
                                # Stop Loss Hit (Pre-Anchor Support)
                                if f_row['Low'] <= stop_loss:
                                    exit_price = stop_loss
                                    win = False
                                    break

                            # Time-Based Exit (Max 30 Days)
                            if exit_price == entry_price and not future_df.empty:
                                exit_price = future_df['Close'].iloc[-1]
                                win = exit_price > entry_price

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({"Win": win, "PnL_%": pnl_pct})

                            i += 10 # Skip forward to avoid duplicate entries on same swing
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
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0

    return {
        "Trades": total_tr,
        "Win_Rate": win_rate,
        "Gross_Profit": gross_profit,
        "Gross_Loss": gross_loss,
        "Profit_Factor": profit_factor
    }


# ===== 3. EXECUTE BACKTEST ACROSS ALL STOCKS =====
all_trades = 0
all_profit = 0.0
all_loss = 0.0
winrate_list = []

print("\nRunning V112.2 Engine Backtest...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 100:
            continue

        res = backtest_v112_engine(df)
        if res:
            all_trades += res["Trades"]
            all_profit += res["Gross_Profit"]
            all_loss += res["Gross_Loss"]
            winrate_list.append(res["Win_Rate"])
    except Exception:
        pass

if all_trades > 0:
    avg_winrate = np.mean(winrate_list)
    overall_pf = all_profit / all_loss if all_loss > 0 else 999.0

    print("\n==================================================================")
    print("🏆 RESULTS: V112.2 PURE VOL-ACCUMULATION ENGINE")
    print("==================================================================")
    print(f"Total Executed Quality Trades  : {all_trades}")
    print(f"Average Win-Rate               : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the V112.2 criteria in the 3-year window.")
    
