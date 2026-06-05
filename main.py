import yfinance as yf
import pandas as pd
import gspread
import json
import os
import time
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

print("=== VSA SPRING SNIPER V3.2: JSON SERIALIZABLE FIX ===")

# 1. GOOGLE SHEET CONNECT
gcp_json_creds = json.loads(os.environ['GSHEET_KEY'])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")
ws_watchlist = sh.worksheet("Watchlist")

# 2. A1 DATE
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
print(f"Reference Date: {date_str}")

# 3. BUYER/SELLER CALCULATOR
def calculate_buyer_seller(df):
    df = df.copy()
    df['Buyer_Vol'] = 0
    df['Seller_Vol'] = 0

    for i in range(len(df)):
        h, l, c, v = df['High'].iloc[i], df['Low'].iloc[i], df['Close'].iloc[i], df['Volume'].iloc[i]
        range_hl = h - l
        if range_hl == 0:
            buyer, seller = 0, 0
        else:
            buyer = (c - l) / range_hl * v
            seller = (h - c) / range_hl * v

        df.loc[df.index[i], 'Buyer_Vol'] = buyer
        df.loc[df.index[i], 'Seller_Vol'] = seller

    df['20DMA'] = df['Close'].rolling(20).mean()
    return df

# 4. VSA SPRING HUNTER - UPDATED QUALITY LOGIC
def find_vsa_spring(df, end_date):
    df_till_date = df[df.index <= end_date].copy()
    if len(df_till_date) < 50:
        return []

    df_till_date = calculate_buyer_seller(df_till_date)
    setups = []

    start_idx = max(21, len(df_till_date) - 365)

    for i in range(start_idx, len(df_till_date) - 4):
        curr = df_till_date.iloc[i]
        prev = df_till_date.iloc[i-1]

        prev_buyer = prev['Buyer_Vol']
        prev_seller = prev['Seller_Vol']
        if prev_buyer == 0: continue

        spring_ratio = prev_seller / prev_buyer
        spring_drop = (prev['Low'] - prev['Open']) / prev['Open']

        if not (spring_ratio >= 2 and spring_drop < -0.08):
            continue

        confirm = False
        confirm_idx = None
        for j in range(1, 4):
            if i + j >= len(df_till_date): break
            conf_candle = df_till_date.iloc[i + j]
            if conf_candle['Seller_Vol'] == 0: continue
            if conf_candle['Buyer_Vol'] / conf_candle['Seller_Vol'] > 3:
                confirm = True
                confirm_idx = i + j
                break

        if not confirm:
            continue

        if pd.isna(curr['20DMA']) or curr['Close'] <= curr['20DMA']:
            continue

        entry_price = float(curr['Close'])
        sl = float(prev['Low'] * 0.98)
        risk = entry_price - sl
        target = float(entry_price + (risk * 3))
        risk_pct = (risk / entry_price) * 100

        if risk_pct > 10 or risk_pct < 0.5:
            continue

        exit_price, exit_date, days, result = 0, None, 0, 'Running'
        for j in range(i + 1, len(df)):
            days += 1
            h, l, c = df['High'].iloc[j], df['Low'].iloc[j], df['Close'].iloc[j]

            if l <= sl:
                exit_price, exit_date, result = sl, df.index[j], 'SL Hit'
                break
            if h >= target:
                exit_price, exit_date, result = target, df.index[j], 'Target Hit'
                break
            if j == len(df) - 1:
                exit_price, exit_date, result = float(c), df.index[j], 'Running'

        if exit_date is None:
            continue

        pl_pct = ((exit_price - entry_price) / entry_price) * 100

        # QUALITY GRADE - TERA LOGIC
        if spring_ratio >= 5:
            quality = "A++"
        elif spring_ratio >= 4 and spring_ratio < 5:
            quality = "A+"
        elif spring_ratio >= 3 and spring_ratio < 4:
            quality = "A"
        elif spring_ratio >= 2 and spring_ratio < 3:
            quality = "B"
        else:
            quality = "C"

        setups.append({
            'entry_date': curr.name.strftime('%Y-%m-%d'),
            'spring_date': prev.name.strftime('%Y-%m-%d'),
            'confirm_date': df_till_date.iloc[confirm_idx].name.strftime('%Y-%m-%d'),
            'entry_price': round(entry_price, 2),
            'sl': round(sl, 2),
            'target': round(target, 2),
            'exit_date': exit_date.strftime('%Y-%m-%d'),
            'exit_price': round(exit_price, 2),
            'days': int(days),
            'pl_pct': round(pl_pct, 2),
            'result': result,
            'risk_pct': round(risk_pct, 1),
            'spring_ratio': round(spring_ratio, 1),
            'spring_drop': round(spring_drop * 100, 1),
            'quality': quality
        })

    return setups

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
        if len(df) < 150:
            print("Data kam hai")
            continue

        df['Vol_50'] = df['Volume'].rolling(50).mean()
        last_candle = df.iloc[-1]
        avg_vol = last_candle['Vol_50']
        avg_turnover = avg_vol * last_candle['Close']

        if pd.isna(avg_vol) or avg_vol < 1000000 or avg_turnover < 30000000:
            print("Volume/Turnover kam")
            continue

        trades = find_vsa_spring(df, ref_date)
        if len(trades) == 0:
            print("No VSA Spring")
            continue

        for trade in trades:
            rr = (trade['target'] - trade['entry_price']) / (trade['entry_price'] - trade['sl'])

            print(f" ✅ {trade['quality']} | {trade['entry_date']} | {trade['result']} | P&L: {trade['pl_pct']}% | Ratio: {trade['spring_ratio']}x")

            signals.append({
                'Stock': stock,
                'Quality': trade['quality'],
                'Entry_Date': trade['entry_date'],
                'Spring_Date': trade['spring_date'],
                'Confirm_Date': trade['confirm_date'],
                'Entry_Price': trade['entry_price'],
                'SL': trade['sl'],
                'Target': trade['target'],
                'Exit_Date': trade['exit_date'],
                'Exit_Price': trade['exit_price'],
                'Days': trade['days'],
                'P&L_%': trade['pl_pct'],
                'Result': trade['result'],
                'Risk_%': trade['risk_pct'],
                'R:R': round(rr, 1),
                'Spring_Ratio': trade['spring_ratio'],
                'Spring_Drop_%': trade['spring_drop'],
                'AvgVol_Lakh': round(avg_vol/100000, 1)
            })

        if (i + 1) % 20 == 0:
            time.sleep(0.5)

    except Exception as e:
        print(f"Error: {stock}: {e}")

