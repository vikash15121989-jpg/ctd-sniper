import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER V2: BASE + VSA + CLIMAX FILTER ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE - MULTIPLE FORMAT HANDLE
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
    raise ValueError(f"A1 me date format samajh nahi aaya: {date_raw}. Use YYYY-MM-DD ya DD/MM/YYYY")

date_str = ref_date.strftime('%Y-%m-%d')
print(f"Reference Date: {date_str} | + 10 Days Back Check")

# 3. VSA + BASE DETECTION FUNCTION - FIXED
def analyze_base_context(df, end_date, window=22, max_range_pct=15):
    """
    end_date tak ka data leke pichle 22 din me base dhoondo
    Ab Buying Climax + 52W High filter ke saath
    """
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 100:
        return None

    # FIX 1: BUYING CLIMAX FILTER - Top pe base reject
    recent_30 = df_till_date.tail(30)
    if len(recent_30) >= 20:
        vol_avg_30 = recent_30['Volume'].mean()
        high_20d = recent_30['High'].rolling(20).max()
        climax_check = recent_30[
            (recent_30['Volume'] > vol_avg_30 * 2.5) &
            (recent_30['High'] >= high_20d * 0.99) &
            (recent_30['Close'] < recent_30['High'] * 0.97)
        ]
        if not climax_check.empty:
            return None

    df_recent = df_till_date.tail(window + 60).copy()

    for i in range(len(df_recent) - 20):
        base_window = df_recent.iloc[i:i+20]
        base_high = base_window['High'].max()
        base_low = base_window['Low'].min()
        base_start_date = base_window.index[0]
        base_end_date = base_window.index[-1]

        range_pct = (base_high - base_low) / base_low * 100
        if range_pct > max_range_pct: continue

        # VSA CHECK 1: VOLATILITY SOOKHI KYA
        daily_range = (base_window['High'] - base_window['Low']) / base_window['Close'] * 100
        if daily_range.mean() > 3.5: continue

        # VSA CHECK 2: VOLUME DRY UP
        if len(base_window) >= 20:
            vol_first = base_window['Volume'].iloc[:10].mean()
            vol_second = base_window['Volume'].iloc[10:].mean()
            if vol_second > vol_first * 0.8: continue

        # FIX 2: SAHI RALLY PCT - 60 din ka swing low se
        lookback_60 = df_recent.iloc[max(0, i-60):i]
        if lookback_60.empty: continue
        swing_low_60 = lookback_60['Low'].min()
        rally_pct = (base_low - swing_low_60) / swing_low_60 * 100

        # FIX 3: 52-WEEK HIGH FILTER
        yearly_high = df_till_date['High'].tail(252).max()
        distance_from_high = (yearly_high - base_high) / yearly_high * 100

        up_vol = base_window[base_window['Close'] > base_window['Open']]['Volume'].sum()
        down_vol = base_window[base_window['Close'] < base_window['Open']]['Volume'].sum()
        vol_ratio = up_vol / down_vol if down_vol > 0 else 99

        df_after_base_start = df_recent.iloc[i:]
        spring_low = df_after_base_start['Low'].min()
        spring_exists = spring_low <= base_low * 1.02 and spring_low >= base_low * 0.95

        # NEW CLASSIFICATION LOGIC - TIGHT
        if distance_from_high < 12: # 52-wk high se 12% ke andar = Distribution
            base_type = 'Distribution Risk'
        elif rally_pct < 25 and spring_exists and vol_ratio > 1.1:
            base_type = 'Accumulation'
        elif rally_pct > 70 and vol_ratio > 1.3 and spring_exists:
            base_type = 'Re-Accumulation'
        else:
            base_type = 'Weak Base'

        # BREAKDOWN/BREAKOUT CHECK
        df_after_base = df_recent.iloc[i+20:]
        if df_after_base.empty:
            status = f'Forming: {base_type}'
            breakout_date = None
        elif df_after_base['Close'].min() < base_low * 0.98:
            return None
        elif df_after_base['Close'].max() > base_high:
            status = f'Breakout: {base_type}'
            breakout_date = df_after_base[df_after_base['Close'] > base_high].index[0]
        else:
            status = f'Forming: {base_type}'
            breakout_date = None

        return {
            'base_high': base_high,
            'base_low': base_low,
            'base_start_date': base_start_date,
            'base_end_date': base_end_date,
            'base_type': base_type,
            'status': status,
            'range_pct': range_pct,
            'rally_pct': rally_pct,
            'vol_ratio': vol_ratio,
            'spring_exists': spring_exists,
            'breakout_date': breakout_date,
            'distance_from_52w_high_%': round(distance_from_high, 1)
        }
    return None

