import os, json, gspread, yfinance as yf, pandas as pd
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))
print(f"=== SOLID RS + EMA FINAL | {datetime.now(IST).strftime('%d-%b %H:%M')} ===")

# --- GSheet Connect ---
gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)

# Kachra saaf - ETF, BEES, GOLD, delisted wale
STOCKS = []
for s in raw:
    if not s: continue
    s = s.strip().upper()
    if s in ["STOCK","SYMBOL","NAME"]: continue
    if any(x in s for x in ["ETF","BEES","GOLD","SILVER","NIFTY","SENSEX"]): continue
    if not s.endswith(".NS"): s = s + ".NS"
    STOCKS.append(s)
STOCKS = list(set(STOCKS))
print(f"Watchlist Clean: {len(STOCKS)}")

all_trades = []

for symbol in STOCKS:
    try:
        d = yf.Ticker(symbol).history(period="2y", interval="1d", auto_adjust=True)
        if len(d) < 250: continue
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)

        d['EMA20'] = d['Close'].ewm(span=20).mean()
        d['EMA50'] = d['Close'].ewm(span=50).mean()
        d['SMA200'] = d['Close'].rolling(200).mean()
        d['Vol20'] = d['Volume'].rolling(20).mean()
        d['Ret60'] = d['Close'].pct_change(60) * 100

        # Daily loop
        for i in range(200, len(d)-25):
            row = d.iloc[i]

            # 1. Bada Trend Filter - Yehi solid hai
            if not (row['Close'] > row['SMA200'] and row['EMA20'] > row['EMA50'] and row['EMA50'] > row['SMA200']):
                continue

            # 2. RS Filter - 60 din me 15% se 60% tak
            ret = row['Ret60']
            if pd.isna(ret) or ret < 15 or ret > 60:
                continue

            # 3. Pullback Filter - EMA20 ke 3% andar
            dist_ema = (row['Close'] - row['EMA20']) / row['EMA20'] * 100
            if dist_ema < -3 or dist_ema > 1:
                continue
            
            # 4. Volume - Bounce pe volume aana chahiye
            if row['Volume'] < row['Vol20'] * 1.2:
                continue

            # 5. Pichla din EMA ke neeche gaya ho
            prev_low = d['Low'].iloc[i-1]
            prev_ema = d['EMA20'].iloc[i-1]
            if prev_low > prev_ema: # pichle din touch hi nahi kiya
                continue

            entry = row['Close']
            sl = row['EMA50'] # SL 50 EMA
            risk = (entry - sl) / entry * 100

            if risk < 2.5 or risk > 7:
                continue

            # Future - 20 din me 5%
            future = d.iloc[i+1 : i+21]
            if future.empty: continue

            hit5 = False
            hitSL = False
            for _, r in future.iterrows():
                if r['Low'] <= sl:
                    hitSL = True
                    break
                if r['High'] >= entry * 1.05:
                    hit5 = True
                    break
            
            all_trades.append([
                symbol.replace(".NS",""),
                d.index[i].strftime('%Y-%m-%d'),
                round(entry,2),
                round(sl,2),
                round(risk,2),
                round(ret,1),
                hit5,
                hitSL
            ])

    except Exception as e:
        continue

# --- Sheet me daal ---
try:
    ws = sh.worksheet("Backtest_Solid_RS")
except:
    ws = sh.add_worksheet("Backtest_Solid_RS", 5000, 10)
ws.clear()

if all_trades:
    df = pd.DataFrame(all_trades, columns=["Stock","Date","Entry","SL","Risk%","Ret60%","Hit_5%","SL_First"])
    total = len(df)
    wins = df['Hit_5%'].sum()
    win_pct = wins/total*100 if total>0 else 0
    print(f"TOTAL {total} | 5% WIN {win_pct:.1f}%")
    
    ws.append_row([f"Total: {total}", f"5% Win: {win_pct:.1f}%", f"Run: {datetime.now(IST).strftime('%d-%b %H:%M')}"])
    ws.append_row(["Stock","Date","Entry","SL","Risk%","Ret60%","Hit_5%","SL_First"])
    ws.append_rows(df.values.tolist())
else:
    ws.append_row(["No trades found - watchlist check karo"])
    print("No trades")
