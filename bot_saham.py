import yfinance as yf
import pandas as pd
import numpy as np
import datetime
import holidays
import asyncio
import aiohttp
import os

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

def analisis_saham_pro(ticker_symbol):
    ticker = f"{ticker_symbol}.JK"
    data = yf.download(ticker, period="3mo", interval="1d", progress=False)
    
    if data.empty or len(data) < 21:
        return None 
        
    data['Typical_Price'] = (data['High'] + data['Low'] + data['Close']) / 3
    data['Volume_Price'] = data['Typical_Price'] * data['Volume']
    data['VWMA_20'] = data['Volume_Price'].rolling(window=20).sum() / data['Volume'].rolling(window=20).sum()
    
    data['SMA_20'] = data['Close'].rolling(window=20).mean()
    data['STD_20'] = data['Close'].rolling(window=20).std()
    data['Upper_BB'] = data['SMA_20'] + (data['STD_20'] * 2)
    data['Lower_BB'] = data['SMA_20'] - (data['STD_20'] * 2)
    data['BB_Width'] = (data['Upper_BB'] - data['Lower_BB']) / data['SMA_20']
    
    data['H-L'] = data['High'] - data['Low']
    data['H-PC'] = abs(data['High'] - data['Close'].shift(1))
    data['L-PC'] = abs(data['Low'] - data['Close'].shift(1))
    data['True_Range'] = data[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    data['ATR_14'] = data['True_Range'].rolling(window=14).mean()
    
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    data['RSI_14'] = 100 - (100 / (1 + rs))
    
    hari_ini = data.iloc[-1]
    kemarin = data.iloc[-2]
    
    rata_volume = data['Volume'].rolling(window=20).mean().iloc[-1]
    ledakan_volume = hari_ini['Volume'] > (rata_volume * 1.5)
    uptrend_bandar = hari_ini['Close'] > hari_ini['VWMA_20']
    sedang_squeeze = kemarin['BB_Width'] < 0.10
    nembus_atas = hari_ini['Close'] > hari_ini['SMA_20']
    ruang_naik = hari_ini['RSI_14'] < 70
    
    status = "HOLD / SKIP"
    pesan = ""
    
    if ledakan_volume and uptrend_bandar and ruang_naik:
        cut_loss = hari_ini['Close'] - (hari_ini['ATR_14'] * 1.5)
        if sedang_squeeze and nembus_atas:
            status = "🚀 SUPER BUY (SQUEEZE BREAKOUT)"
            pesan = f"**{ticker_symbol}** meledak dari area Squeeze dengan volume raksasa!"
        else:
            status = "✅ BUY (TREND BANDAR KUAT)"
            pesan = f"**{ticker_symbol}** diakumulasi. Harga kokoh di atas VWMA."
            
        pesan += f"\n🛡️ **Cut Loss:** Rp {cut_loss:,.0f} | 📊 **RSI:** {hari_ini['RSI_14']:.1f}"
    
    elif hari_ini['Close'] < hari_ini['VWMA_20']:
        status = "☠️ BAHAYA!!!"
        pesan = f"**{ticker_symbol}** di bawah garis bandar. Jangan dibeli!"
        
    return {"status": status, "pesan": pesan, "harga_terakhir": hari_ini['Close']}

async def kirim_discord(session, pesan):
    if not WEBHOOK_URL:
        print("Error: Discord Webhook belum diatur!")
        return
    
    payload = {"content": pesan}
    async with session.post(WEBHOOK_URL, json=payload) as response:
        if response.status in [200, 204]:
            print("Pesan terkirim ke Discord!")
        else:
            print(f"Gagal kirim. Status: {response.status}")

async def main():
    waktu_sekarang = datetime.datetime.now()
    tanggal_sekarang = waktu_sekarang.date()
    hari = waktu_sekarang.weekday()
    
    print(f"Bot jalan pada: {waktu_sekarang}")
    
    libur_indo = holidays.country_holidays('ID')
    if tanggal_sekarang in libur_indo or hari > 4:
        pesan_libur = f"☕ **INFO MARKET TUTUP** ☕\nTanggal {tanggal_sekarang} ini libur nasional atau weekend."
        
        if hari == 4:
            pesan_libur += "\n\n📁 **LAPORAN MINGGUAN:** Selesai direkap. Silakan cek performa minggu ini!"
            
        async with aiohttp.ClientSession() as session:
            await kirim_discord(session, pesan_libur)
            
        print("Bursa Tutup. Laporan libur dikirim ke Discord.")
        return

    try:
        with open("watchlist.txt", "r") as file:
            daftar_saham = [line.strip().upper() for line in file if line.strip()]
    except FileNotFoundError:
        print("File watchlist.txt tidak ditemukan.")
        daftar_saham = []

    pesan_discord = f"**LAPORAN SCANNING SAHAM ({tanggal_sekarang})**\n"
    ada_sinyal_beli = False

    for saham in daftar_saham:
        print(f"Menganalisis {saham}...")
        hasil = analisis_saham_pro(saham)
        
        if hasil and "BUY" in hasil['status']:
            pesan_discord += f"\n{hasil['status']}\n{hasil['pesan']}\n"
            ada_sinyal_beli = True

    async with aiohttp.ClientSession() as session:
        if ada_sinyal_beli:
            await kirim_discord(session, pesan_discord)
        else:
            print("Tidak ada sinyal BUY hari ini.")
            
        if hari == 4:
            pesan_jumat = "📁 **LAPORAN MINGGUAN:** Selesai direkap. Silakan cek performa minggu ini!"
            await kirim_discord(session, pesan_jumat)

if __name__ == "__main__":
    asyncio.run(main())