# 6. SHEET UPDATE - JSON FIX ✅
try:
    ws_output = sh.worksheet("VSA_Spring_Setups")
except:
    ws_output = sh.add_worksheet(title="VSA_Spring_Setups", rows=1000, cols=18)

ws_output.clear()
if signals and len(signals) > 0:
    df_out = pd.DataFrame(signals)
    df_out = df_out.sort_values('P&L_%', ascending=False)

    # FIX: Convert all to native python types for gspread
    df_out = df_out.astype(object).where(pd.notnull(df_out), None)

    payload = [df_out.columns.values.tolist()] + df_out.values.tolist()
    ws_output.update('A1', payload)

    total_trades = len(df_out)
    wins = len(df_out[df_out['Result'] == 'Target Hit'])
    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0
    total_pl = df_out['P&L_%'].sum()
    avg_pl = round(total_pl / total_trades, 2) if total_trades > 0 else 0

    quality_counts = df_out['Quality'].value_counts()

    summary = [
        ['', ''],
        ['TOTAL TRADES', total_trades],
        ['WINS', wins],
        ['WIN RATE %', win_rate],
        ['TOTAL P&L %', round(total_pl, 2)],
        ['AVG P&L %', avg_pl],
        ['AVG R:R', '1:3'],
        ['', ''],
        ['A++ Count', quality_counts.get('A++', 0)],
        ['A+ Count', quality_counts.get('A+', 0)],
        ['A Count', quality_counts.get('A', 0)],
        ['B Count', quality_counts.get('B', 0)]
    ]
    ws_output.update(f'A{len(payload)+2}', summary)

    print(f"\n=== DONE: {len(signals)} VSA SPRINGS | WIN RATE: {win_rate}% | TOTAL P&L: {total_pl:.1f}% ===")
    print(f"A++: {quality_counts.get('A++', 0)} | A+: {quality_counts.get('A+', 0)} | A: {quality_counts.get('A', 0)} | B: {quality_counts.get('B', 0)}")
else:
    ws_output.update('A1', [["Ref_Date", "Status", "Reason"], [date_str, "No VSA Springs Found", "2x Seller + 8% drop + 3x Confirm nahi mila"]])
    print("\n=== DONE: 0 SETUPS - Filter strict hai ===")
