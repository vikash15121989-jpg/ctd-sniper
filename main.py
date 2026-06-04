import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== WYCKOFF V10.0: GIRAAVAT KE BAAD BASE + ACCUMULATION + VCP ===")

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
print(f"Reference Date: {date_str} | RULE: Giraavat ke baad Base + Accumulation")

# 3. NIFTY DATA FOR RS RATING
nifty_df = yf.download("^NSEI", period="2y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. VCP DETECTOR
def detect_vcp(range_df):
    if len(range_df) < 30:
        return "No VCP", 0, False

    part_len = len(range_df) // 3
    part1 = range_df.iloc[:part_len]
    part2 = range_df.iloc[part_len:2*part_len]
    part3 = range_df.iloc[2*part_len:]

    range1 = (part1['High'].max() - part1['Low'].min()) / part1['Low'].min() * 100
    range2 = (part2['High'].max() - part2['Low'].min()) / part2['Low'].min() * 100
    range3 = (part3['High'].max() - part3['Low'].min()) / part3['Low'].min() * 100

    vol1 = part1['Volume'].mean()
    vol2 = part2['Volume'].mean()
    vol3 = part3['Volume'].mean()

    contractions = 0
    if range2 < range1 * 0.7: contractions += 1
    if range3 < range2 * 0.7: contractions += 1
    if vol3 < vol1 * 0.6: contractions += 1

    if contractions >= 2 and range3 < 5:
        return "Strong VCP", 3, True
    elif contractions >= 1 and range3 < 8:
        return "Weak VCP", 2, True
    else:
        return "No VCP", 1, False

# 5. BASE HUNTER V10.0 - GIRAAVAT KE BAAD ✅
def find_base_after_decline(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 100:
        return None

    # RULE-1: GIRAAVAT CHECK - 52W High se 20%+ neeche hona chahiye ✅
    yearly_high = df_till_date['High'].tail(252).max()
    last_close = df_till_date['Close'].iloc[-1]
    decline_pct = (yearly_high - last_close) / yearly_high * 100

    if decline_pct < 20: # 20% se kam gira to reject - rally me hai
        return None

    # RULE-2: LAST 60 DIN ME SABSE TIGHT RANGE DHUNDO ✅
    df_recent = df_till_date.tail(200).copy() # 200 din me dekho
    best_base = None
    best_length = 0

    # 20 se 150 din tak ki har possible range check karo
    for length in range(150, 19, -5): # 150D se 20D tak, badi range pehle
        if len(df_recent) < length: continue

        range_df = df_recent.tail(length)
        base_high = range_df['High'].max()
        base_low = range_df['Low'].min()
        range_pct = (base_high - base_low) / base_low * 100

        # 5-25% range chahiye
        if range_pct < 5 or range_pct > 25: continue

        # Daily volatility check
        daily_range = (range_df['High'] - range_df['Low']) / range_df['Close'] * 100
        if daily_range.mean() > 3.5: continue

        # ACCUMULATION CHECK - COMPULSORY ✅
        vol_first_3rd = range_df['Volume'].iloc[:length//3].mean()
        vol_last_3rd = range_df['Volume'].iloc[-length//3:].mean()
        vol_dry_ratio = vol_last_3rd / vol_first_3rd if vol_first_3rd > 0 else 1

        up_vol = range_df[range_df['Close'] > range_df['Open']]['Volume'].sum()
        down_vol = range_df[range_df['Close'] < range_df['Open']]['Volume'].sum()
        vol_ratio = up_vol / down_vol if down_vol > 0 else 99

        is_accumulation = vol_ratio >= 1.15 and vol_dry_ratio <= 0.80
        if not is_accumulation: continue # Accumulation nahi to reject

        # VCP CHECK - Bonus
        vcp_status, vcp_strength, has_vcp = detect_vcp(range_df)

        # 52W Location
        yearly_low = df_till_date['Low'].tail(252).min()
        location_pct = (base_low - yearly_low) / (yearly_high - yearly_low) * 100 if yearly_high!= yearly_low else 50

        # Best base mil gaya - sabse lamba
        best_base = {
            'base_high': base_high, 'base_low': base_low, 'base_length': length,
            'range_pct': range_pct, 'vol_ratio': vol_ratio, 'vol_dry_ratio': vol_dry_ratio,
            'vcp_status': vcp_status, 'vcp_strength': vcp_strength, 'has_vcp': has_vcp,
            'location_pct': location_pct, 'decline_pct': decline_pct,
            'last_close': last_close
        }
        best_length = length
        break # Sabse lamba mil gaya, ruk jao

    if best_base is None:
        return None

    # SCORE CALCULATION
    length_bonus = best_base['base_length'] * 0.5 # 150D = 75 points
    tightness_score = 30 - best_base['range_pct']
    demand_score = best_base['vol_ratio'] * 15
    dry_score = (1 - best_base['vol_dry_ratio']) * 25
    vcp_bonus = best_base['vcp_strength'] * 15
    location_bonus = max(0, 15 - best_base['location_pct'] * 0.3)
    decline_bonus = best_base['decline_pct'] * 0.2 # 50% gira = 10 points

    breakout_score = (length_bonus + tightness_score + demand_score +
                    dry_score + vcp_bonus + location_bonus + decline_bonus)

    best_base['breakout_score'] = breakout_score
    return best_base

# 6. MAIN LOOP
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

        if pd.isna(avg_vol) or avg_vol < 1000000 or avg_turnover < 30000000:
            print(f" ❌ Liquidity low")
            continue

        # 200 EMA filter hata diya - neeche wale stock bhi chalenge
        base_info = find_base_after_decline(df, ref_date)
        if base_info is None:
            print(f" ❌ Giraavat ke baad Base+Accumulation nahi")
            continue

        entry_level = base_info['base_high'] # Creek
        support_level = base_info['base_low']
        stop_loss = support_level * 0.95 # Base low se 5% neeche
        risk_pct = round((entry_level - stop_loss) / entry_level * 100, 1)
        if risk_pct > 20: continue

        target_15pct = entry_level * 1.15
        target_30pct = entry_level * 1.30
        rr = round((target_15pct - entry_level) / (entry_level - stop_loss), 1)

        dist_to_entry = (entry_level - base_info['last_close']) / base_info['last_close'] * 100

        # STATUS - Rally se matlab nahi ✅
        if base_info['has_vcp']:
            entry_status = "ACCUM+VCP"
        else:
            entry_status = "ACCUM-BASE"

        print(f" ✅ {base_info['base_length']}D Base | -{base_info['decline_pct']:.0f}% Gira | {base_info['vcp_status']} | Score:{base_info['breakout_score']:.0f}")

        signals.append({
            'Rank_Score': round(base_info['breakout_score'], 0),
            'Stock': stock,
            'Setup_Type': entry_status,
            'VCP_Status': base_info['vcp_status'],
            'Base_Days': base_info['base_length'],
            'Decline_52W_%': round(base_info['decline_pct'], 0),
            'Resistance_Creek': round(entry_level, 2),
            'Support_Base': round(support_level, 2),
            'CMP': round(base_info['last_close'], 2),
            'Dist_to_Creek_%': round(dist_to_entry, 1),
            'Stop_Loss': round(stop_loss, 2),
            'Risk_%': risk_pct,
            'Target_15%': round(target_15pct, 2),
            'Target_30%': round(target_30pct, 2),
            'R:R': rr,
            'Vol_Ratio': round(base_info['vol_ratio'], 2),
            'Vol_Dry_%': round((1-base_info['vol_dry_ratio'])*100, 0),
            'Range_%': round(base_info['range_pct'], 1),
            'Location_52W_%': round(base_info['location_pct'], 0),
            'AvgVol_Lakh': round(avg_vol/100000, 1)
        })

        if (i + 1) % 30 == 0:
            time.sleep(0.3)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. SHEET UPDATE - BADE BASE FIRST ✅
try:
    ws_output = sh.worksheet("BaseSetups")
except:
    ws_output = sh.add_worksheet(title="BaseSetups", rows=1000, cols=20)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    # RANKING: 1. Base_Days sabse lamba 2. VCP wala 3. Score
    df_out = df_out.sort_values(['Base_Days', 'Setup_Type', 'Rank_Score'],
                                ascending=[False, True, False])
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {len(signals)} BASES FOUND | LONGEST FIRST ===")
else:
    ws_output.update('A1', [["Ref_Date", "Status"], [date_str, "No Bases Found"]])
    print("\n=== DONE: 0 SETUPS ===")
