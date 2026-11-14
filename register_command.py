import os
import requests

APP_ID = os.environ["DISCORD_APP_ID"]
BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

URL = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
HEADERS = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

COMMANDS = [
    {
        "name": "cek",
        "description": "Analisis kuantitatif saham BEI (kosongkan untuk scan semua watchlist)",
        "options": [{"name": "kode", "description": "Contoh: BBCA atau BBCA,TLKM", "type": 3, "required": False}],
    },
    {
        "name": "screener",
        "description": "Cari saham BEI terbaik hari ini & pangkas watchlist yang buruk",
    },
    {
        "name": "beli",
        "description": "Catat posisi beli agar dikawal Chandelier Trailing Stop",
        "options": [
            {"name": "kode", "description": "Kode saham (contoh: BBCA)", "type": 3, "required": True},
            {"name": "harga", "description": "Harga rata-rata beli", "type": 10, "required": True},
            {"name": "lot", "description": "Jumlah lot", "type": 4, "required": False},
        ],
    },
    {
        "name": "jual",
        "description": "Tutup posisi portofolio saham",
        "options": [{"name": "kode", "description": "Kode saham yang dijual", "type": 3, "required": True}],
    },
    {
        "name": "watchlist",
        "description": "Lihat, tambah, atau hapus daftar pantauan saham",
        "options": [
            {
                "name": "aksi",
                "description": "Pilih aksi",
                "type": 3,
                "required": True,
                "choices": [
                    {"name": "lihat", "value": "lihat"},
                    {"name": "tambah", "value": "tambah"},
                    {"name": "hapus", "value": "hapus"},
                ],
            },
            {"name": "kode", "description": "Kode saham (untuk tambah/hapus)", "type": 3, "required": False},
        ],
    },
    {
        "name": "berita",
        "description": "Cek Keterbukaan Informasi BEI & RSS CNBC/Kontan terbaru",
        "options": [{"name": "kode", "description": "Filter kode emiten (opsional)", "type": 3, "required": False}],
    },
    {
        "name": "performa",
        "description": "Lihat audit akurasi sinyal bot dari Signal Ledger",
    },
]

resp = requests.put(URL, headers=HEADERS, json=COMMANDS, timeout=15)
print("Status:", resp.status_code)
print(resp.json())
