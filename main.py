import yfinance as yf
import pandas as pd
import gspread
import json
import os
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== SPRING FINDER: BASE FORMATION + CREEK + LIQUIDITY ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 SE DATE - BAS REFERENCE KE LIYE
date_str = str(ws_watchlist.acell('A1').value).split(' ')[0]
print(f"Reference Date: {date_str}")

# 3. BASE DETECTION FUNCTION - NAYA ✅
def is_base_forming(df_post_spring, min_days=20, max_range_pct=15):
    """Spring ke baad base ban raha ya nahi check karo"""
    if len(df_post_spring) < min_days:
        return False, 0, "Days kam"

    df_recent = df_post_spring.tail(40).copy() # Last 40 din ka base dekho

    # 1. RANGE CHECK - High Low ka gap 15% se kam
    base_high = df_recent['High'].max()
    base_low = df_recent['Low'].min()
    range_pct = (base_high - base_low) / base_low * 100
    if range_pct > max_range_pct:
        return False, base_high, f"Range bada {range_pct:.1f}%"

    # 2. VOLATILITY CHECK - Daily range sookh gaya
    df_recent['DailyRange'] = (df_recent['High'] - df_recent['Low']) / df_recent['Close'] * 100
    avg_range = df_recent['DailyRange'].mean()
    if avg_range > 3.5:
        return False, base_high, f"Volatile {avg_range:.1f}%"

    # 3. VOLUME DRY UP - Last 20 din, usse pehle ke 20 din se 40% kam
    if len(df_recent) >= 40:
        vol_first = df_recent['Volume'].iloc[:20].mean()
        vol_second = df_recent['Volume'].iloc[20:].mean()
        if vol_second > vol_first * 0.7: # 30% se jyada kam nahi hua
            return False, base_high, "Volume sookha nahi"

    # 4. SUPPORT HOLD - Neeche nahi ja raha
    if df_recent['Close'].iloc[-1] < base_low * 1.02: # Low ke 2% upar hi ho
        return False, base_high, "Support ke paas"

    return True, base_high, f"Base OK: {range_pct:.1f}% Range"

# 4. STOCK LIST
stocks = ws_watchlist.col_values(1)[1:]
stocks = [s.strip().upper() for s in stocks if s.strip()]

