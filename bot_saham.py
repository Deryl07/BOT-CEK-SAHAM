import yfinance as yf
import pandas as pd
import aiohttp
import asyncio
import datetime
import os
import holidays
import json
import matplotlib.pyplot as plt

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")
PING_USER = "@everyone"

catatan_notif = {}

def baca_watchlist():
    if os.path.exists("watchlist.txt"):
        with open("watchlist.txt", "r") as f:
            saham = [line.strip().upper() for line in f if line.strip()]
            if saham:
                return saham
    return ["BULL", "CBRE", "ENRG", "FORU", "KDTN", "TLKM", "MEDC"]

def hitung_atr(df, periode=14):
    high_low = df['High'] - df['Low']
    high_close = abs(df['High'] - df['Close'].shift())
    low_close = abs(df['Low'] - df['Close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    return ranges.max(axis=1).rolling(periode).mean()

def deteksi_pola_lilin(df):
    if len(df) < 2:
        return "➖ Pola standar."
    hari_ini = df.iloc[-1]
    kemarin = df.iloc[-2]
    
    body_sekarang = abs(hari_ini['Close'] - hari_ini['Open'])
    body_kemarin = abs(kemarin['Close'] - kemarin['Open'])
    
    hammer = (hari_ini['Close'] > hari_ini['Open']) and ((hari_ini['Open'] - hari_ini['Low']) > (2 * body_sekarang)) and ((hari_ini['High'] - hari_ini['Close']) < (0.5 * body_sekarang))
    bullish_engulfing = (kemarin['Close'] < kemarin['Open']) and (hari_ini['Close'] > hari_ini['Open']) and (hari_ini['Close'] >= kemarin['Open']) and (hari_ini['Open'] <= kemarin['Close'])
    
    if hammer:
        return "🔨 **Pola Hammer (Palu Sakti):** Sinyal pantulan kuat ke atas. **SARAN AKSI: Waktunya siap-siap IKUT BELI!**"
    elif bullish_engulfing:
        return "🕯️ **Pola Bullish Engulfing:** Pembeli mendominasi pasar secara mutlak. **SARAN AKSI: Sangat direkomendasikan untuk IKUT BELI!**"
    return "➖ Pola lilin standar. **SARAN AKSI: Beli hanya jika indikator lain sangat kuat.**"

def cek_multi_timeframe(ticker):
    try:
        t = yf.Ticker(f"{ticker}.JK")
        df_1d = t.history(period="10d", interval="1d")
        df_1h = t.history(period="5d", interval="1h")
        
        tren_harian = df_1d['Close'].iloc[-1] > df_1d['Close'].iloc[-5] if len(df_1d) >= 5 else False
        tren_perjam = df_1h['Close'].iloc[-1] > df_1h['Close'].iloc[-3] if len(df_1h) >= 3 else False
        
        if tren_harian and tren_perjam:
            return "✅ **Multi-Timeframe Kompak:** Grafik Harian & Per Jam SAMA-SAMA NAIK. **SARAN AKSI: Sangat aman untuk eksekusi beli!**"
        elif not tren_harian and not tren_perjam:
            return "❌ **Multi-Timeframe Lemah:** Grafik Harian & Per Jam kompak turun. **SARAN AKSI: Hindari dulu, jangan sentuh!**"
        else:
            return "⚠️ **Multi-Timeframe Beda Arah:** Belum sinkron. **SARAN AKSI: Pantau ketat, jangan beli terlalu banyak (cicil).**"
    except Exception:
        return "➖ Data multi-timeframe tidak tersedia."

def cek_bandar_dan_insider(ticker_obj, df):
    try:
        vol_hari_ini = df['Volume'].iloc[-1]
        vol_rata2 = df['Volume'].rolling(20).mean().iloc[-1]
        
        info_sentimen = "➖ Info Insider/Sentimen: Belum ada pergerakan mencolok."
        berita = ticker_obj.news
        if berita:
            judul_gabungan = " ".join([b.get('title', '').lower() for b in berita[:5]])
            if any(kata in judul_gabungan for kata in ['akuisisi', 'direktur beli', 'borong', 'investasi']):
                info_sentimen = "🕵️‍♂️ **INSIDER INFO:** Ada sentimen orang dalam/perusahaan borong saham! **SARAN AKSI: Ikut akumulasi sekarang sebelum meledak!**"
        
        if vol_hari_ini >= (3.0 * vol_rata2):
            return f"🌐 **BANDARMOLOGY DETECTED:** Volume meledak 3x lipat! Asing/Bandar masuk besar-besaran. **SARAN AKSI: NUMPANG BELI SEKARANG JUGA!**\n{info_sentimen}"
        elif vol_hari_ini >= (1.5 * vol_rata2):
            return f"📈 **Akumulasi Sedang:** Mulai ada cicilan dana masuk.\n{info_sentimen}"
    except Exception:
        pass
    return "➖ Arus modal bandar/asing terpantau normal."

def cek_penurunan_beruntun(df):
    if len(df) < 4:
        return False, 0
    penurunan = 0
    for i in range(1, 5):
        if df['Close'].iloc[-i] < df['Close'].iloc[-i-1]:
            penurunan += 1
        else:
            break
    if penurunan >= 3:
        return True, penurunan
    return False, penurunan

async def kirim_notifikasi_gambar(session, pesan, nama_file=None):
    if nama_file and os.path.exists(nama_file):
        with open(nama_file, 'rb') as f:
            gambar = f.read()
        data = aiohttp.FormData()
        data.add_field('payload_json', json.dumps({"content": pesan}))
        data.add_field('file', gambar, filename=nama_file, content_type='image/png')
        async with session.post(WEBHOOK_URL, data=data) as response:
            pass
        os.remove(nama_file)
    else:
        data = {"content": pesan}
        async with session.post(WEBHOOK_URL, json=data) as response:
            pass

def buat_grafik(df, ticker):
    plt.style.use('dark_background')
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 12), gridspec_kw={'height_ratios': [2, 1, 1]})
    
    support = df['Close'].tail(20).min()
    resistance = df['Close'].tail(20).max()

    ax1.plot(df.index, df['Close'], label='Harga', color='cyan', linewidth=2)
    ax1.plot(df.index, df['SMA_50'], label='SMA 50', color='yellow', linestyle='--')
    ax1.plot(df.index, df['SMA_200'], label='SMA 200', color='red', linestyle='--')
    ax1.axhline(support, color='lime', linestyle='-', linewidth=2, label=f'Lantai (Support): {support:.0f}')
    ax1.axhline(resistance, color='magenta', linestyle='-', linewidth=2, label=f'Atap (Resistance): {resistance:.0f}')
    ax1.legend(loc='upper left')
    ax1.set_title(f"Analisis Teknikal {ticker}", fontsize=14, fontweight='bold')
    ax1.grid(color='gray', linestyle=':', linewidth=0.5)
    
    ax2.plot(df.index, df['MACD'], label='MACD', color='cyan')
    ax2.plot(df.index, df['Signal'], label='Signal', color='red')
    macd_hist = df['MACD'] - df['Signal']
    warna_bar = ['green' if val > 0 else 'red' for val in macd_hist]
    ax2.bar(df.index, macd_hist, color=warna_bar, alpha=0.5)
    ax2.legend(loc='upper left')
    ax2.set_title("MACD")
    ax2.grid(color='gray', linestyle=':', linewidth=0.5)
    
    ax3.plot(df.index, df['RSI'], label='RSI', color='magenta')
    ax3.axhline(70, color='red', linestyle='--')
    ax3.axhline(30, color='green', linestyle='--')
    ax3.legend(loc='upper left')
    ax3.set_title("RSI (Momentum)")
    ax3.grid(color='gray', linestyle=':', linewidth=0.5)
    
    plt.tight_layout()
    nama_file = f"grafik_{ticker}.png"
    plt.savefig(nama_file)
    plt.close()
    return nama_file

