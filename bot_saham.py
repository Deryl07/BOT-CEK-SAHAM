import yfinance as yf
import pandas as pd
import datetime
import holidays
import asyncio
import aiohttp
import os

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK")

def ambil_berita(ticker_symbol):
    try:
        saham = yf.Ticker(f"{ticker_symbol}.JK")
        berita = saham.news
        if berita and len(berita) > 0:
            return berita[0].get('title', 'Tidak ada tajuk berita spesifik.')
    except Exception:
        pass
    return "Tidak ada pembaruan berita."

def analisis_akurat(ticker_symbol):
    try:
        ticker = f"{ticker_symbol}.JK"
        saham_yf = yf.Ticker(ticker)
        data = saham_yf.history(period="5d", interval="15m")
        
        if data.empty or len(data) < 21:
            return None
            
        data['Typical_Price'] = (data['High'] + data['Low'] + data['Close']) / 3
        data['Volume_Price'] = data['Typical_Price'] * data['Volume']
        data['VWMA_20'] = data['Volume_Price'].rolling(window=20).sum() / data['Volume'].rolling(window=20).sum()
        
        delta = data['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        data['RSI_14'] = 100 - (100 / (1 + rs))
        
        now = data.iloc[-1]
        prev = data.iloc[-2]
        
        rata_volume = data['Volume'].rolling(window=20).mean().iloc[-1]
        ledakan_volume = now['Volume'] > (rata_volume * 1.5)
        
        batas_bawah = now['VWMA_20'] * 0.99
        batas_atas = now['VWMA_20'] * 1.01
        
        fase_akumulasi = (now['Close'] >= batas_bawah) and (now['Close'] <= batas_atas)
        filter_palsu = now['Close'] >= prev['Close']
        potensi_awal = now['RSI_14'] < 60
        
        status = "Pantau"
        skor = "C"
        
        # Deteksi Beli (Fase Awal Akumulasi Institusi)
        if fase_akumulasi and ledakan_volume and potensi_awal and filter_palsu:
            status = "Beli (Fase Awal Uptrend)"
            skor = "A"
            
        # Deteksi Jual (Fase Distribusi Institusi / Buang Barang)
        elif now['Close'] < batas_bawah and ledakan_volume and not filter_palsu:
            status = "JUAL SEKARANG (Potensi Turun Tajam)"
            skor = "F"
            
        # Evaluasi Tren Lanjutan untuk Raport
        elif now['Close'] > now['VWMA_20'] and now['RSI_14'] > 50:
            skor = "B"
        elif now['Close'] < now['VWMA_20'] and now['RSI_14'] < 40:
            skor = "F"
            
        return {
            "harga": now['Close'],
            "status": status,
            "skor": skor,
            "rsi": now['RSI_14'],
            "vwma": now['VWMA_20']
        }
    except Exception:
        return None

async def kirim_pesan(session, pesan):
    if not WEBHOOK_URL:
        return
    try:
        async with session.post(WEBHOOK_URL, json={"content": pesan}) as resp:
            pass
    except Exception:
        pass

async def main():
    tz_wib = datetime.timezone(datetime.timedelta(hours=7))
    waktu = datetime.datetime.now(tz_wib)
    
    str_waktu = waktu.strftime("%H:%M:%S")
    str_tanggal = waktu.strftime("%d-%m-%Y")
    stempel_waktu = f"Jam: {str_waktu} WIB, Tanggal: {str_tanggal}"
    
    hari = waktu.weekday()
    jam = waktu.hour
    
    libur = holidays.country_holidays('ID')
    
    async with aiohttp.ClientSession() as session:
        # Aturan 7: Pengecekan Hari Libur atau Akhir Pekan
        if waktu.date() in libur or hari > 4:
            if jam == 9:
                await kirim_pesan(session, f"@everyone\nInformasi Sistem: Pasar saham sedang libur atau akhir pekan. ({stempel_waktu})")
            return

        # Aturan 8: Pengecekan Jam Operasional Pasar
        if jam < 9 or jam >= 16:
            if jam == 16:
                pass # Lanjutkan eksekusi untuk mengirimkan laporan penutupan pasar
            else:
                return

        # Aturan 10: Membaca Watchlist
        try:
            with open("watchlist.txt", "r") as file:
                daftar_saham = [line.strip().upper() for line in file if line.strip()]
        except FileNotFoundError:
            daftar_saham = ["BULL", "TLKM", "BBCA", "GOTO", "MEDC"]

        # Aturan 6: Penanda Waktu Presisi
        pesan_utama = f"@everyone\n**Laporan Eksekusi Sistem**\nPengambilan data indikator dan berita pada {stempel_waktu}\n\n"
        
        if jam == 9:
            pesan_utama = f"@everyone\n**Pemberitahuan: Pasar Saham Telah Dibuka**\nPengambilan data awal pada {stempel_waktu}\n\n"
            
        raport_teks = ""
        evaluasi_watchlist = "\n**Saran Evaluasi Watchlist:**\n"
        ada_evaluasi = False
        
        for saham in daftar_saham:
            hasil = analisis_akurat(saham)
            berita = ambil_berita(saham)
            
            # Aturan 11: Harga Live untuk Semua Saham
            if hasil:
                pesan_utama += f"**{saham}** | Harga: Rp {hasil['harga']:,.0f} | Status: {hasil['status']}\n"
                
                # Aturan 3: Berita Saham
                pesan_utama += f"Berita Terkini: {berita}\n"
                
                if "Beli" in hasil['status']:
                    pesan_utama += f"Instruksi: Sinyal divalidasi. Harga mendekati batas institusi (Rp {hasil['vwma']:,.0f}) dengan lonjakan volume beli.\n"
                elif "JUAL" in hasil['status']:
                    pesan_utama += f"Instruksi: Peringatan Dini. Institusi sedang melakukan distribusi. Harga menembus batas bawah (Rp {hasil['vwma']:,.0f}) dengan volume besar.\n"
                
                raport_teks += f"- {saham}: Skor {hasil['skor']} (RSI: {hasil['rsi']:.1f})\n"
                
                # Aturan 4: Raport dan Saran Evaluasi Watchlist
                if hasil['skor'] == "F":
                    evaluasi_watchlist += f"- Pertimbangkan untuk mengeluarkan {saham} (Tren distribusi, momentum patah).\n"
                    ada_evaluasi = True
            else:
                pesan_utama += f"**{saham}** | Gagal mengambil data pasar.\n"
            pesan_utama += "------------------------\n"

        # Aturan 4 dan 8: Laporan Teks Penutupan Pasar
        if jam == 16:
            pesan_utama = f"@everyone\n**Pemberitahuan: Pasar Saham Telah Ditutup**\nLaporan akhir ditarik pada {stempel_waktu}\n\n**Raport Saham Hari Ini:**\n{raport_teks}"
            if ada_evaluasi:
                pesan_utama += evaluasi_watchlist
            else:
                pesan_utama += "\n**Saran Evaluasi Watchlist:**\n- Seluruh saham terpantau dalam kondisi teknikal yang stabil. Tidak ada saran pengeluaran."

        await kirim_pesan(session, pesan_utama)

if __name__ == "__main__":
    asyncio.run(main())
