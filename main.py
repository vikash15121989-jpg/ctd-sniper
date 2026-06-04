import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== WYCKOFF RANGE HUNTER V8.7: RANKED BY BREAKOUT PROBABILITY ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. DATE
date_raw = str(ws_watchlist.acell('A1').value).split(' ')[0]
date_formats = ['%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y', '%m/%d/%Y']
ref_date = None
for fmt in date_formats:
    try:
        ref_date = datetime.strptime(date_raw, fmt)
        break
    except ValueError:
        continue
if ref_date is None:
    raise ValueError(f"A1 me date galat: {date_raw}")
date_str = ref_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str} | RANKING: High Prob Breakout Upar")

# 3. NIFTY FOR RS
nifty_df = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. RANGE HUNTER + RANKING SCORE ✅
def find_clean_range(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 50:
        return None

    df_recent = df_till_date.tail(250).copy()
    base_windows = [20, 35, 50, 75, 100, 150, 200]
    best_base = None

    for window in base_windows:
        if len(df_recent) < window + 10: continue

        total_len = len(df_recent)
        for i in range(max(0, total_len - 120), total_len - window):
            base_window = df_recent.iloc[i:i+window]
            base_high = base_window['High'].max()
            base_low = base_window['Low'].min()

            # RULE-1: RANGE TIGHT
            range_pct = (base_high - base_low) / base_low * 100
            if range_pct > 25 or range_pct < 5: continue

            # RULE-2: VOLATILITY TIGHT
            daily_range = (base_window['High'] - base_window['Low']) / base_window['Close'] * 100
            if daily_range.mean() > 3.5: continue

            # RULE-3: VOLUME DRY UP
            vol_first_3rd = base_window['Volume'].iloc[:window//3].mean()
            vol_last_3rd = base_window['Volume'].iloc[-window//3:].mean()
            vol_dry_ratio = vol_last_3rd / vol_first_3rd if vol_first_3rd > 0 else 1
            if vol_dry_ratio > 0.80: continue

            # RULE-4: UP/DOWN VOLUME RATIO
            up_vol = base_window[base_window['Close'] > base_window['Open']]['Volume'].sum()
            down_vol = base_window[base_window['Close'] < base_window['Open']]['Volume'].sum()
            vol_ratio = up_vol / down_vol if down_vol > 0 else 99
            if vol_ratio < 1.15: continue

            # RULE-5: SPRING CHECK - BREAKDOWN RECLAIM ALLOWED ✅
            df_after_base = df_recent.iloc[i+window:]
            if len(df_after_base) < 3: continue

            spring_low = df_after_base['Low'].min()
            last_close = df_after_base.iloc[-1]['Close']
            creek_high = df_after_base['High'].max() if not df_after_base.empty else base_high

            # Spring logic
            if spring_low >= base_low * 0.99:
                spring_type = "No Spring"
                spring_strength = 1
            elif spring_low < base_low * 0.99 and last_close >= base_low * 0.98:
                spring_type = "Spring Reclaim"
                spring_strength = 3 # Strongest
            elif spring_low < base_low * 0.99 and last_close < base_low * 0.98:
                continue # Failed breakdown reject
            else:
                spring_type = "Weak Spring"
                spring_strength = 2

            if spring_low < base_low * 0.85: continue # 15% se zyada tod diya

            # RULE-6: BREAKOUT NAHI HUA HONA CHAHIYE
            if last_close > creek_high * 1.01: continue
            if not (base_low * 0.85 <= last_close <= creek_high * 1.01): continue

            # BREAKOUT PROBABILITY SCORE CALCULATION ✅
            dist_to_creek_pct = (creek_high - last_close) / last_close * 100
            tightness_score = 30 - range_pct # Tight range = high score
            demand_score = vol_ratio * 15 # High vol ratio = high score
            dry_score = (1 - vol_dry_ratio) * 20 # Zyada dry = high score
            spring_bonus = spring_strength * 10 # Spring Reclaim = 30 bonus
            proximity_score = max(0, 20 - dist_to_creek_pct * 4) # Creek ke paas = high score
            length_bonus = window * 0.05 # Lambi range = slight bonus
            freshness_penalty = (total_len - i) * 0.1 # Purani range = penalty

            breakout_score = (tightness_score + demand_score + dry_score +
                            spring_bonus + proximity_score + length_bonus - freshness_penalty)

            if best_base is None or breakout_score > best_base['breakout_score']:
                best_base = {
                    'base_high': base_high, 'base_low': base_low, 'creek_high': creek_high,
                    'base_length': window, 'range_pct': range_pct,
                    'vol_ratio': vol_ratio, 'vol_dry_ratio': vol_dry_ratio,
                    'spring_low': spring_low, 'spring_type': spring_type,
                    'dist_to_creek_pct': dist_to_creek_pct,
                    'breakout_score': breakout_score
                }

    return best_base

# 5. MAIN LOOP
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue

        # Liquidity check
        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        avg_vol = last_candle['Vol_50']
        if pd.isna(avg_vol) or avg_vol < 1000000:
            print(f" ❌ Volume low")
            continue

        # RS check
        stock_ret_63d = df['Close'].pct_change(63).iloc[-1] * 100
        nifty_ret_63d = nifty_close.pct_change(63).iloc[-1] * 100
        rs_rating = stock_ret_63d - nifty_ret_63d if not pd.isna(stock_ret_63d) else -99
        if rs_rating < -3:
            print(f" ❌ RS weak: {rs_rating:.1f}%")
            continue

        # 200 EMA filter - Downtrend reject
        df['EMA200'] = df['Close'].ewm(span=200).mean()
        if df.iloc[-1]['Close'] < df.iloc[-1]['EMA200'] * 0.90:
            print(f" ❌ 200EMA se 10% neeche")
            continue

        base_info = find_clean_range(df, ref_date)
        if base_info is None:
            print(f" ❌ Clean range nahi")
            continue

        # ENTRY = CREEK HIGH
        entry_level = base_info['creek_high']
        stop_loss = base_info['spring_low'] * 0.97
        risk_pct = round((entry_level - stop_loss) / entry_level * 100, 1)
        if risk_pct > 15: continue

        target_8pct = entry_level * 1.08
        target_15pct = entry_level * 1.15
        rr = round((target_8pct - entry_level) / (entry_level - stop_loss), 1)

        dist_to_entry = base_info['dist_to_creek_pct']

        if dist_to_entry <= 0.5:
            entry_status = "BUY ZONE"
        elif dist_to_entry <= 2:
            entry_status = "ALERT"
        elif dist_to_entry <= 5:
            entry_status = "WATCH"
        else:
            entry_status = "WATCHLIST"

        print(f" ✅ {base_info['base_length']}D | {base_info['spring_type']} | Score:{base_info['breakout_score']:.0f} | {entry_status}")

        signals.append({
            'Rank_Score': round(base_info['breakout_score'], 0),
            'Stock': stock,
            'Status': entry_status,
            'Range_Days': base_info['base_length'],
            'Entry_Creek_High': round(entry_level, 2),
            'CMP': round(last_candle['Close'], 2),
            'Dist_to_Entry_%': round(dist_to_entry, 1),
            'Range_Low': round(base_info['base_low'], 2),
            'Range_High': round(base_info['base_high'], 2),
            'Spring_Type': base_info['spring_type'],
            'Spring_Low': round(base_info['spring_low'], 2),
            'Stop_Loss': round(stop_loss, 2),
            'Risk_%': risk_pct,
            'Target_8%': round(target_8pct, 2),
            'Target_15%': round(target_15pct, 2),
            'R:R': rr,
            'Vol_Ratio': round(base_info['vol_ratio'], 2),
            'Vol_Dry_%': round((1-base_info['vol_dry_ratio'])*100, 0),
            'Range_%': round(base_info['range_pct'], 1),
            'RS_vs_Nifty': round(rs_rating, 1),
            'AvgVol_Lakh': round(avg_vol/100000, 1)
        })

        if (i + 1) % 30 == 0:
            time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. SHEET UPDATE - RANKED ✅
try:
    ws_output = sh.worksheet("RangeSetups")
except:
    ws_output = sh.add_worksheet(title="RangeSetups", rows=1000, cols=25)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    # RANKING: 1. Rank_Score sabse upar 2. BUY ZONE first 3. Creek ke paas
    status_priority = {'BUY ZONE': 1, 'ALERT': 2, 'WATCH': 3, 'WATCHLIST': 4}
    df_out['Priority'] = df_out['Status'].map(status_priority)
    df_out = df_out.sort_values(['Rank_Score', 'Priority', 'Dist_to_Entry_%'],
                                ascending=[False, True, True])
    df_out = df_out.drop(['Priority'], axis=1)
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {len(signals)} RANGES | TOP RANK = HIGHEST BREAKOUT PROB ===")
else:
    ws_output.update('A1', [["Ref_Date", "Status"], [date_str, "No Clean Ranges Found"]])
    print("\n=== DONE: 0 SETUPS ===")