# 4. STOCK LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
check_dates = [ref_date, ref_date - timedelta(days=14)]

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 100:
            print(f" ❌ Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        actual_last_date = df.index[-1]

        # 5. LIQUIDITY CHECK
        avg_vol = last_candle['Vol_50']
        avg_turnover = avg_vol * last_candle['Close']
        if pd.isna(avg_vol) or (avg_vol < 5000000 and avg_turnover < 50000000):
            print(f" ❌ Liquidity low")
            continue

        # 6. DONO DATES PE CHECK KARO
        for check_date in check_dates:
            check_date_str = check_date.strftime('%Y-%m-%d')
            print(f" Checking till: {check_date_str}")

            base_info = analyze_base_context(df, check_date)

            if base_info is None:
                continue

            if 'Distribution' in base_info['base_type'] or 'Weak' in base_info['base_type']:
                print(f" ❌ {base_info['status']}")
                continue

            print(f" ✅ {base_info['status']} | Range: {base_info['range_pct']:.1f}% | 52W_High_Dist: {base_info['distance_from_52w_high_%']}%")

            # 7. SPRING DHOONDO - OPTIONAL CONTEXT
            df_check = df[df.index <= check_date].iloc[:-2]
            spring_low, spring_date = None, None
            if len(df_check) > 20:
                df_check_rev = df_check.iloc[::-1]
                for idx, row in df_check_rev.iterrows():
                    current_low = row['Low']
                    after_idx = df.index.get_loc(idx) + 1
                    df_after = df.iloc[after_idx:df.index.get_loc(check_date)+1]
                    if df_after.empty: continue
                    if df_after['Close'].min() > current_low:
                        spring_low = current_low
                        spring_date = idx
                        break

            signals.append({
                'Stock': stock,
                'Check_Date': check_date.strftime('%d/%m/%Y'),
                'Data_Till': actual_last_date.strftime('%d/%m/%Y'),
                'Base_Start': base_info['base_start_date'].strftime('%d/%m/%Y'),
                'Base_End': base_info['base_end_date'].strftime('%d/%m/%Y'),
                'Base_Status': base_info['status'],
                'Base_Type': base_info['base_type'],
                'Base_Low': round(base_info['base_low'], 2),
                'Base_Top': round(base_info['base_high'], 2),
                'Base_Range_%': round(base_info['range_pct'], 1),
                'From_52W_High_%': base_info['distance_from_52w_high_%'],
                'Rally_Before_%': round(base_info['rally_pct'], 1),
                'Up/Down_Vol_Ratio': round(base_info['vol_ratio'], 2),
                'Spring_Present': 'Yes' if base_info['spring_exists'] else 'No',
                'Spring_Date': spring_date.strftime('%d/%m/%Y') if spring_date else 'None',
                'Spring_Low': round(spring_low, 2) if spring_low else 0,
                'Breakout_Date': base_info['breakout_date'].strftime('%d/%m/%Y') if base_info['breakout_date'] else 'Not Yet',
                'CMP': round(last_candle['Close'], 2),
                'Distance_To_Top_%': round((base_info['base_high'] - last_candle['Close'])/last_candle['Close']*100, 1),
                'Avg_Vol_Lakh': round(avg_vol/100000, 1)
            })
            break

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 8. SHEET UPDATE
try:
    ws_output = sh.worksheet("SpringSetups")
except:
    ws_output = sh.add_worksheet(title="SpringSetups", rows=1000, cols=25)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    df_out['Priority'] = df_out['Base_Type'].apply(lambda x: 1 if 'Re-Accumulation' in x else 2 if 'Accumulation' in x else 3)
    df_out = df_out.sort_values(['Priority', 'Base_Start'])
    df_out = df_out.drop('Priority', axis=1)
    ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
    print(f"\n=== DONE: {len(signals)} SETUPS | Re-Accumulation Priority ===")
else:
    ws_output.update([["Ref_Date", "Status"], [date_str, "No Setups Found"]])
    print("\n=== DONE: 0 SETUPS ===")