async def ultimate_bot_discord(ticker, session, tanggal_sekarang, waktu_ui):
    saham_objek = yf.Ticker(f"{ticker}.JK")
    df = saham_objek.history(period="1y")

    if df.empty or len(df) < 200:
        return

    df.index = df.index.tz_localize(None)
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

    pola_lilin_info = deteksi_pola_lilin(df)
    multi_tf_info = cek_multi_timeframe(ticker)
    info_bandar = cek_bandar_dan_insider(saham_objek, df)
    turun_beruntun, jumlah_turun = cek_penurunan_beruntun(df)

    if turun_beruntun:
        if catatan_notif.get(f"{ticker}_TURUN") != tanggal_sekarang:
            grafik = buat_grafik(df.iloc[-100:], ticker)
            pesan_turun = (
                f"{PING_USER}\n🚨 **ALARM DARURAT: {ticker} ANJLOK BERUNTUN!** 🚨\n"
                f"🕒 *{waktu_ui}*\n"
                f"Harga: **Rp {hari_ini['Close']:.0f}**\n\n"
                f"⚠️ Saham ini turun merah **{jumlah_turun} hari berturut-turut!**\n"
                f"💡 **SARAN AKSI:** JANGAN SENTUH DULU! Ibarat nangkap pisau jatuh, tunggu sampai ada tanda mantul naik (hijau) baru berani masuk.\n"
            )
            await kirim_notifikasi_gambar(session, pesan_turun, grafik)
            catatan_notif[f"{ticker}_TURUN"] = tanggal_sekarang

    beli_tren = hari_ini['Close'] > hari_ini['SMA_200'] and hari_ini['Close'] > hari_ini['SMA_50']
    beli_macd = kemarin['MACD'] <= kemarin['Signal'] and hari_ini['MACD'] > hari_ini['Signal']
    beli_volume = hari_ini['Volume'] >= (1.5 * hari_ini['Rata_Volume_20'])
    beli_rsi = hari_ini['RSI'] < 70

    if beli_tren and beli_macd and beli_volume and beli_rsi:
        if catatan_notif.get(f"{ticker}_BELI") != tanggal_sekarang:
            stop_loss = hari_ini['Close'] - (hari_ini['ATR'] * 2)
            risiko = hari_ini['Close'] - stop_loss
            target_profit = hari_ini['Close'] + (risiko * 2)
            grafik = buat_grafik(df.iloc[-100:], ticker)

            pesan_beli = (
                f"{PING_USER}\n👑 **ULTIMATE BUY SIGNAL: {ticker}** 👑\n"
                f"🕒 *{waktu_ui}*\n"
                f"Harga Masuk: **Rp {hari_ini['Close']:.0f}**\n\n"
                f"🎯 **Saran & Instruksi Aksi:**\n"
                f"• {pola_lilin_info}\n"
                f"• {multi_tf_info}\n"
                f"• {info_bandar}\n\n"
                f"🚨 *JUAL RUGI (Cut Loss) di:* Rp {stop_loss:.0f}\n"
                f"🎯 *TAKE PROFIT di:* Rp {target_profit:.0f}\n"
            )
            await kirim_notifikasi_gambar(session, pesan_beli, grafik)
            catatan_notif[f"{ticker}_BELI"] = tanggal_sekarang

    jual_macd_turun = kemarin['MACD'] >= kemarin['Signal'] and hari_ini['MACD'] < hari_ini['Signal']
    jual_jebol_sma50 = kemarin['Close'] >= kemarin['SMA_50'] and hari_ini['Close'] < hari_ini['SMA_50']

    pesan_jual = []
    if jual_jebol_sma50:
        pesan_jual.append("🚨 Harga jebol garis aman (SMA 50). **SARAN AKSI: SEGERA JUAL, TREN HANCUR!**")
    if jual_macd_turun:
        pesan_jual.append("⚠️ MACD menukik turun. **SARAN AKSI: Siap-siap amankan keuntungan (Take Profit Sebagian).**")
    if hari_ini['RSI'] > 70:
        pesan_jual.append("⚠️ RSI kemahalan (>70). **SARAN AKSI: Rawan orang jualan massal, jangan beli di pucuk!**")

    if pesan_jual:
        if catatan_notif.get(f"{ticker}_JUAL") != tanggal_sekarang:
            alasan = "\n".join(pesan_jual)
            grafik = buat_grafik(df.iloc[-100:], ticker)
            pesan_peringatan = f"{PING_USER}\n🔴 **SELL ALERT: {ticker}** 🔴\n🕒 *{waktu_ui}*\nHarga: Rp {hari_ini['Close']:.0f}\n\n{alasan}"
            await kirim_notifikasi_gambar(session, pesan_peringatan, grafik)
            catatan_notif[f"{ticker}_JUAL"] = tanggal_sekarang

