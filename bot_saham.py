import yfinance as yf
import pandas as pd
import datetime
import holidays
import os
import requests
import warnings
from flask import Flask, jsonify

warnings.filterwarnings('ignore')

app = Flask(__name__)
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

def ambil_berita(ticker_symbol):
    try:
        saham = yf.Ticker(f"{ticker_symbol}.JK")
        berita = saham.news
        if berita and len(berita) > 0:
            return berita[0].get('title', 'Tidak ada rilis berita terbaru pada interval ini.')
    except Exception:
        pass
    return "Data berita tidak dapat ditarik dari peladen pusat."

def hitung_seasonality(ticker_symbol, bulan_sekarang):
    try:
        saham = yf.Ticker(f"{ticker_symbol}.JK")
        data = saham.history(period="5y", interval="1mo")
        if data.empty:
            return 50.0
        data_bulan = data[data.index.month == bulan_sekarang]
        if data_bulan.empty:
            return 50.0
        positif = (data_bulan['Close'] > data_bulan['Open']).sum()
        total = len(data_bulan)
        return (positif / total) * 100
    except Exception:
        return 50.0

def analisis_mendalam(ticker_symbol, bulan_sekarang):
    try:
        ticker = f"{ticker_symbol}.JK"
        saham_yf = yf.Ticker(ticker)
        data = saham_yf.history(period="5d", interval="5m")
        
        if data.empty or len(data) < 35:
            return None
            
        data['Typical_Price'] = (data['High'] + data['Low'] + data['Close']) / 3
        data['Volume_Price'] = data['Typical_Price'] * data['Volume']
        data['VWMA_20'] = data['Volume_Price'].rolling(window=20).sum() / data['Volume'].rolling(window=20).sum()
        
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        data['RSI_14'] = 100 - (100 / (1 + rs))
        
        exp1 = data['Close'].ewm(span=12, adjust=False).mean()
        exp2 = data['Close'].ewm(span=26, adjust=False).mean()
        data['MACD'] = exp1 - exp2
        data['Signal_Line'] = data['MACD'].ewm(span=9, adjust=False).mean()
        
        support = data['Low'].rolling(window=20).min().iloc[-1]
        resistance = data['High'].rolling(window=20).max().iloc[-1]
        
        now = data.iloc[-1]
        prev = data.iloc[-2]
        rata_volume = data['Volume'].rolling(window=20).mean().iloc[-1]
        
        validasi_volume_akut = now['Volume'] > (rata_volume * 2.0)
        filter_palsu = now['Close'] >= prev['Close']
        
        batas_bawah = now['VWMA_20'] * 0.99
        batas_atas = now['VWMA_20'] * 1.01
        
        fase_akumulasi = (now['Close'] >= batas_bawah) and (now['Close'] <= batas_atas)
        tren_positif = (now['MACD'] > now['Signal_Line']) and (prev['MACD'] <= prev['Signal_Line'])
        momentum_awal = 40 < now['RSI_14'] < 65
        distribusi_terdeteksi = now['Close'] < batas_bawah and now['MACD'] < now['Signal_Line']
        
        seasonality = hitung_seasonality(ticker_symbol, bulan_sekarang)
        musiman_ok = seasonality >= 50.0
        
        status = "Dalam Pemantauan"
        skor = "C"
        
        if fase_akumulasi and validasi_volume_akut and tren_positif and momentum_awal and filter_palsu and musiman_ok:
            status = "INSTRUKSI BELI (Awal Kenaikan Terkonfirmasi)"
            skor = "A"
        elif distribusi_terdeteksi and validasi_volume_akut:
            status = "INSTRUKSI JUAL (Penurunan Terkonfirmasi)"
            skor = "F"
        elif now['Close'] > now['VWMA_20'] and now['MACD'] > now['Signal_Line']:
            skor = "B"
        elif now['Close'] < now['VWMA_20'] and now['MACD'] < now['Signal_Line']:
            skor = "F"
            
        return {
            "harga": now['Close'],
            "status": status,
            "skor": skor,
            "rsi": now['RSI_14'],
            "vwma": now['VWMA_20'],
            "macd_positif": now['MACD'] > now['Signal_Line'],
            "support": support,
            "resistance": resistance,
            "seasonality": seasonality
        }
    except Exception:
        return None

def kirim_pesan(pesan):
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json={"content": pesan}, timeout=10)
    except Exception:
        pass

@app.route('/')
def index():
    return "Peladen Analisis Institusional Beroperasi Secara Formal."

