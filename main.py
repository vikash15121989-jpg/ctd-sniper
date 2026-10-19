from datetime import datetime, timezone, timedelta
import json, os, math, gspread
import pandas as pd
import yfinance as yf

IST = timezone(timedelta(hours=5, minutes=30))
now = datetime.now(IST)
print(f"=== [SOLID ISLAND BACKTEST] | {now.strftime('%d-%b-%Y %H:%M IST')} ===", flush=True)

def sanitize_rows(rows):
    out=[]
    for r in rows:
        nr=[]
        for v in r:
            if isinstance(v,float) and (math.isnan(v) or math.isinf(v)): nr.append(0.0)
            else: nr.append(v)
        out.append(nr)
    return out

# Sheets Connect
gcp_json_creds = json.loads(os.environ["GSHEET_KEY"])
gc = gspread.service_account_from_dict(gcp_json_creds)
sh = gc.open("CTD_Sniper")

def get_or_create_ws(title):
    try: return sh.worksheet(title)
    except: return sh.add_worksheet(title=title, rows="5000", cols="15")

# Watchlist
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = [s.strip().upper()+".NS" if not s.strip().upper().endswith(".NS") else s.strip().upper() for s in raw if s and s.upper() not in ["STOCK","SYMBOL","NAME"]]
STOCKS = list(set(STOCKS))
print(f"Scanning {len(STOCKS)} stocks...", flush=True)

all_trades=[]

for symbol in STOCKS:
    try:
        stock_clean=symbol.replace(".NS","")
        data = yf.Ticker(symbol).history(period="5y", interval="1d", auto_adjust=True)
        if data.empty or len(data)<300: continue
        if isinstance(data.columns, pd.MultiIndex): data.columns=data.columns.get_level_values(0)
        
        monthly = data.resample('ME').agg({'Open':'first','High':'max','Low':'min','Close':'last','Volume':'sum'}).dropna()
        if len(monthly)<20: continue

        for i in range(6, len(monthly)-2): # 2 months forward check ke liye chhoda
            prev = monthly.iloc[i-1]
            curr = monthly.iloc[i]
            
            # 1. 6 Month High se 20% Down Check
            high_6m = monthly['High'].iloc[i-6:i].max()
            if prev['Close'] >= high_6m*0.80: continue # 20% gira hi nahi

            # 2. Prev Month Dead Candle (small body + lowest volume)
            body_pct = abs(prev['Close']-prev['Open'])/prev['Open']*100
            vol_4 = monthly['Volume'].iloc[i-4:i]
            if not (body_pct < 3.5 and prev['Volume'] <= vol_4.min()*1.1): continue

            # 3. Current Month Volume Blast
            max_vol_4 = vol_4.max()
            if not (curr['Close'] > prev['High'] and curr['Volume'] >= 2.0*max_vol_4): continue

            # Solid Setup Found
            entry = curr['High']
            sl = curr['Low']
            risk = (entry-sl)/entry*100
            if risk<2 or risk>10: continue # SL 2% se kam ya 10% se zyada to bekar

            # Forward 60 Days Result Check (Daily pe)
            # Entry ke baad ke 60 trading days me kya hua
            future_start = monthly.index[i]
            daily_future = data.loc[future_start:].iloc[1:61] # next 60 days
            if daily_future.empty: continue

            hit_10 = False; hit_15=False; hit_sl=False
            first_10=None; first_15=None; first_sl=None

            for idx_f, (d, row) in enumerate(daily_future.iterrows()):
                if row['Low'] <= sl and first_sl is None: first_sl=idx_f
                if row['High'] >= entry*1.10 and first_10 is None: first_10=idx_f
                if row['High'] >= entry*1.15 and first_15 is None: first_15=idx_f
            
            if first_10 is not None and (first_sl is None or first_10 < first_sl): hit_10=True
            if first_15 is not None and (first_sl is None or first_15 < first_sl): hit_15=True
            if first_sl is not None and (first_10 is None or first_sl < first_10): hit_sl=True

            all_trades.append({
                'Stock':stock_clean,
                'Setup_Month':monthly.index[i].strftime('%Y-%m'),
                'Entry':round(entry,2),
                'SL':round(sl,2),
                'Risk%':round(risk,2),
                'Hit_10%':hit_10,
                'Hit_15%':hit_15,
                'SL_First':hit_sl
            })
            print(f"✅ {stock_clean} {monthly.index[i].strftime('%Y-%m')} Risk {risk:.1f}%", flush=True)

    except: continue

# Export
df = pd.DataFrame(all_trades)
ws = get_or_create_ws("Backtest_Island_Solid")
ws.clear()

if not df.empty:
    total=len(df)
    w10=df['Hit_10%'].sum()
    w15=df['Hit_15%'].sum()
    sl=df['SL_First'].sum()
    print(f"\nTotal Trades: {total} | 10% Win: {w10/total*100:.1f}% | 15% Win: {w15/total*100:.1f}% | SL First: {sl/total*100:.1f}%")
    
    ws.append_row([f"Total: {total}", f"10% Win: {w10/total*100:.1f}%", f"15% Win: {w15/total*100:.1f}%", f"SL First: {sl/total*100:.1f}%"])
    ws.append_row([])
    ws.append_row(["Stock","Setup_Month","Entry","SL","Risk%","Hit_10%","Hit_15%","SL_First"])
    ws.append_rows(sanitize_rows(df.values.tolist()))
else:
    ws.append_row(["NO SOLID TRADES FOUND - Filter bahut tight hai"])
    print("No trades - Yehi to solid ka matlab hai, kam trade")

print("✅ Done")
