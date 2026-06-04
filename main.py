import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== WYCKOFF RANGE HUNTER V8.7: ACCUMULATION vs DISTRIBUTION + RANKED ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 DATE HANDLING
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
    raise ValueError(f"A1 me date format galat: {date_raw}")

date_str = ref_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str} | RANGE: 20D to 200D | BREAKOUT RANKING ON")

# 3. NIFTY DATA FOR RS RATING
nifty_df = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. RANGE HUNTER - ACCUMULATION vs DISTRIBUTION + VSA ✅
def analyze_base_context(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 100:
        return None

    # BUYING CLIMAX REJECT - Distribution top
    recent_30 = df_till_date.tail(30)
    if len(recent_30) >= 20:
        vol_avg_50 = df_till_date['Volume'].tail(50).mean()
        high_20d = recent_30['High'].rolling(20).max()
        climax_check = recent_30[
            (recent_30['Volume'] > vol_avg_50 * 2.0) &
            (recent_30['High'] >= high_20d * 0.98) &
            (recent_30['Close'] < recent_30['High'] * 0.97) &
            (recent_30['Close'] < recent_30['Open'])
        ]
        if not climax_check.empty:
            return None

    df_recent = df_till_date.tail(250).copy() # 1 saal tak range check
    base_windows = [20, 35, 50, 75, 100, 150, 200] # 20 din se 200 din ✅
    best_base = None

    for window in base_windows:
        if len(df_recent) < window + 20: continue

        total_len = len(df_recent)
        # Last 120 din me fresh range dhundo
        for i in range(max(0, total_len - 120), total_len - window):
            base_window = df_recent.iloc[i:i+window]
            base_high = base_window['High'].max() # RESISTANCE ✅
            base_low = base_window['Low'].min() # SUPPORT ✅

            # RULE-1: RANGE TIGHT 5% to 25%
            range_pct = (base_high - base_low) / base_low * 100
            if range_pct > 25 or range_pct < 5: continue

            # RULE-2: VOLATILITY TIGHT - Range ka saboot
            daily_range = (base_window['High'] - base_window['Low']) / base_window['Close'] * 100
            if daily_range.mean() > 3.5: continue

            # RULE-3: VOLUME DRY UP % - VSA
            vol_first_3rd = base_window['Volume'].iloc[:window//3].mean()
            vol_last_3rd = base_window['Volume'].iloc[-window//3:].mean()
            vol_dry_ratio = vol_last_3rd / vol_first_3rd if vol_first_3rd > 0 else 1
            if vol_dry_ratio > 0.80: continue # 20% se kam sukha to reject

            # RULE-4: UP/DOWN VOLUME RATIO - Accumulation vs Distribution ✅
            up_vol = base_window[base_window['Close'] > base_window['Open']]['Volume'].sum()
            down_vol = base_window[base_window['Close'] < base_window['Open']]['Volume'].sum()
            vol_ratio = up_vol / down_vol if down_vol > 0 else 99

            # VSA CLASSIFICATION
            if vol_ratio >= 1.15:
                vsa_type = 'Accumulation' # Demand > Supply
            elif vol_ratio <= 0.85:
                vsa_type = 'Distribution' # Supply > Demand - REJECT
                continue
            else:
                vsa_type = 'Neutral'
                continue # Clear bias nahi

            # RULE-5: SPRING CHECK - Breakdown Reclaim Allowed ✅
            df_after_base = df_recent.iloc[i+window:]
            if len(df_after_base) < 3: continue

            spring_low = df_after_base['Low'].min()
            last_close = df_after_base.iloc[-1]['Close']
            creek_high = df_after_base['High'].max() if not df_after_base.empty else base_high

            # Spring Type
            if spring_low >= base_low * 0.99:
                spring_type = "No Spring"
                spring_strength = 1
            elif spring_low < base_low * 0.99 and last_close >= base_low * 0.98:
                spring_type = "Spring Reclaim" # Strongest
                spring_strength = 3
            elif spring_low < base_low * 0.99 and last_close < base_low * 0.98:
                continue # Failed breakdown
            else:
                spring_type = "Weak Spring"
                spring_strength = 2

            if spring_low < base_low * 0.85: continue # 15% se zyada tod diya

            # RULE-6: BREAKOUT NAHI HUA HONA CHAHIYE ✅
            if last_close > creek_high * 1.01: continue # 1% bhi upar = reject
            if not (base_low * 0.85 <= last_close <= creek_high * 1.01): continue

            # 52W LOCATION - Info only
            yearly_high = df_till_date['High'].tail(252).max()
            yearly_low = df_till_date['Low'].tail(252).min()
            location_pct = (base_low - yearly_low) / (yearly_high - yearly_low) * 100 if yearly_high!= yearly_low else 50

            # BREAKOUT PROBABILITY SCORE - JITNA BADA ACCUMULATION UTNA ACHHA ✅
            dist_to_creek_pct = (creek_high - last_close) / last_close * 100
            tightness_score = 30 - range_pct
            demand_score = vol_ratio * 15
            dry_score = (1 - vol_dry_ratio) * 25 # Dry volume ko zyada weight
            spring_bonus = spring_strength * 10
            proximity_score = max(0, 25 - dist_to_creek_pct * 5) # Creek ke paas bonus
            length_bonus = window * 0.1 # 200D range = 20 bonus points ✅
            location_bonus = max(0, 15 - location_pct * 0.3) # Low ke paas bonus
            freshness_penalty = (total_len - i) * 0.1

            breakout_score = (tightness_score + demand_score + dry_score +
                            spring_bonus + proximity_score + length_bonus +
                            location_bonus - freshness_penalty)

            if best_base is None or breakout_score > best_base['breakout_score']:
                best_base = {
                    'base_high': base_high, 'base_low': base_low, 'creek_high': creek_high,
                    'base_length': window, 'range_pct': range_pct, 'vsa_type': vsa_type,
                    'vol_ratio': vol_ratio, 'vol_dry_ratio': vol_dry_ratio,
                    'spring_low': spring_low, 'spring_type': spring_type,
                    'dist_to_creek_pct': dist_to_creek_pct, 'location_pct': location_pct,
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
        if len(df) < 150: continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        avg_vol = last_candle['Vol_50']
        avg_turnover = avg_vol * last_candle['Close']

        # Liquidity filter
        if pd.isna(avg_vol) or avg_vol < 1000000 or avg_turnover < 30000000:
            print(f" ❌ Liquidity low")
            continue

        # RS RATING
        stock_ret_63d = df['Close'].pct_change(63).iloc[-1] * 100
        nifty_ret_63d = nifty_close.pct_change(63).iloc[-1] * 100
        rs_rating = stock_ret_63d - nifty_ret_63d if not pd.isna(stock_ret_63d) else 0
        if rs_rating < -5:
            print(f" ❌ RS weak: {rs_rating:.1f}%")
            continue

        # 200 EMA FILTER - Downtrend reject
        df['EMA200'] = df['Close'].ewm(span=200).mean()
        if df.iloc[-1]['Close'] < df.iloc[-1]['EMA200'] * 0.90:
            print(f" ❌ 200EMA se 10% neeche")
            continue

        base_info = analyze_base_context(df, ref_date)
        if base_info is None:
            print(f" ❌ Clean range/VSA nahi")
            continue

        # ENTRY = CREEK HIGH = RESISTANCE ✅
        entry_level = base_info['creek_high']
        support_level = base_info['base_low'] # SUPPORT ✅
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

        print(f" ✅ {base_info['base_length']}D | {base_info['vsa_type']} | {base_info['spring_type']} | Score:{base_info['breakout_score']:.0f}")

        signals.append({
            'Rank_Score': round(base_info['breakout_score'], 0),
            'Stock': stock,
            'Status': entry_status,
            'VSA_Type': base_info['vsa_type'],
            'Range_Days': base_info['base_length'],
            'Resistance_Creek': round(entry_level, 2), # RESISTANCE ✅
            'Support_Base': round(support_level, 2), # SUPPORT ✅
            'CMP': round(last_candle['Close'], 2),
            'Dist_to_Entry_%': round(dist_to_entry, 1),
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
            'Location_52W_%': round(base_info['location_pct'], 0),
            'RS_vs_Nifty': round(rs_rating, 1),
            'AvgVol_Lakh': round(avg_vol/100000, 1)
        })

        if (i + 1) % 30 == 0:
            time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. SHEET UPDATE - RANKED BY BREAKOUT PROBABILITY ✅
try:
    ws_output = sh.worksheet("RangeSetups")
except:
    ws_output = sh.add_worksheet(title="RangeSetups", rows=1000, cols=25)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    # RANKING: Score sabse upar, fir BUY ZONE, fir Creek ke paas
    status_priority = {'BUY ZONE': 1, 'ALERT': 2, 'WATCH': 3, 'WATCHLIST': 4}
    df_out['Priority'] = df_out['Status'].map(status_priority)
    df_out = df_out.sort_values(['Rank_Score', 'Priority', 'Dist_to_Entry_%'],
                                ascending=[False, True, True])
    df_out = df_out.drop(['Priority'], axis=1)
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {len(signals)} RANGES | TOP = HIGHEST BREAKOUT CHANCE ===")
else:
    ws_output.update('A1', [["Ref_Date", "Status"], [date_str, "No Clean Ranges Found"]])
    print("\n=== DONE: 0 SETUPS ===")
