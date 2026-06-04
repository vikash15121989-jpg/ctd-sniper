import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

print("=== WYCKOFF SPRING FINDER V8.3: LOCATION AGNOSTIC + NAN FIXED ===")

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
print(f"Reference Date: {date_str} | TOP/MID/BOTTOM SAB BASE ALLOWED")

# 3. NIFTY DATA FOR RS RATING
nifty_df = yf.download("^NSEI", period="1y", progress=False, auto_adjust=True)
if isinstance(nifty_df.columns, pd.MultiIndex):
    nifty_df.columns = nifty_df.columns.get_level_values(0)
nifty_close = nifty_df['Close']

# 4. WYCKOFF VSA - LOCATION FILTER HATA DIYA ✅
def analyze_base_context(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 100:
        return None

    # BUYING CLIMAX REJECT - SIRF DISTRIBUTION FILTER ✅
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

    df_recent = df_till_date.tail(150).copy()
    base_windows = [15, 22, 35, 50]
    best_base = None

    for window in base_windows:
        if len(df_recent) < window + 20: continue

        total_len = len(df_recent)
        for i in range(max(0, total_len - 80), total_len - window):
            base_window = df_recent.iloc[i:i+window]
            base_high = base_window['High'].max()
            base_low = base_window['Low'].min()

            range_pct = (base_high - base_low) / base_low * 100
            max_range_allowed = 8 if window <= 22 else 12
            if range_pct > max_range_allowed: continue

            # VSA 1: VOLATILITY TIGHT
            daily_range = (base_window['High'] - base_window['Low']) / base_window['Close'] * 100
            if daily_range.mean() > 3.0: continue

            # VSA 2: VOLUME DRY UP
            mid = window // 2
            vol_first = base_window['Volume'].iloc[:mid].mean()
            vol_last = base_window['Volume'].iloc[mid:].mean()
            if vol_last > vol_first * 0.75: continue

            # RALLY PCT - SIRF CLASSIFICATION KE LIYE ✅
            lookback_60 = df_recent.iloc[max(0, i-60):i]
            if lookback_60.empty: continue
            swing_low_60 = lookback_60['Low'].min()
            rally_pct = (base_low - swing_low_60) / swing_low_60 * 100

            # 52W DATA - SIRF INFO KE LIYE ✅
            yearly_high = df_till_date['High'].tail(252).max()
            yearly_low = df_till_date['Low'].tail(252).min()
            distance_from_high = (yearly_high - base_high) / yearly_high * 100
            distance_from_low = (base_low - yearly_low) / yearly_low * 100

            # VSA 3: UP/DOWN VOLUME RATIO
            up_vol = base_window[base_window['Close'] > base_window['Open']]['Volume'].sum()
            down_vol = base_window[base_window['Close'] < base_window['Open']]['Volume'].sum()
            vol_ratio = up_vol / down_vol if down_vol > 0 else 99
            if vol_ratio < 1.1: continue

            # VSA 4: SPRING
            df_after_base = df_recent.iloc[i+window:]
            if len(df_after_base) < 3: continue
            df_30d_after = df_after_base.head(30)
            spring_low = df_30d_after['Low'].min()
            spring_exists = spring_low <= base_low * 1.02 and spring_low >= base_low * 0.93
            if not spring_exists: continue

            spring_idx = df_30d_after['Low'].idxmin()
            after_spring = df_30d_after[df_30d_after.index > spring_idx]
            if len(after_spring) >= 1:
                if after_spring['Close'].iloc[-1] < base_low * 0.97:
                    continue

            # CLASSIFICATION - RALLY % SE ✅
            if rally_pct < 25 and vol_ratio > 1.2:
                base_type = 'Accumulation'
            elif rally_pct >= 25 and rally_pct < 200 and vol_ratio > 1.3:
                base_type = 'Re-Accumulation'
            else:
                continue

            # CREEK CHECK
            creek_high = df_after_base['High'].max() if not df_after_base.empty else base_high
            last_close = df_after_base.iloc[-1]['Close']

            if len(df_after_base) >= 14:
                atr = (df_after_base['High'] - df_after_base['Low']).rolling(14).mean().iloc[-1]
            else:
                atr = (df_after_base['High'] - df_after_base['Low']).mean()

            if pd.isna(atr) or atr == 0:
                atr = (creek_high - base_low) * 0.05

            fail_level = min(creek_high * 0.93, creek_high - 2*atr)

            if df_after_base['Close'].min() < base_low * 0.98: continue
            if last_close < fail_level: continue
            if last_close > creek_high: continue

            current_score = (100 - range_pct) + vol_ratio * 10 - (total_len - i)
            if best_base is None or current_score > best_base['score']:
                best_base = {
                    'base_high': base_high, 'base_low': base_low, 'creek_high': creek_high,
                    'base_type': base_type, 'base_length': window, 'range_pct': range_pct,
                    'rally_pct': rally_pct, 'vol_ratio': vol_ratio, 'spring_low': spring_low,
                    'distance_from_52w_high_%': round(distance_from_high, 1),
                    'distance_from_52w_low_%': round(distance_from_low, 1),
                    'score': current_score
                }

    return best_base

# 5. MAIN LOOP - NAN FIX ✅
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]
signals = []
check_dates = [ref_date, ref_date - timedelta(days=14)]