signals = []
for i, stock in enumerate(stocks):
    print(f"\n--- [{i+1}/{len(stocks)}] {stock} ---")
    try:
        df = yf.download(f"{stock}.NS", period="1y", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        if len(df) < 100:
            print(f" ❌ Data kam hai: {len(df)} rows")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        actual_last_date = df.index[-1].strftime('%d/%m/%Y')

        # 5. LIQUIDITY CHECK
        avg_vol = last_candle['Vol_50']
        avg_turnover = avg_vol * last_candle['Close']
        if pd.isna(avg_vol) or (avg_vol < 5000000 and avg_turnover < 50000000):
            print(f" ❌ Liquidity low - Vol:{avg_vol/100000:.1f}L, Turnover:{avg_turnover/10000000:.1f}Cr")
            continue

        # 6. SPRING DHOONDO - MINIMUM 2 DIN PURANA
        df_check = df.iloc[:-2]
        if len(df_check) < 20: continue
        df_check_rev = df_check.iloc[::-1]

        spring_low = None
        spring_date = None
        spring_candle = None

        for idx, row in df_check_rev.iterrows():
            current_low = row['Low']
            after_idx = df.index.get_loc(idx) + 1
            df_after = df.iloc[after_idx:]
            if df_after.empty: continue
            if df_after['Close'].min() > current_low:
                spring_low = current_low
                spring_date = idx
                spring_candle = row
                break

        if spring_low is None:
            print(f" ❌ Koi Unbroken Spring nahi mila")
            continue

        spring_idx = df.index.get_loc(spring_date)

        # 7. BASE CHECK - NAYA ✅ SPRING KE BAAD BASE BAN RAHA?
        df_post_spring = df.iloc[spring_idx:]
        is_base, base_top, base_reason = is_base_forming(df_post_spring)

        if not is_base:
            print(f" ❌ Base nahi: {base_reason}")
            continue
        print(f" ✅ Base Forming: {base_reason} | Top: {base_top:.2f}")

        # 8. CREEK = SWING HIGH YA BASE TOP
        df_before_spring = df.iloc[:spring_idx]
        if len(df_before_spring) < 20: continue

        df_recent = df_before_spring.tail(60).copy()
        swing_high_indices = []
        for j in range(3, len(df_recent)-3):
            current_close = df_recent['Close'].iloc[j]
            left_3_max = df_recent['Close'].iloc[j-3:j].max()
            right_3_max = df_recent['Close'].iloc[j+1:j+4].max()
            if current_close > left_3_max and current_close > right_3_max:
                swing_high_indices.append(df_recent.index[j])

        # Creek = Swing High nahi mila to Base Top use karo
        if len(swing_high_indices) == 0:
            creek_high = base_top # ← Base ka top hi Creek
            creek_date = df_post_spring['High'].idxmax().strftime('%d/%m/%Y')
            creek_type = 'Base Top'
        else:
            creek_date_idx = swing_high_indices[-1]
            creek_high = df_recent.loc[creek_date_idx, 'High']
            creek_date = creek_date_idx.strftime('%d/%m/%Y')
            creek_type = 'Nearest Swing High'

        # SANITY CHECK
        if creek_high > spring_low * 1.8:
            print(f" ❌ Creek {creek_high:.2f} Spring {spring_low:.2f} se 80%+ door")
            continue
        if creek_high > last_candle['Close'] * 2:
            print(f" ❌ Creek {creek_high:.2f} CMP se 100%+ door")
            continue

        # Creek break check Close se
        if last_candle['Close'] >= creek_high:
            print(f" ❌ Creek break ho gaya")
            continue

        # 9. KITNE DIN PURANA
        days_ago = len(df) - spring_idx - 1
        if days_ago > 120:
            print(f" ❌ Spring purana {days_ago} din")
            continue

        # 10. VOLUME CHECK
        spring_vol_high = spring_candle['Volume'] > spring_candle['Vol_50'] * 1.5 if pd.notna(spring_candle['Vol_50']) else False
        spring_strength = 'STRONG' if spring_vol_high else 'WEAK'

        signals.append({
            'Stock': stock,
            'Ref_Date': date_str,
            'Data_Till': actual_last_date,
            'Spring_Date': spring_date.strftime('%d/%m/%Y'),
            'Spring_Low': round(spring_low, 2),
            'Spring_Strength': spring_strength,
            'Trading_Days_Ago': days_ago,
            'Base_Status': base_reason, # ← NAYA
            'Base_Top': round(base_top, 2), # ← NAYA - Ye watch karna hai
            'Creek_High': round(creek_high, 2),
            'Creek_Date': creek_date,
            'Creek_Type': creek_type,
            'CMP': round(last_candle['Close'], 2),
            'Distance_To_Creek_%': round((creek_high - last_candle['Close'])/last_candle['Close']*100, 1),
            'Avg_Vol_Lakh': round(avg_vol/100000, 1),
            'Avg_Turnover_Cr': round(avg_turnover/10000000, 1)
        })
        print(f"[PASS] ✅ {base_reason} | Creek {creek_high:.2f}")

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 11. SHEET UPDATE
try:
    ws_output = sh.worksheet("SpringSetups")
except:
    ws_output = sh.add_worksheet(title="SpringSetups", rows=1000, cols=20)

ws_output.clear()
if signals:
    df_out = pd.DataFrame(signals).sort_values('Trading_Days_Ago')
    ws_output.update([df_out.columns.values.tolist()] + df_out.values.tolist())
    print(f"\n=== DONE: {len(signals)} BASE WALE SETUPS ===")
else:
    ws_output.update([["Ref_Date", "Status"], [date_str, "No Base Setups"]])
    print("\n=== DONE: 0 SETUPS ===")