@app.route('/eksekusi')
def eksekusi():
    tz_wib = datetime.timezone(datetime.timedelta(hours=7))
    waktu = datetime.datetime.now(tz_wib)
    
    hari_dict = {0: "Senin", 1: "Selasa", 2: "Rabu", 3: "Kamis", 4: "Jumat", 5: "Sabtu", 6: "Minggu"}
    bulan_dict = {1: "Januari", 2: "Februari", 3: "Maret", 4: "April", 5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus", 9: "September", 10: "Oktober", 11: "November", 12: "Desember"}
    
    nama_hari = hari_dict[waktu.weekday()]
    nama_bulan = bulan_dict[waktu.month]
    
    str_waktu = waktu.strftime("%H:%M:%S")
    stempel_waktu = f"Jam {waktu.hour:02d}, Menit {waktu.minute:02d}, Detik {waktu.second:02d}, Hari {nama_hari}, Tanggal {waktu.day:02d} Bulan {nama_bulan} Tahun {waktu.year}"
    
    hari = waktu.weekday()
    jam = waktu.hour
    menit = waktu.minute
    
    libur = holidays.country_holidays('ID')
    
    if waktu.date() in libur or hari > 4:
        if jam == 9 and menit < 5:
            kirim_pesan(f"@everyone\n**Pemberitahuan Sistem:** Pasar saham sedang libur atau akhir pekan. Transaksi ditutup.\nData ditarik pada: {stempel_waktu}")
        return jsonify({"status": "Libur", "waktu": str_waktu})

    if jam < 9 or jam >= 16:
        if jam == 16 and menit < 5:
            pass 
        else:
            return jsonify({"status": "Luar Operasional", "waktu": str_waktu})

    try:
        with open("watchlist.txt", "r") as file:
            daftar_saham = [line.strip().upper() for line in file if line.strip()]
    except FileNotFoundError:
        daftar_saham = ["BULL", "TLKM", "BBCA", "GOTO", "MEDC"]

    pesan_utama = f"@everyone\n**Laporan Eksekusi Algoritma Institusional**\nData ditarik pada: {stempel_waktu}\n\n"
    
    if jam == 9 and menit < 5:
        pesan_utama = f"@everyone\n**Pemberitahuan Sistem:** Sesi perdagangan pasar saham telah dibuka.\nData ditarik pada: {stempel_waktu}\n\n"
        
    raport_teks = ""
    evaluasi_watchlist = "\n**Saran Modifikasi Daftar Pantauan (Watchlist):**\n"
    ada_evaluasi = False
    
    for saham in daftar_saham:
        hasil = analisis_mendalam(saham, waktu.month)
        berita = ambil_berita(saham)
        
        if hasil:
            pesan_utama += f"**Emiten {saham}** | Harga Aktual: Rp {hasil['harga']:,.0f} | Status: {hasil['status']}\n"
            pesan_utama += f"Rencana Perdagangan: Beli di area Rp {hasil['support']:,.0f} | Jual di area Rp {hasil['resistance']:,.0f}\n"
            pesan_utama += f"Analisis Musiman Bulan {nama_bulan}: Probabilitas keberhasilan {hasil['seasonality']:.1f}%\n"
            pesan_utama += f"Tajuk Berita Resmi: {berita}\n"
            
            raport_teks += f"- {saham}: Peringkat {hasil['skor']} (Probabilitas Musiman: {hasil['seasonality']:.1f}%)\n"
            
            if hasil['skor'] == "F" or hasil['seasonality'] < 50.0:
                evaluasi_watchlist += f"- Keluarkan {saham} dari pantauan. (Alasan: Skor F atau Musiman di bawah standar 50%).\n"
                ada_evaluasi = True
        else:
            pesan_utama += f"**Emiten {saham}** | Terjadi kegagalan penarikan data metrik.\n"
        pesan_utama += "------------------------\n"

    if jam == 16 and menit < 5:
        pesan_utama = f"@everyone\n**Pemberitahuan Sistem:** Sesi perdagangan pasar saham telah ditutup.\nData ditarik pada: {stempel_waktu}\n\n**Raport Saham Harian:**\n{raport_teks}"
        if ada_evaluasi:
            pesan_utama += evaluasi_watchlist
        else:
            pesan_utama += "\n**Saran Modifikasi Daftar Pantauan (Watchlist):**\n- Pertahankan seluruh saham, kondisi fundamental dan teknikal stabil."
            
        if hari == 4:
            pesan_utama += "\n**Rekomendasi Kandidat Watchlist Baru Untuk Minggu Depan:**\n"
            kandidat_tambahan = ["BBRI", "BMRI", "AMMN", "ASII", "BRPT", "BREN"]
            for kandidat in kandidat_tambahan:
                if kandidat not in daftar_saham:
                    hasil_kandidat = analisis_mendalam(kandidat, waktu.month)
                    if hasil_kandidat and hasil_kandidat['seasonality'] >= 50.0 and hasil_kandidat['skor'] in ["A", "B"]:
                        pesan_utama += f"- Masukkan {kandidat} (Skor {hasil_kandidat['skor']}, Musiman {hasil_kandidat['seasonality']:.1f}%).\n"

    kirim_pesan(pesan_utama)
    return jsonify({"status": "Analisis Selesai", "waktu": str_waktu})

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
