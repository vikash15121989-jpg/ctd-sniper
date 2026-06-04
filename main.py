import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== WYCKOFF V9.3 BACKTEST: PICHLE 1 SAAL ME KITNE SETUP BANE ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. BACKTEST DATES - PICHLE 1 SAAL
end_date = datetime.now()
start_date = end_date - timedelta(days=365)
backtest_dates = pd.date_range(start=start_date, end=end_date, freq='B') # B = Business days
print(f"Backtest Range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
print(f"Total Trading Days: {len(backtest_dates)}")

# 3. NIFTY DATA FOR RS RATING
nifty_df = yf.download("^NSEI", period="2y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. VCP DETECTOR FUNCTION
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

# 5. RANGE HUNTER V9.3 - SAME LOGIC
def analyze_base_context(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 100:
        return None

    # BUYING CLIMAX REJECT
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

    df_recent = df_till_date.tail(250).copy()

    # RULE-1: LAST 10 DIN ME RANGE BANI HAI?
    last_10 = df_recent.tail(10)
    recent_high = last_10['High'].max()
    recent_low = last_10['Low'].min()
    recent_range_pct = (recent_high - recent_low) / recent_low * 100

    if recent_range_pct > 20 or recent_range_pct < 5:
        return None

    # RULE-2: YE RANGE LOW KE PAAS BANI HAI?
    yearly_high = df_till_date['High'].tail(252).max()
    yearly_low = df_till_date['Low'].tail(252).min()
    location_pct = (recent_low - yearly_low) / (yearly_high - yearly_low) * 100 if yearly_high!= yearly_low else 50

    if location_pct > 40:
        return None

    # RULE-3: IS RANGE KO PICHE EXPAND KARO
    support_zone = recent_low * 0.98
    resistance_zone = recent_high * 1.02

    range_start_idx = len(df_recent) - 10

    for i in range(len(df_recent) - 11, -1, -1):
        candle = df_recent.iloc[i]
        if candle['Low'] < support_zone * 0.90 or candle['High'] > resistance_zone * 1.10:
            break
        range_start_idx = i

    full_range = df_recent.iloc[range_start_idx:]
    base_high = full_range['High'].max()
    base_low = full_range['Low'].min()
    base_length = len(full_range)

    if base_length < 20:
        return None

    range_pct = (base_high - base_low) / base_low * 100
    if range_pct > 25: return None

    # RULE-4: STRONG ACCUMULATION COMPULSORY
    daily_range = (full_range['High'] - full_range['Low']) / full_range['Close'] * 100
    if daily_range.mean() > 3.5: return None

    vol_first_3rd = full_range['Volume'].iloc[:base_length//3].mean()
    vol_last_3rd = full_range['Volume'].iloc[-base_length//3:].mean()
    vol_dry_ratio = vol_last_3rd / vol_first_3rd if vol_first_3rd > 0 else 1

    up_vol = full_range[full_range['Close'] > full_range['Open']]['Volume'].sum()
    down_vol = full_range[full_range['Close'] < full_range['Open']]['Volume'].sum()
    vol_ratio = up_vol / down_vol if down_vol > 0 else 99

    is_accumulation = vol_ratio >= 1.15 and vol_dry_ratio <= 0.80
    if not is_accumulation:
        return None

    vsa_type = 'Accumulation'

    # RULE-5: VCP CHECK
    vcp_status, vcp_strength, has_vcp = detect_vcp(full_range)

    # RULE-6: SPRING CHECK
    df_after_base = df_recent.iloc[range_start_idx + base_length:]
    last_close = df_recent.iloc[-1]['Close']
    creek_high = base_high

    if len(df_after_base) < 3:
        spring_low = base_low
        spring_type = "No Spring Yet"
        spring_strength = 0
    else:
        spring_low = df_after_base['Low'].min()
        creek_high = df_after_base['High'].max() if not df_after_base.empty else base_high

        if len(df_after_base) >= 5:
            post_range_high = df_after_base['High'].head(5).max()
            if last_close < post_range_high * 0.92:
                return None

        if spring_low >= base_low * 0.99:
            spring_type = "No Spring"
            spring_strength = 1
        elif spring_low < base_low * 0.99 and last_close >= base_low * 0.98:
            spring_type = "Spring Reclaim"
            spring_strength = 3
        elif spring_low < base_low * 0.99 and last_close < base_low * 0.98:
            return None
        else:
            spring_type = "Weak Spring"
            spring_strength = 2

        if spring_low < base_low * 0.85: return None
        if last_close > creek_high * 1.01: return None

    # RULE-7: RANKING SCORE
    dist_to_creek_pct = (creek_high - last_close) / last_close * 100
    tightness_score = 30 - range_pct
    demand_score = vol_ratio * 15
    dry_score = (1 - vol_dry_ratio) * 25
    spring_bonus = spring_strength * 10
    vcp_bonus = vcp_strength * 15
    proximity_score = max(0, 25 - dist_to_creek_pct * 5)
    length_bonus = base_length * 0.3
    location_bonus = max(0, 15 - location_pct * 0.3)

    breakout_score = (tightness_score + demand_score + dry_score +
                    spring_bonus + vcp_bonus + proximity_score + length_bonus +
                    location_bonus)

    return {
        'base_high': base_high, 'base_low': base_low, 'creek_high': creek_high,
        'base_length': base_length, 'range_pct': range_pct, 'vsa_type': vsa_type,
        'vcp_status': vcp_status, 'vol_ratio': vol_ratio, 'vol_dry_ratio': vol_dry_ratio,
        'spring_low': spring_low, 'spring_type': spring_type,
        'dist_to_creek_pct': dist_to_creek_pct, 'location_pct': location_pct,
        'breakout_score': breakout_score, 'has_vcp': has_vcp, 'date': end_date.strftime('%Y-%m-%d')
    }

# 6. BACKTEST LOOP - HAR STOCK HAR DIN ✅
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
all_signals = []

print(f"\nScanning {len(stocks)} stocks for {len(backtest_dates)} days...")
print("Ye 30-60 min chalega. Wait kar...")

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="2y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 200: continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        df['EMA200'] = df['Close'].ewm(span=200).mean()

        stock_signals = 0
        # Har 10 din me ek baar check karo - warna 1 saal me 250*2362 = 6 lakh loop ho jayega
        for test_date in backtest_dates[::10]: # Every 10th trading day
            if test_date not in df.index: continue

            row = df.loc[test_date]
            avg_vol = row['Vol_50']
            avg_turnover = avg_vol * row['Close']

            if pd.isna(avg_vol) or avg_vol < 1000000 or avg_turnover < 30000000:
                continue

            if row['Close'] < row['EMA200'] * 0.95:
                continue

            base_info = analyze_base_context(df, test_date)
            if base_info is None:
                continue

            setup_type = "ACCUM"
            if base_info['has_vcp']: setup_type += "+VCP"

            all_signals.append({
                'Date': base_info['date'],
                'Stock': stock,
                'Setup_Type': setup_type,
                'VCP_Status': base_info['vcp_status'],
                'Range_Days': base_info['base_length'],
                'Resistance_Creek': round(base_info['creek_high'], 2),
                'Support_Base': round(base_info['base_low'], 2),
                'Spring_Type': base_info['spring_type'],
                'Vol_Ratio': round(base_info['vol_ratio'], 2),
                'Vol_Dry_%': round((1-base_info['vol_dry_ratio'])*100, 0),
                'Location_52W_%': round(base_info['location_pct'], 0),
                'Rank_Score': round(base_info['breakout_score'], 0)
            })
            stock_signals += 1

        print(f" ✅ {stock}: {stock_signals} setups found in 1 year")
        time.sleep(0.2)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 7. SHEET UPDATE - BACKTEST RESULTS ✅
try:
    ws_output = sh.worksheet("Backtest_Results")
except:
    ws_output = sh.add_worksheet(title="Backtest_Results", rows=5000, cols=15)

ws_output.clear()
if all_signals:
    df_out = pd.DataFrame(all_signals)
    df_out = df_out.sort_values(['Date', 'Rank_Score'], ascending=[False, False])
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== BACKTEST DONE: {len(all_signals)} TOTAL SETUPS IN 1 YEAR ===")
    print(f"Sheet 'Backtest_Results' me dekh. Har date pe kaunse stock setup diye.")
else:
    ws_output.update('A1', [["Status"], ["No setups found in last 1 year"]])
    print("\n=== BACKTEST DONE: 0 SETUPS IN 1 YEAR ===")
    print("Agar ye aaye to filter bahut strict hai ya watchlist galat hai.")
