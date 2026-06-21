import os
import datetime
import pandas as pd
import numpy as np
import requests
import gspread
from google.oauth2.service_account import Credentials

# ==========================================
# 1. SETTINGS & CONFIGURATIONS
# ==========================================
# Yeh links agar 404 bhi denge, toh bhi code crash nahi hoga
Ticker_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
Backup_Ticker_URL = "https://raw.githubusercontent.com/datasets/top-100-companies-india/master/data/nifty_100.csv"

MAX_HOLD_DAYS = 30
COOLDOWN_DAYS = 10
TARGET_PCT = 0.10
STOP_LOSS_PCT = 0.05

# Google Sheets Setup via Environment Variables
Creds_Dict = {
    "type": os.environ.get("GCP_TYPE"),
    "project_id": os.environ.get("GCP_PROJECT_ID"),
    "private_key_id": os.environ.get("GCP_PRIVATE_KEY_ID"),
    "private_key": os.environ.get("GCP_PRIVATE_KEY", "").replace("\\n", "\n"),
    "client_email": os.environ.get("GCP_CLIENT_EMAIL"),
    "client_id": os.environ.get("GCP_CLIENT_ID"),
    "auth_uri": os.environ.get("GCP_AUTH_URI"),
    "token_uri": os.environ.get("GCP_TOKEN_URI"),
    "auth_provider_x509_cert_url": os.environ.get("GCP_AUTH_PROVIDER_X509_CERT_URL"),
    "client_x509_cert_url": os.environ.get("GCP_CLIENT_X509_CERT_URL"),
    "universe_domain": os.environ.get("GCP_UNIVERSE_DOMAIN")
}

