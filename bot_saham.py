import yfinance as yf
import pandas as pd
import aiohttp
import asyncio
import datetime
import os
import holidays

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

catatan_notif = {}

def hitung_atr(df, periode=14):
    high_low = df['High'] - df['Low']
    high_close = abs(df['High'] - df['Close'].shift())
    low_close = abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    return ranges.max(axis=1).rolling(periode).mean()

async def kirim_notifikasi(session, pesan):
    data = {"content": pesan}
    async with session.post(WEBHOOK_URL, json=data) as response:
        if response.status not in (200, 204):
            print(f"Gagal mengirim pesan, status code: {response.status}")

async def ultimate_bot_discord(ticker, session, tanggal_sekarang, waktu_ui):
    saham_objek = yf.Ticker(f"{ticker}.JK")
    df = saham_objek.history(period="1y")

    if not df.empty:
        df.index = df.index.tz_localize(None)

    if df.empty or len(df) < 200:
        return

    df['SMA_50'] = df['Close'].rolling(window=50).mean()
    df['SMA_200'] = df['Close'].rolling(window=200).mean()

    df['EMA_12'] = df['Close'].ewm(span=12, adjust=False).mean()
    df['EMA_26'] = df['Close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = df['EMA_12'] - df['EMA_26']
    df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()

    df['Rata_Volume_20'] = df['Volume'].rolling(window=20).mean()

    delta = df['Close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13, adjust=False).mean() / down.ewm(com=13, adjust=False).mean()
    df['RSI'] = 100 - (100 / (1 + rs))

    df['ATR'] = hitung_atr(df)

    kemarin = df.iloc[-2]
    hari_ini = df.iloc[-1]

    sinyal_terkirim = False 

    beli_tren = hari_ini['Close'] > hari_ini['SMA_200'] and hari_ini['Close'] > hari_ini['SMA_50']
    beli_macd = kemarin['MACD'] <= kemarin['Signal'] and hari_ini['MACD'] > hari_ini['Signal']
    beli_volume = hari_ini['Volume'] >= (1.5 * hari_ini['Rata_Volume_20'])
    beli_rsi = hari_ini['RSI'] < 70

    if beli_tren and beli_macd and beli_volume and beli_rsi:
        if catatan_notif.get(f"{ticker}_BELI") != tanggal_sekarang:
            stop_loss = hari_ini['Close'] - (hari_ini['ATR'] * 2)
            risiko = hari_ini['Close'] - stop_loss
            target_profit = hari_ini['Close'] + (risiko * 2)

            pesan_beli = (
                f"👑 **ULTIMATE BUY SIGNAL: {ticker}** 👑\n"
                f"🕒 *Ditemukan Pada: {waktu_ui}*\n"
                f"Harga Masuk: **Rp {hari_ini['Close']:.0f}**\n\n"
                f"✅ Momentum & Volume Terkonfirmasi \n"
                f"🚨 *JUAL RUGI (Cut Loss) kalau turun ke:* Rp {stop_loss:.0f}\n"
                f"🎯 *BUNGKUS CUAN (Take Profit) kalau naik ke:* Rp {target_profit:.0f}"
            )
            await kirim_notifikasi(session, pesan_beli)
            print(f"[{ticker}] Sinyal Beli dikirim ke Discord! ({waktu_ui})")

            catatan_notif[f"{ticker}_BELI"] = tanggal_sekarang
            sinyal_terkirim = True

    jual_macd_turun = kemarin['MACD'] >= kemarin['Signal'] and hari_ini['MACD'] < hari_ini['Signal']
    jual_rsi_mahal = kemarin['RSI'] <= 70 and hari_ini['RSI'] > 70
    jual_jebol_sma50 = kemarin['Close'] >= kemarin['SMA_50'] and hari_ini['Close'] < hari_ini['SMA_50']

    pesan_jual = []
    if jual_jebol_sma50:
        pesan_jual.append("🚨 **BAHAYA:** Harga jebol garis aman (SMA 50). Tren hancur, buruan keluar!")
    if jual_macd_turun:
        pesan_jual.append("⚠️ **WASPADA:** MACD mulai menukik. Tenaga naiknya udah habis.")
    if jual_rsi_mahal:
        pesan_jual.append("⚠️ **WASPADA:** RSI kemahalan (>70). Siap-siap orang pada jualan.")

    if pesan_jual:
        if catatan_notif.get(f"{ticker}_JUAL") != tanggal_sekarang:
            alasan = "\n".join(pesan_jual)
            pesan_peringatan = (
                f"🔴 **SELL ALERT / PERINGATAN: {ticker}** 🔴\n"
                f"🕒 *Ditemukan Pada: {waktu_ui}*\n"
                f"Harga Sekarang: Rp {hari_ini['Close']:.0f}\n\n"
                f"{alasan}\n\n"
                f"*(Amankan uangmu, siap-siap pencet tombol JUAL!)*"
            )
            await kirim_notifikasi(session, pesan_peringatan)
            print(f"[{ticker}] Peringatan Jual dikirim ke Discord! ({waktu_ui})")

            catatan_notif[f"{ticker}_JUAL"] = tanggal_sekarang
            sinyal_terkirim = True

    if not sinyal_terkirim:
        print(f"[{ticker}] Belum ada sinyal pergerakan baru. ({waktu_ui})")

async def main():
    daftar_saham = ["BULL", "CBRE", "ENRG", "FORU", "KDTN", "TLKM", "MEDC"]

    print("🚀 BOT NYALA!")

    async with aiohttp.ClientSession(trust_env=True) as session:
        waktu_sekarang = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        nama_hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
        hari_ini_str = nama_hari[waktu_sekarang.weekday()]
        waktu_ui = f"{hari_ini_str}, {waktu_sekarang.strftime('%d-%m-%Y %H:%M:%S')} WIB"
        tanggal_sekarang = waktu_sekarang.strftime("%Y-%m-%d")
        
        jam = waktu_sekarang.hour
        hari_int = waktu_sekarang.weekday()

        print(f"⏰ [{waktu_ui}] Mulai scan...")

        libur_indo = holidays.country_holidays('ID')
        if tanggal_sekarang in libur_indo:
            nama_libur = libur_indo.get(tanggal_sekarang)
            pesan_libur = f"ℹ️ **INFO PASAR SAHAM TUTUP**\n🗓️ {waktu_ui}\n\nHari ini tanggal merah lho ({nama_libur}). Bursa tutup, bot mau rebahan dulu!"
            await kirim_notifikasi(session, pesan_libur)
            print(f"Pasar tutup (Tanggal Merah: {nama_libur}). Bot mati.")
            return

        if hari_int >= 5: 
            pesan_libur = f"ℹ️ **INFO PASAR SAHAM TUTUP**\n🗓️ {waktu_ui}\n\nPasar saham sedang libur akhir pekan. Bot istirahat dulu ya!"
            await kirim_notifikasi(session, pesan_libur)
            print("Pasar tutup (Weekend). Bot mati.")
            return

        if jam < 9 or jam >= 16: 
            pesan_libur = f"ℹ️ **INFO PASAR SAHAM TUTUP**\n🗓️ {waktu_ui}\n\nSekarang berada di luar jam operasional bursa. Bot istirahat dulu ya!"
            await kirim_notifikasi(session, pesan_libur)
            print("Pasar tutup (Luar jam bursa). Bot mati.")
            return

        pesan_rekap = f"📊 **REKAP PERGERAKAN SAHAM (1 JAM TERAKHIR)**\n🗓️ {waktu_ui}\n\n"
        for saham in daftar_saham:
            try:
                ticker = yf.Ticker(f"{saham}.JK")
                df_1h = ticker.history(period="5d", interval="1h")
                if len(df_1h) >= 2:
                    harga_skrg = df_1h['Close'].iloc[-1]
                    harga_sblm = df_1h['Close'].iloc[-2]
                    selisih = harga_skrg - harga_sblm
                    persen = (selisih / harga_sblm) * 100 if harga_sblm != 0 else 0
                    
                    if selisih > 0:
                        ikon = "📈 NAIK"
                    elif selisih < 0:
                        ikon = "📉 TURUN"
                    else:
                        ikon = "➖ STAGNAN"
                        
                    pesan_rekap += f"• **{saham}**: Rp {harga_skrg:.0f} | {ikon} {persen:.2f}%\n"
                else:
                    pesan_rekap += f"• **{saham}**: Data belum masuk\n"
            except Exception as e:
                pesan_rekap += f"• **{saham}**: Error ambil data\n"
            await asyncio.sleep(1)
        
        await kirim_notifikasi(session, pesan_rekap)

        for saham in daftar_saham:
            try:
                await ultimate_bot_discord(saham, session, tanggal_sekarang, waktu_ui)
                await asyncio.sleep(3)
            except Exception as e:
                print(f"Error {saham}: {e}")
                
        print("✅ Scan selesai, bot berhenti.")

if __name__ == "__main__":
    asyncio.run(main())
