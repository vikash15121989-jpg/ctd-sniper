import json
import os
import warnings
from datetime import datetime, timedelta
import gspread
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

print("=== WYCKOFF ACCUMULATION -> MARKUP MOTHER CANDLE DRY VOLUME BACKTEST ===", flush=True)

# ===== CONFIGURATION =====
MIN_DAILY_TURNOVER = 50_000_000   # Min ₹5 Crore Daily Liquidity
MAX_HOLDING_DAYS = 40             # Positional Holding Period up to 40 Days

END_DATE = datetime.now().date()
START_DATE = END_DATE - timedelta(days=1095) # 3 Years Historical Data

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
def backtest_wyckoff_markup_squeeze(df_daily):
    trades = []
    df = df_daily.copy()

    # Calculate Moving Averages & Volume Indicators
    df['Turnover'] = df['Close'] * df['Volume']
    df['Turnover_MA20'] = df['Turnover'].rolling(20).mean()
    df['SMA20'] = df['Close'].rolling(20).mean()
    df['EMA50'] = df['Close'].ewm(span=50, adjust=False).mean()
    df['Vol_SMA20'] = df['Volume'].rolling(20).mean()

    n = len(df)
    i = 80 # Warmup window for Accumulation & Moving Averages

    while i < n - MAX_HOLDING_DAYS:
        # Liquidity Check
        if df['Turnover_MA20'].iloc[i] >= MIN_DAILY_TURNOVER:
            
            # Step 1: Wyckoff Accumulation Check (Past 40-60 days Volatility Squeeze / Range)
            past_accum = df.iloc[i-50 : i-10]
            accum_high = past_accum['High'].max()
            accum_low = past_accum['Low'].min()
            accum_range_pct = (accum_high - accum_low) / accum_low

            # Accumulation Range should be relatively tight (<= 25% price height over 40 days)
            if accum_range_pct <= 0.25:
                
                # Step 2: Markup Phase Confirmation (Stock trading above 20 SMA, 50 EMA & breaking accum high)
                curr_close = df['Close'].iloc[i]
                sma20_val = df['SMA20'].iloc[i]
                ema50_val = df['EMA50'].iloc[i]

                if curr_close > sma20_val and curr_close > ema50_val and curr_close >= accum_high * 0.95:
                    
                    # Step 3: High Volume Mother Candle near 20 MA
                    curr_vol = df['Volume'].iloc[i]
                    avg_vol = df['Vol_SMA20'].iloc[i]
                    is_green = df['Close'].iloc[i] > df['Open'].iloc[i]

                    if is_green and curr_vol >= avg_vol * 1.5:
                        mother_high = df['High'].iloc[i]
                        mother_vol = curr_vol
                        mother_low = df['Low'].iloc[i]

                        # Lookahead 1-4 days for Dry Volume (< 60% of Mother Candle) + Breakout
                        for d in range(1, 5):
                            if i + d < n:
                                dry_vol = df['Volume'].iloc[i + d]

                                # Step 4: Volume Drop Condition (< 60% of Mother Candle Volume)
                                if dry_vol < mother_vol * 0.60:
                                    
                                    # Look for Entry Trigger: Price Closing above Mother Candle High
                                    for entry_d in range(d + 1, d + 5):
                                        if i + entry_d < n:
                                            trig_close = df['Close'].iloc[i + entry_d]
                                            prev_close = df['Close'].iloc[i + entry_d - 1]

                                            if trig_close > mother_high and prev_close <= mother_high:
                                                entry_price = trig_close
                                                squeeze_low = df['Low'].iloc[i : i + entry_d].min()
                                                stop_loss = squeeze_low
                                                risk = entry_price - stop_loss

                                                # Risk Cap (2% to 8% max risk per trade)
                                                if risk > 0 and 0.02 <= (risk / entry_price) <= 0.08:
                                                    target_price = entry_price + (risk * 2.0) # 1:2 RR Target
                                                    future_df = df.iloc[i + entry_d + 1 : i + entry_d + 1 + MAX_HOLDING_DAYS]

                                                    win = False
                                                    exit_price = entry_price

                                                    for _, f_row in future_df.iterrows():
                                                        # Hit Target
                                                        if f_row['High'] >= target_price:
                                                            exit_price = target_price
                                                            win = True
                                                            break
                                                        # Hit Stop Loss
                                                        if f_row['Low'] <= stop_loss:
                                                            exit_price = stop_loss
                                                            win = False
                                                            break
                                                        # Exit on 20 SMA Closing Breakdown
                                                        if f_row['Close'] < f_row['SMA20']:
                                                            exit_price = f_row['Close']
                                                            win = exit_price > entry_price
                                                            break

                                                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                                                    trades.append({"Win": win, "PnL_%": pnl_pct})

                                                    i += entry_d + 5 # Fast-forward index
                                                    break
                                            break
                                    break
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

print("\nRunning Wyckoff Accumulation -> Markup Squeeze Engine...", flush=True)

for stock in STOCKS:
    try:
        df = yf.download(stock, start=START_DATE, end=END_DATE, progress=False, auto_adjust=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.empty or len(df) < 150:
            continue

        res = backtest_wyckoff_markup_squeeze(df)
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
    print("🏆 RESULTS: WYCKOFF ACCUMULATION + MOTHER CANDLE DRY SQUEEZE")
    print("==================================================================")
    print(f"Total Executed Trades          : {all_trades}")
    print(f"Average Win-Rate               : {round(avg_winrate, 2)}%")
    print(f"Profit Factor                  : {round(overall_pf, 2)}")
    print("==================================================================")
else:
    print("\nNo trades met the exact Wyckoff criteria.")
    