async def rekap_mingguan(session, daftar_saham, waktu_ui):
    pesan_rapor = f"🏆 **RAPOR PASAR MINGGUAN & ANALISIS** 🏆\n🗓️ {waktu_ui}\n\nBerikut rekap saham watchlist minggu ini:\n\n"
    data_csv = []
    
    for saham in daftar_saham:
        try:
            ticker_obj = yf.Ticker(f"{saham}.JK")
            df_minggu = ticker_obj.history(period="5d")
            if len(df_minggu) >= 2:
                harga_jumat = df_minggu['Close'].iloc[-1]
                harga_senin = df_minggu['Close'].iloc[0]
                persen_minggu = ((harga_jumat - harga_senin) / harga_senin) * 100
                ikon = "📈" if persen_minggu > 0 else "📉" if persen_minggu < 0 else "➖"
                
                saran = "Hold/Pantau"
                if persen_minggu > 5:
                    saran = "Cuan tebal, pertimbangkan Take Profit sebagian"
                elif persen_minggu < -5:
                    saran = "Waspada tren turun, ketatkan Cut Loss"
                    
                pesan_rapor += f"• **{saham}**: {ikon} {persen_minggu:.2f}% (Rp {harga_jumat:.0f}) | *Saran: {saran}*\n"
                data_csv.append({"Emiten": saham, "Harga_Akhir": harga_jumat, "Perubahan_Mingguan_Persen": persen_minggu})
        except Exception:
            pass
            
    pesan_rapor += "\n📁 *Data mingguan sedang disimpan ke Excel/CSV!*"
    
    if data_csv:
        df_csv = pd.DataFrame(data_csv)
        df_csv.to_csv("rekap_mingguan.csv", index=False)
        await kirim_notifikasi_gambar(session, pesan_rapor, "rekap_mingguan.csv")
    else:
        await kirim_notifikasi_gambar(session, pesan_rapor)

async def main():
    daftar_saham = baca_watchlist()
    
    async with aiohttp.ClientSession(trust_env=True) as session:
        waktu_sekarang = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=7)))
        nama_hari = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
        hari_ini_str = nama_hari[waktu_sekarang.weekday()]
        waktu_ui = f"{hari_ini_str}, {waktu_sekarang.strftime('%d-%m-%Y %H:%M:%S')} WIB"
        tanggal_sekarang = waktu_sekarang.strftime("%Y-%m-%d")
        
        jam = waktu_sekarang.hour
        hari_int = waktu_sekarang.weekday()

        libur_indo = holidays.country_holidays('ID')
        if tanggal_sekarang in libur_indo:
            return

        if hari_int >= 5:
            return

        if jam < 9 or jam >= 16:
            if hari_int == 4 and jam == 16:
                await rekap_mingguan(session, daftar_saham, waktu_ui)
            return

        for saham in daftar_saham:
            try:
                await ultimate_bot_discord(saham, session, tanggal_sekarang, waktu_ui)
                await asyncio.sleep(3)
            except Exception:
                pass

if __name__ == "__main__":
    asyncio.run(main())
