# 🤖 Finance Tracker Telegram Bot — Panduan Setup

Bot Telegram untuk mencatat keuangan pribadi secara otomatis ke Google Sheets menggunakan AI (Gemini).

---

## Prasyarat

- Python 3.10+
- Akun Telegram
- Akun Google

---

## Langkah 1 — Buat Telegram Bot

1. Buka Telegram, cari **@BotFather**
2. Kirim `/newbot`
3. Ikuti instruksi — masukkan nama dan username bot
4. Salin **token** yang diberikan (format: `123456:ABC-DEF...`)
5. Simpan token ini untuk `.env`

---

## Langkah 2 — Dapatkan Gemini API Key (GRATIS)

1. Buka https://aistudio.google.com
2. Login dengan akun Google
3. Klik **"Get API Key"** → **"Create API key"**
4. Salin API key yang muncul
5. Simpan untuk `.env`

> **Free tier Gemini 1.5 Flash**: 15 request/menit, 1.500 request/hari — lebih dari cukup untuk penggunaan pribadi.

---

## Langkah 3 — Setup Google Sheets & Service Account

### 3a. Buat Spreadsheet
1. Buka https://sheets.google.com
2. Buat spreadsheet baru, beri nama misalnya "Keuangan Pribadi"
3. Salin **ID spreadsheet** dari URL:
   ```
   https://docs.google.com/spreadsheets/d/[INI_YANG_DISALIN]/edit
   ```

### 3b. Buat Service Account (untuk akses programatik)
1. Buka https://console.cloud.google.com
2. Buat project baru (atau gunakan yang ada)
3. Masuk ke **"APIs & Services"** → **"Enable APIs"**
4. Aktifkan:
   - **Google Sheets API**
   - **Google Drive API**
5. Masuk ke **"APIs & Services"** → **"Credentials"**
6. Klik **"Create Credentials"** → **"Service Account"**
7. Isi nama, klik **"Create and Continue"** → **"Done"**
8. Klik service account yang baru dibuat
9. Tab **"Keys"** → **"Add Key"** → **"Create new key"** → pilih **JSON**
10. File JSON akan otomatis terunduh — **simpan sebagai `credentials.json`** di folder yang sama dengan `bot.py`

### 3c. Bagikan Spreadsheet ke Service Account
1. Buka file `credentials.json`, cari nilai `"client_email"` (contoh: `bot@project.iam.gserviceaccount.com`)
2. Buka Google Sheets Anda
3. Klik tombol **"Share"** (Bagikan)
4. Tempelkan email service account tersebut
5. Beri akses **Editor**
6. Klik **"Send"**

---

## Langkah 4 — Install & Konfigurasi

```bash
# Clone / salin folder finance_bot ke komputer Anda
cd finance_bot

# Install dependencies
pip install -r requirements.txt

# Salin file contoh .env
cp .env.example .env
```

Edit file `.env` dan isi semua nilai:

```env
TELEGRAM_TOKEN=token_dari_botfather
GEMINI_API_KEY=api_key_dari_aistudio
SPREADSHEET_ID=id_dari_url_spreadsheet
SHEET_NAME=Transaksi
ALLOWED_USER_IDS=          # kosongkan dulu
```

---

## Langkah 5 — Jalankan Bot

```bash
python bot.py
```

Jika berhasil, akan muncul log:
```
INFO - Bot berjalan...
```

---

## Langkah 6 — Test Bot

1. Buka Telegram, cari bot Anda berdasarkan username
2. Kirim `/start`
3. Coba kirim transaksi:
   - `Beli air mineral 600ml 1 buah 5000`
   - `Makan siang nasi padang 20000`
   - `Gaji bulan Juni 5000000`
   - `Bayar listrik 150rb`
4. Cek Google Sheets — data seharusnya langsung masuk!

---

## Perintah Bot

| Perintah | Fungsi |
|---|---|
| `/start` | Tampilkan panduan singkat |
| `/bantuan` | Panduan lengkap penggunaan |
| `/ringkasan` | Ringkasan pemasukan & pengeluaran bulan ini |
| Teks bebas | Catat transaksi |

---

## Deploy (Opsional) — Agar Bot Berjalan 24/7

### Opsi A: PC sendiri (paling mudah)
Jalankan `python bot.py` dan biarkan terminal terbuka. Atau gunakan `screen`/`tmux` di Linux.

### Opsi B: Wispbyte (bisa free-tier)
1. Buka [wispbyte.com](https://wispbyte.com/).
2. Daftar dan login.
3. Klik "_Create a new server_".
4. Upload seluruh file project-nya ke Wispbyte di bagian _Files_.
5. Sesuaikan _Startup Command_ agar file Python dan dependensinya dapat jalan secara otomatis ketika server dinyalakan.

---

## Struktur Google Sheets

Bot akan otomatis membuat header kolom:

| Tanggal | Tipe | Kategori | Deskripsi | Qty | Harga Satuan | Total | Catatan | User |
|---|---|---|---|---|---|---|---|---|
| 2026-06-06 10:30:00 | pengeluaran | Makanan & Minuman | Air mineral 600ml | 1 | 5000 | 5000 | | Sapto |

---

## Troubleshooting

**Bot tidak merespons**
- Pastikan `TELEGRAM_TOKEN` benar
- Pastikan bot sudah dijalankan (`python bot.py`)

**Error Google Sheets**
- Pastikan `credentials.json` ada di folder yang sama dengan `bot.py`
- Pastikan spreadsheet sudah di-share ke email service account
- Pastikan Google Sheets API & Drive API sudah diaktifkan

**Error Gemini**
- Pastikan `GEMINI_API_KEY` benar
- Cek kuota di https://aistudio.google.com
