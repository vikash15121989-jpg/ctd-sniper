import os, json, gspread, yfinance as yf, pandas as pd
from datetime import datetime, timezone, timedelta
IST = timezone(timedelta(hours=5, minutes=30))

gc = gspread.service_account_from_dict(json.loads(os.environ["GSHEET_KEY"]))
sh = gc.open("CTD_Sniper")
raw = sh.worksheet("Watchlist").col_values(1)
STOCKS = list(set([s.strip().upper()+".NS" if not s.strip().upper().endswith(".NS") else s.strip().upper() for s in raw if s and s.upper() not in ["STOCK","SYMBOL","NAME","ETF"]]))

nifty = yf.Ticker("^NSEI").history(period="3y", auto_adjust=True)
nifty_ret_60 = nifty['Close'].pct_change(60)

all_trades=[]
for symbol in STOCKS:
  try:
    if "ETF" in symbol or "BEES" in symbol or "GOLD" in symbol or "SILVER" in symbol: continue
    d = yf.Ticker(symbol).history(period="2y", auto_adjust=True)
    if len(d)<250: continue
    if isinstance(d.columns, pd.MultiIndex): d.columns = d.columns.get_level_values(0)
    d['EMA20']=d['Close'].ewm(span=20).mean()
    d['EMA50']=d['Close'].ewm(span=50).mean()
    d['SMA200']=d['Close'].rolling(200).mean()
    d['Vol20']=d['Volume'].rolling(20).mean()
    d['Ret60']=d['Close'].pct_change(60)

    for i in range(200, len(d)-20):
        row = d.iloc[i]
        # Trend filter
        if not (row['Close']>row['SMA200'] and row['EMA20']>row['EMA50'] and row['EMA50']>row['SMA200']): continue
        # RS filter - Nifty se tez
        try: n_ret = nifty_ret_60.loc[:d.index[i]].iloc[-1]
        except: continue
        if row['Ret60'] < n_ret*1.5: continue
        
        # Pullback to 20 EMA with low volume
        if not (d['Low'].iloc[i-1]<=d['EMA20'].iloc[i-1] and row['Close']>row['EMA20'] and row['Volume']<row['Vol20']*0.9): continue
        
        entry=row['Close']; sl=row['EMA50']; risk=(entry-sl)/entry*100
        if risk<2 or risk>7: continue
        future=d.iloc[i+1:i+21] # 20 din me 5%?
        hit5=False; hitSL=False
        for _, r in future.iterrows():
            if r['Low']<=sl: hitSL=True; break
            if r['High']>=entry*1.05: hit5=True; break
        all_trades.append([symbol.replace(".NS",""), d.index[i].strftime('%Y-%m-%d'), round(entry,2), round(sl,2), round(risk,2), hit5, hitSL])
  except: continue

ws = sh.worksheet("Backtest_Solid_RS") if "Backtest_Solid_RS" in [x.title for x in sh.worksheets()] else sh.add_worksheet("Backtest_Solid_RS",2000,10)
ws.clear()
if all_trades:
    df=pd.DataFrame(all_trades, columns=["Stock","Date","Entry","SL","Risk%","Hit_5%","SL_First"])
    win=df['Hit_5%'].sum()/len(df)*100
    print(f"TOTAL {len(df)} | 5% WIN {win:.1f}%")
    ws.append_row([f"Total {len(df)} | 5% Win {win:.1f}%"])
    ws.append_row(["Stock","Date","Entry","SL","Risk%","Hit_5%","SL_First"])
    ws.append_rows(df.values.tolist())
else:
    ws.append_row(["No trades"])
