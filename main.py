import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== WEEKLY MOTHER CANDLE DRY VOLUME BREAKOUT BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_TURNOVER = 20_000_000   # ₹2 Crore Daily Trading Value
MAX_HOLDING_DAYS = 60       # Positional Holding up to 60 Trading Days

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Data

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
    print(f"✅ Total Stocks Loaded: {len(STOCKS)}", flush=True)

except Exception as e:
    print(f"❌ Error Reading Watchlist: {e}")
    exit(1)


# ===== 2. MOTHER CANDLE & DRY VOLUME BACKTEST ENGINE =====
def backtest_mother_candle_breakout(df_daily):
    trades = []
    df_d = df_daily.copy()

    # Calculate Daily Turnover
    df_d['Turnover'] = df_d['Close'] * df_d['Volume']
    df_d['Turnover_MA20'] = df_d['Turnover'].rolling(20).mean()

    # Resample Daily to Weekly Data
    df_w = df_d.resample('W').agg({
        'Open': 'first',
        'High': 'max',
        'Low': 'min',
        'Close': 'last',
        'Volume': 'sum'
    }).dropna()

    if len(df_w) < 30:
        return None

    # Weekly Metrics
    df_w['Range'] = df_w['High'] - df_w['Low']
    df_w['Range_MA10'] = df_w['Range'].rolling(10).mean()
    df_w['Vol_MA10'] = df_w['Volume'].rolling(10).mean()

    # 1. Mother Candle Identification Condition (Green + High Range + High Volume)
    df_w['Is_Mother_Candle'] = (df_w['Close'] > df_w['Open']) & \
                               (df_w['Range'] > df_w['Range_MA10'] * 1.3) & \
                               (df_w['Volume'] > df_w['Vol_MA10'] * 1.5)

    n_d = len(df_d)
    n_w = len(df_w)

    for w_idx in range(15, n_w - 8):
        if df_w['Is_Mother_Candle'].iloc[w_idx]:
            mother_high = df_w['High'].iloc[w_idx]
            mother_low = df_w['Low'].iloc[w_idx]
            mother_vol = df_w['Volume'].iloc[w_idx]
            mother_date_end = df_w.index[w_idx]

            # 2. Check subsequent 1-3 weeks for Volume Dry Phase
            dry_phase_valid = False
            min_dry_vol = float('inf')

            for k in range(1, 4):
                if w_idx + k < n_w:
                    next_vol = df_w['Volume'].iloc[w_idx + k]
                    next_high = df_w['High'].iloc[w_idx + k]
                    next_low = df_w['Low'].iloc[w_idx + k]

                    # Price inside Mother Candle Range & Volume Squeezed (<50% of Mother Vol)
                    if next_vol < (mother_vol * 0.50) and next_high <= mother_high * 1.02 and next_low >= mother_low * 0.98:
                        dry_phase_valid = True
                        if next_vol < min_dry_vol:
                            min_dry_vol = next_vol

            if dry_phase_valid:
                # 3. Switch to Daily Chart post Mother Candle
                daily_sub = df_d[df_d.index > mother_date_end]

                if len(daily_sub) < 10:
                    continue

                for d_i in range(1, min(40, len(daily_sub) - MAX_HOLDING_DAYS)):
                    curr_row = daily_sub.iloc[d_i]
                    prev_row = daily_sub.iloc[d_i - 1]

                    # Watchlist Condition: Daily Volume starts expanding above dry volume level
                    vol_expanding = curr_row['Volume'] > (min_dry_vol / 5.0)

                    # Trigger: Daily Close breaks Weekly Mother Candle High
                    if vol_expanding and curr_row['Close'] > mother_high and prev_row['Close'] <= mother_high:
                        
                        # Liquidity Filter Check
                        if curr_row['Turnover_MA20'] < MIN_TURNOVER:
                            break

                        entry_price = curr_row['Close']
                        
                        # Stop Loss = Lowest point of Mother Candle Range
                        stop_loss = mother_low
                        risk = entry_price - stop_loss

                        if risk > 0 and (risk / entry_price) <= 0.08: # Max 8% Risk Cap
                            future_df = daily_sub.iloc[d_i + 1 : d_i + 1 + MAX_HOLDING_DAYS]

                            win = False
                            exit_price = entry_price
                            trail_sl = stop_loss

                            for _, f_row in future_df.iterrows():
                                # Trailing Stop Loss once price moves > 1.5R in profit
                                if f_row['Close'] > entry_price + (risk * 1.5):
                                    trail_sl = max(trail_sl, f_row['Low'])

                                if f_row['Low'] <= trail_sl:
                                    exit_price = trail_sl
                                    win = exit_price > entry_price
                                    break

                            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                            trades.append({"Win": win, "PnL_%": pnl_pct})
                            break # Move to next mother candle event

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

print("\nRunning Mother Candle Dry Volume Breakout Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 200:
            continue

        res = backtest_mother_candle_breakout(df)
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
    print("🏆 RESULTS: WEEKLY MOTHER CANDLE DRY VOLUME BREAKOUT")
    print("==================================================================")
    print(f"Total Quality Trades Executed : {all_trades}")
    print(f"Average Win-Rate                : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                   : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo mother candle trades met the exact criteria.")
    