for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        if len(df) < 100: continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()

        for check_date in check_dates:
            # FIX: check_date tak ka data lo ✅
            check_date_data = df[df.index <= check_date]
            if check_date_data.empty or len(check_date_data) < 50:
                continue
            candle_on_date = check_date_data.iloc[-1]

            avg_vol = candle_on_date['Vol_50']
            avg_turnover = avg_vol * candle_on_date['Close']
            if pd.isna(avg_vol) or (avg_vol < 2000000 or avg_turnover < 50000000):
                print(f" ❌ Liquidity low")
                continue

            # RS RATING
            stock_ret_63d = check_date_data['Close'].pct_change(63).iloc[-1] * 100
            nifty_ret_63d = nifty_close.pct_change(63).iloc[-1] * 100
            if pd.isna(stock_ret_63d) or pd.isna(nifty_ret_63d):
                rs_rating = 0
            else:
                rs_rating = stock_ret_63d - nifty_ret_63d

            if rs_rating < -5:
                print(f" ❌ RS weak: {rs_rating:.1f}%")
                continue

            base_info = analyze_base_context(df, check_date)
            if base_info is None:
                continue

            print(f" ✅ {base_info['base_type']} | {base_info['base_length']}D | 52W_H:{base_info['distance_from_52w_high_%']}% | RS:{rs_rating:.1f}%")

            breakout_level = base_info['creek_high'] * 1.005
            retest_level = base_info['creek_high'] * 0.995
            stop_loss = base_info['spring_low'] * 0.98
            risk_pct = round((candle_on_date['Close'] - stop_loss) / candle_on_date['Close'] * 100, 1)

            vol_ok = candle_on_date['Volume'] > avg_vol * 1.5
            dist_to_creek = round((base_info['creek_high'] - candle_on_date['Close'])/candle_on_date['Close']*100, 1)

            if dist_to_creek <= 1 and vol_ok:
                entry_status = "BUY NOW"
            elif dist_to_creek <= 3:
                entry_status = "ALERT LAGAO"
            else:
                entry_status = "WATCH"

            signals.append({
                'Stock': stock, 'Check_Date': check_date.strftime('%d/%m/%Y'),
                'Base_Type': base_info['base_type'], 'Base_Length': base_info['base_length'],
                'Base_Low': round(base_info['base_low'], 2), 'Base_High': round(base_info['base_high'], 2),
                'Creek_High': round(base_info['creek_high'], 2), 'CMP': round(candle_on_date['Close'], 2),
                'Distance_To_Creek_%': dist_to_creek, 'Entry_Breakout': round(breakout_level, 2),
                'Entry_Retest': round(retest_level, 2), 'Stop_Loss': round(stop_loss, 2),
                'Risk_%': risk_pct, 'Entry_Status': entry_status, 'RS_Rating': round(rs_rating, 1),
                'VSA_Confirm': 'Yes', 'Volume_OK': 'Yes' if vol_ok else 'No',
                'Base_Range_%': round(base_info['range_pct'], 1),
                'From_52W_High_%': base_info['distance_from_52w_high_%'],
                'From_52W_Low_%': base_info['distance_from_52w_low_%'],
                'Rally_Before_%': round(base_info['rally_pct'], 1),
                'Up/Down_Vol_Ratio': round(base_info['vol_ratio'], 2),
                'Avg_Vol_Lakh': round(avg_vol/100000, 1)
            })
            break

        if (i + 1) % 50 == 0:
            time.sleep(0.2)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. SHEET UPDATE
try:
    ws_output = sh.worksheet("SpringSetups")
except:
    ws_output = sh.add_worksheet(title="SpringSetups", rows=1000, cols=30)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals)
    status_priority = {'BUY NOW': 1, 'ALERT LAGAO': 2, 'WATCH': 3}
    df_out['Status_Priority'] = df_out['Entry_Status'].map(status_priority)
    df_out = df_out.sort_values(['Status_Priority', 'Distance_To_Creek_%', 'RS_Rating'], ascending=[True, True, False])
    df_out = df_out.drop(['Status_Priority'], axis=1)
    df_out = df_out.astype(str)
    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)
    print(f"\n=== DONE: {len(signals)} SETUPS | LOCATION AGNOSTIC ===")
else:
    ws_output.update('A1', [["Ref_Date", "Status"], [date_str, "No Clean Setups"]])
    print("\n=== DONE: 0 SETUPS ===")