# ==========================================
# 2. YAHOO FINANCE STEALTH SCRAPER
# ==========================================
def fetch_yfinance_stealth(ticker, days=365):
    end_dt = datetime.datetime.now()
    start_dt = end_dt - datetime.timedelta(days=days)
    
    period1 = int(start_dt.timestamp())
    period2 = int(end_dt.timestamp())
    
    url = f"https://query1.finance.yahoo.com/v7/finance/download/{ticker}?period1={period1}&period2={period2}&interval=1d&events=history&includeAdjustedClose=true"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    try:
        res = requests.get(url, headers=headers, timeout=15)
        if res.status_code != 200:
            return None
        
        lines = res.text.split('\n')
        if len(lines) <= 1:
            return None
            
        data = []
        for line in lines[1:]:
            row = line.strip().split(',')
            if len(row) == 7 and 'null' not in row:
                data.append(row)
                
        if not data:
            return None
            
        df = pd.DataFrame(data, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Date'])
        for col in ['Open', 'High', 'Low', 'Close', 'Adj Close', 'Volume']:
            df[col] = pd.to_numeric(df[col])
            
        df = df.sort_values('Date').reset_index(drop=True)
        return df
    except Exception:
        return None

# ==========================================
# 3. PRICE ACTION LOGIC ENGINE
# ==========================================
def build_indicators(df):
    if len(df) < 21:
        return df
    df['Support_20D'] = df['Low'].shift(1).rolling(window=20).min()
    df['Resistance_10D'] = df['High'].shift(1).rolling(window=10).max()
    df['Vol_20MA'] = df['Volume'].shift(1).rolling(window=20).mean()
    df['Vol_Multiple'] = df['Volume'] / df['Vol_20MA']
    return df

def check_pure_price_action(df, idx):
    if idx < 1:
        return False, None
        
    row = df.iloc[idx]
    row_prev = df.iloc[idx-1]
    is_green = row['Close'] > row['Open']
    
    # STRATEGY 1: PA_SUPPORT_RETEST
    low_near_support = ((row['Low'] / row['Support_20D']) - 1) * 100 <= 1.2
    body = abs(row['Close'] - row['Open'])
    lower_wick = min(row['Open'], row['Close']) - row['Low']
    strong_rejection = lower_wick >= (body * 1.2)
    
    if low_near_support and (strong_rejection or is_green):
        return True, "PA_SUPPORT_RETEST"
        
    # STRATEGY 2: PA_CHoCH_BREAKOUT
    broke_resistance = row['Close'] > row['Resistance_10D'] and row_prev['Close'] <= row_prev['Resistance_10D']
    strong_volume = row.get('Vol_Multiple', 1.5) > 1.25
    
    if broke_resistance and strong_volume and is_green:
        return True, "PA_CHoCH_BREAKOUT"
        
    return False, None

# ==========================================
# 4. BACKTESTING BACKBONE
# ==========================================
def run_backtest_for_ticker(ticker, df):
    if df is None or len(df) < 30:
        return []
        
    df = build_indicators(df)
    signals = []
    idx = 21
    total_rows = len(df)
    
    while idx < total_rows:
        is_signal, mode = check_pure_price_action(df, idx)
        if is_signal:
            entry_row = df.iloc[idx]
            entry_price = entry_row['Close']
            entry_date = entry_row['Date']
            
            target_price = entry_price * (1 + TARGET_PCT)
            sl_price = entry_price * (1 - STOP_LOSS_PCT)
            
            result = "TIMEOUT"
            exit_date = None
            exit_price = entry_price
            
            exit_idx = idx + 1
            while exit_idx < min(idx + 1 + MAX_HOLD_DAYS, total_rows):
                future_row = df.iloc[exit_idx]
                
                if future_row['High'] >= target_price:
                    result = "PROFIT"
                    exit_date = future_row['Date']
                    exit_price = target_price
                    break
                elif future_row['Low'] <= sl_price:
                    result = "LOSS"
                    exit_date = future_row['Date']
                    exit_price = sl_price
                    break
                    
                exit_price = future_row['Close']
                exit_date = future_row['Date']
                exit_idx += 1
                
            signals.append({
                "Ticker": ticker,
                "Strategy Mode": mode,
                "Entry Date": entry_date.strftime('%Y-%m-%d'),
                "Entry Price": round(entry_price, 2),
                "Exit Date": exit_date.strftime('%Y-%m-%d') if exit_date else "N/A",
                "Exit Price": round(exit_price, 2),
                "Result": result
            })
            idx = exit_idx + COOLDOWN_DAYS
        else:
            idx += 1
            
    return signals

# ==========================================
# 5. MAIN EXECUTION ROUTINE
# ==========================================
def main():
    print("=== PURE PRICE ACTION RAW BACKTEST ENGINE V16 ===")
    
    ticker_list = []
    
    # Ultra Fail-safe Ticker Loading
    try:
        print("Attempting to download ticker list from primary source...")
        tickers_df = pd.read_csv(Ticker_URL)
        col_name = 'SYMBOL' if 'SYMBOL' in tickers_df.columns else 'Symbol'
        ticker_list = tickers_df[col_name].dropna().tolist()
    except Exception as e:
        print(f"Primary URL failed: {e}. Trying backup URL...")
        try:
            tickers_df = pd.read_csv(Backup_Ticker_URL)
            col_name = 'Symbol' if 'Symbol' in tickers_df.columns else tickers_df.columns[0]
            ticker_list = tickers_df[col_name].dropna().tolist()
        except Exception as err:
            print(f"Online lists failed. Activating Hardcoded Safe Fallback Tickers...")
            # Agar dono URL fail ho jayein, toh script is internal list par chalegi (Report aayega hi aayega!)
            ticker_list = [
                "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", 
                "TATAMOTORS", "SBIN", "BHARTIARTL", "ITC", "LT", 
                "RELIANCE", "AXISBANK", "BAJFINANCE", "MARUTI", "WIPRO"
            ]
            
    # Format tickers strictly for Yahoo Finance (.NS extension)
    ticker_list = [str(t).strip() + ".NS" for t in ticker_list if not str(t).strip().endswith(".NS")]
    
    print(f"Processing {len(ticker_list)} stocks...")
    
    all_signals = []
    batch_size = 20
    
    for i in range(0, len(ticker_list), batch_size):
        batch = ticker_list[i:i+batch_size]
        for ticker in batch:
            df = fetch_yfinance_stealth(ticker, days=365)
            if df is not None:
                ticker_signals = run_backtest_for_ticker(ticker, df)
                all_signals.extend(ticker_signals)
        print(f"Batch {int(i/batch_size)+1} done. Total Live Downloads: {min(i+batch_size, len(ticker_list))}")
        
    if not all_signals:
        print("⚠️ Alert: No price action signals discovered across entire market dataset.")
        return
        
    final_df = pd.DataFrame(all_signals)
    
    # Print Summary Report in Console
    print("\n=======================================================")
    print("📢 BACKTEST PERFORMANCE REPORT 📢")
    print("=======================================================")
    print(f"{'Strategy Mode':<20} | {'Total':<5} | {'Wins':<5} | {'Losses':<6} | {'Timeouts':<8} | {'Win Rate %'}")
    print("-" * 65)
    
    for m in ["PA_SUPPORT_RETEST", "PA_CHoCH_BREAKOUT"]:
        m_df = final_df[final_df["Strategy Mode"] == m]
        tot = len(m_df)
        if tot > 0:
            w = len(m_df[m_df["Result"] == "PROFIT"])
            l = len(m_df[m_df["Result"] == "LOSS"])
            t = len(m_df[m_df["Result"] == "TIMEOUT"])
            wr = round((w / tot) * 100, 2)
            print(f"{m:<20} | {tot:<5} | {w:<5} | {l:<6} | {t:<8} | {wr}%")
        else:
            print(f"{m:<20} | 0     | 0     | 0      | 0        | 0.0%")
    print("=======================================================\n")
    
    # Google Sheets Upload
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_info(Creds_Dict, scopes=scopes)
        client = gspread.authorize(creds)
        
        sheet = client.open("CTD_Sniper_Scanner")
        wks = sheet.get_worksheet(0)
        
        if final_df is not None and len(final_df) > 0:
            final_df = final_df.fillna("") 
            wks.clear() 
            sheet_data = [final_df.columns.values.tolist()] + final_df.values.tolist()
            wks.update(sheet_data) 
            print("Google Sheets Update Successful!")
    except Exception as e:
        print(f"Google Sheets Upload Failed: {e}")
        
    print("=== SYSTEM EXECUTION COMPLETE ===")

if __name__ == "__main__":
    main()
    
