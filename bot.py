"""
Finance Tracker Telegram Bot
- Parsing teks transaksi menggunakan Gemini API
- Menyimpan ke Google Sheets
- Fitur utang/piutang dengan reminder otomatis
"""

import os
import json
import logging
import uuid
from datetime import datetime, date, timedelta
from dotenv import load_dotenv

import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, JobQueue
)

from pytz import timezone

load_dotenv()

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Konfigurasi ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY")
SPREADSHEET_ID   = os.getenv("SPREADSHEET_ID")
SHEET_NAME       = os.getenv("SHEET_NAME", "Transaksi")
SHEET_DEBT       = os.getenv("SHEET_DEBT", "Utang-Piutang")
SHEET_BUDGET     = os.getenv("SHEET_BUDGET", "Budget")
SHEET_KATEGORI   = os.getenv("SHEET_KATEGORI", "Kategori")
BUDGET_WARNING   = 0.80
ALLOWED_USER_IDS = os.getenv("ALLOWED_USER_IDS", "")
# Chat ID untuk reminder — isi dengan Telegram user ID Anda
REMINDER_CHAT_ID = os.getenv("REMINDER_CHAT_ID", "")
WIB = timezone("Asia/Jakarta")

allowed_ids = set(
    int(x.strip()) for x in ALLOWED_USER_IDS.split(",") if x.strip()
)

# ── Gemini setup ──────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-3.1-flash-lite")

# ── Google Sheets setup ───────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
gc    = gspread.authorize(creds)

# ── Prompt: transaksi biasa ───────────────────────────────────────────────────
def get_parse_prompt(text: str) -> str:
    kategori = ", ".join(get_kategori_list())
    return f"""
Anggap kamu adalah asisten pencatat keuangan pribadi saya. Ekstrak informasi transaksi dari teks berikut dan kembalikan HANYA JSON array (tanpa markdown, tanpa penjelasan).

Teks: "{text}"

Format JSON yang harus dikembalikan (selalu array, meski hanya 1 item):
[
  {{
    "tipe": "pengeluaran" atau "pemasukan",
    "deskripsi": "nama barang/jasa yang singkat dan jelas",
    "kategori": "salah satu dari: {kategori}",
    "qty": angka (jumlah unit, default 1),
    "harga_satuan": angka (harga per unit dalam rupiah),
    "total": angka (qty x harga_satuan),
    "catatan": "informasi tambahan jika ada, atau kosong"
  }}
]

Aturan:
- Selalu kembalikan array JSON, bahkan untuk 1 transaksi
- Jika ada beberapa item dalam satu pesan, buat entry terpisah untuk masing-masing
- Harga selalu dalam Rupiah (tanpa simbol). "rb" = ribu, "jt" = juta
- "dibagi", "split", "patungan" BUKAN hutang/piutang kecuali ada nama orang yang disebutkan
- Jika tidak ada nama orang spesifik, hitung harga dibagi sesuai angka pembagi
- Jika teks tidak jelas / bukan transaksi keuangan biasa, kembalikan [{{"error": "bukan transaksi"}}]
- Jika teks adalah hutang/piutang, kembalikan [{{"error": "bukan transaksi"}}]
- Tipe "pemasukan" jika ada kata: gaji, terima, dapat, masuk, income, transfer masuk, transfer dari
- Tipe "pengeluaran" untuk semua pembelian/pembayaran
- lunas: true HANYA jika ada kata lunas/lunasin/lunasi
"""

# ── Prompt: utang piutang ────────────────────────────────────────────────────
DEBT_PARSE_PROMPT = """
Anggap kamu adalah asisten pencatat utang piutang pribadi saya. Ekstrak informasi dari teks berikut dan kembalikan HANYA JSON array (tanpa markdown, tanpa penjelasan). Selalu array meski hanya 1 item.

Teks: "{text}"
Tanggal hari ini: {today}

Kembalikan salah satu format berikut:

1. Jika ini adalah PENCATATAN utang/PIUTANG BARU:
{{
  "aksi": "baru",
  "tipe": "utang" atau "piutang",
  "nama": "nama orang",
  "jumlah": angka dalam rupiah,
  "keterangan": "keperluan/alasan pinjam",
  "jatuh_tempo": "YYYY-MM-DD" (jika disebutkan, hitung dari tanggal hari ini. Contoh: '3 hari lagi' = hitung dari hari ini. Jika tidak disebutkan, null)
}}

2. Jika ini adalah PEMBAYARAN SEBAGIAN atau PELUNASAN:
{{
  "aksi": "bayar",
  "tipe": "utang" atau "piutang",
  "nama": "nama orang",
  "jumlah_bayar": angka dalam rupiah WAJIB DIISI jika bukan pelunasan penuh. Contoh: "bayar 500rb" = 500000. Jangan pernah isi 0 kecuali memang pelunasan penuh,
  "lunas": true HANYA jika ada kata lunas/lunasin/lunasi. Untuk bayar sebagian, lunas = false
}}

Aturan:
- Selalu kembalikan array, bahkan untuk 1 item
- Jika ada beberapa utang/piutang dalam satu pesan, buat entry terpisah untuk masing-masing
- "utang" = saya yang berutang ke orang lain
- "piutang" = orang lain yang berutang ke saya
- "rb" = ribu, "jt" = juta
- Jika teks tidak berkaitan dengan utang/piutang, kembalikan {{"error": "ini mah bukan utang piutang"}}
- Kata kunci utang baru: pinjam, utang, minta talang, ngutang, minjem, pinjem, minta bayarin
- Kata kunci piutang baru: minjemin, kasih pinjam, talangin, piutang, bayarin, ngasih, ngebayarin
- Kata kunci bayar: bayar, cicil, nyicil, lunasin, lunasi, transfer balik, balikin
"""

# ── Helper: clean JSON response ───────────────────────────────────────────────
def clean_json(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()

# ── Helper: parse transaksi biasa ─────────────────────────────────────────────
def parse_transaction(text: str):
    try:
        response = model.generate_content(get_parse_prompt(text))
        data = json.loads(clean_json(response.text))
        if isinstance(data, dict):
            data = [data]
        if data and "error" in data[0]:
            return None
        return data
    except Exception as e:
        logger.error(f"Gemini parse error: {e}")
        return None

# ── Helper: parse utang piutang ──────────────────────────────────────────────
def parse_debt(text: str):
    try:
        prompt = DEBT_PARSE_PROMPT.format(
            text=text,
            today=datetime.now(WIB).date().strftime("%Y-%m-%d")
        )
        response = model.generate_content(prompt)
        data = json.loads(clean_json(response.text))
        # Normalisasi ke list
        if isinstance(data, dict):
            data = [data]
        if data and "error" in data[0]:
            return None
        return data
    except Exception as e:
        logger.error(f"Gemini debt parse error: {e}")
        return None

# ── Helper: simpan transaksi ke sheet ────────────────────────────────────────
def save_to_sheet(user: str, data: dict) -> bool:
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        now   = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
        row   = [
            now,
            data.get("tipe", ""),
            data.get("kategori", ""),
            data.get("deskripsi", ""),
            data.get("qty", 1),
            data.get("harga_satuan", 0),
            data.get("total", 0),
            data.get("catatan", ""),
            user,
        ]
        logger.info(f"Data yang akan disimpan: {row}")
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        logger.error(f"Sheets error: {e}")
        return False

# ── Helper: ambil sheet utang piutang ───────────────────────────────────────
def get_debt_sheet():
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_DEBT)

# ── Helper: simpan utang/piutang baru ───────────────────────────────────────
def save_debt(user: str, data: dict) -> str:
    """Simpan utang/piutang baru, return ID unik."""
    try:
        sheet    = get_debt_sheet()
        debt_id  = str(uuid.uuid4())[:8].upper()
        now      = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
        jumlah   = data.get("jumlah", 0)
        row = [
            debt_id,
            now,
            data.get("tipe", ""),
            data.get("nama", ""),
            data.get("keterangan", ""),
            jumlah,   # Total Awal
            0,        # Total Dibayar
            jumlah,   # Sisa
            data.get("jatuh_tempo") or "",
            "belum_lunas",
            user,
        ]
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return debt_id
    except Exception as e:
        logger.error(f"Save debt error: {e}")
        return ""

# ── Helper: proses pembayaran utang/piutang ──────────────────────────────────
def process_payment(data: dict) -> dict | None:
    try:
        sheet  = get_debt_sheet()
        rows   = sheet.get_all_values()
        header = rows[0]
        idx    = {h: i for i, h in enumerate(header)}

        nama_cari    = data.get("nama", "").lower()
        tipe_cari    = data.get("tipe", "")
        jumlah_bayar = data.get("jumlah_bayar", 0)
        lunas_flag   = data.get("lunas", False)

        # Kumpulkan semua baris yang cocok (belum lunas, FIFO = urut dari atas)
        target_rows = []
        for i, row in enumerate(rows[1:], start=2):
            if (row[idx["Nama"]].lower() == nama_cari and
                    row[idx["Tipe"]] == tipe_cari and
                    row[idx["Status"]] == "belum_lunas"):
                target_rows.append((i, row))

        if not target_rows:
            return None

        # Hitung total sisa semua utang orang ini
        total_sisa = sum(
            float(str(r[idx["Sisa"]]).replace(",", "") or 0)
            for _, r in target_rows
        )

        # Tentukan berapa yang dibayar
        if lunas_flag:
            sisa_pembayaran = total_sisa
        elif jumlah_bayar == 0:
            # jumlah_bayar 0 tapi bukan lunas = Gemini gagal parse angka
            return None
        else:
            sisa_pembayaran = min(jumlah_bayar, total_sisa)

        total_dibayar_sesi = sisa_pembayaran
        updated_count      = 0

        # Kurangi FIFO — dari baris paling lama dulu
        for row_idx, row in target_rows:
            if sisa_pembayaran <= 0:
                break

            sisa_baris    = float(str(row[idx["Sisa"]]).replace(",", "") or 0)
            sudah_dibayar = float(str(row[idx["Total Dibayar"]]).replace(",", "") or 0)

            if sisa_pembayaran >= sisa_baris:
                # Baris ini lunas
                bayar_baris        = sisa_baris
                sisa_pembayaran   -= sisa_baris
                sisa_baru          = 0
                status_baru        = "lunas"
            else:
                # Baris ini sebagian
                bayar_baris        = sisa_pembayaran
                sisa_pembayaran    = 0
                sisa_baru          = sisa_baris - bayar_baris
                status_baru        = "belum_lunas"

            dibayar_baru = sudah_dibayar + bayar_baris

            sheet.update_cell(row_idx, idx["Total Dibayar"] + 1, dibayar_baru)
            sheet.update_cell(row_idx, idx["Sisa"] + 1,          sisa_baru)
            sheet.update_cell(row_idx, idx["Status"] + 1,        status_baru)
            updated_count += 1

        sisa_akhir = total_sisa - total_dibayar_sesi

        return {
            "nama":          data.get("nama"),
            "tipe":          tipe_cari,
            "bayar":         total_dibayar_sesi,
            "sisa":          sisa_akhir,
            "status":        "lunas" if sisa_akhir <= 0 else "belum_lunas",
            "total_awal":    total_sisa,
            "updated_count": updated_count,
        }

    except Exception as e:
        logger.error(f"Process payment error: {e}")
        return None

# ── Helper: setup headers ─────────────────────────────────────────────────────
def ensure_headers():
    try:
        wb = gc.open_by_key(SPREADSHEET_ID)

        # Sheet transaksi
        try:
            sh = wb.worksheet(SHEET_NAME)
        except gspread.WorksheetNotFound:
            sh = wb.add_worksheet(SHEET_NAME, rows=1000, cols=10)
        if not sh.row_values(1) or sh.cell(1, 1).value != "Tanggal":
            sh.insert_row(
                ["Tanggal", "Tipe", "Kategori", "Deskripsi",
                 "Qty", "Harga Satuan", "Total", "Catatan", "User"], 1
            )

        # Sheet utang piutang
        try:
            dh = wb.worksheet(SHEET_DEBT)
        except gspread.WorksheetNotFound:
            dh = wb.add_worksheet(SHEET_DEBT, rows=1000, cols=12)
        if not dh.row_values(1) or dh.cell(1, 1).value != "ID":
            dh.insert_row(
                ["ID", "Tanggal", "Tipe", "Nama", "Keterangan",
                 "Total Awal", "Total Dibayar", "Sisa",
                 "Jatuh Tempo", "Status", "User"], 1
            )
        
        # Sheet budget
        try:
            bh = wb.worksheet(SHEET_BUDGET)
        except gspread.WorksheetNotFound:
            bh = wb.add_worksheet(SHEET_BUDGET, rows=20, cols=2)
        if not bh.row_values(1) or bh.cell(1, 1).value != "Kategori":
            bh.insert_row(["Kategori", "Budget"], 1)

        # Sheet kategori
        try:
            kh = wb.worksheet(SHEET_KATEGORI)
        except gspread.WorksheetNotFound:
            kh = wb.add_worksheet(SHEET_KATEGORI, rows=50, cols=1)
            # Isi kategori default
            default_kategori = [
                ["Kategori"],
                ["Makanan & Minuman"], ["Transportasi"], ["Belanja"],
                ["Kesehatan"], ["Hiburan"], ["Tagihan"], ["Gaji"],
                ["Investasi"], ["Lainnya"]
            ]
            kh.update("A1", default_kategori)
    except Exception as e:
        logger.warning(f"Header setup error: {e}")

# ── Helper: ambil sheet budget ────────────────────────────────────────────────
def get_budget_sheet():
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_BUDGET)

# ── Helper: ambil sheet kategori ─────────────────────────────────────────────
def get_kategori_sheet():
    return gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_KATEGORI)

# ── Helper: ambil daftar kategori ────────────────────────────────────────────
def get_kategori_list() -> list:
    """Return list kategori dari sheet Kategori."""
    try:
        rows = get_kategori_sheet().get_all_records()
        return [r["Kategori"] for r in rows if r.get("Kategori")]
    except Exception as e:
        logger.error(f"Get kategori error: {e}")
        # Fallback ke default kalau sheet bermasalah
        return [
            "Makanan & Minuman", "Transportasi", "Belanja",
            "Kesehatan", "Hiburan", "Tagihan", "Gaji",
            "Investasi", "Lainnya"
        ]

# ── Helper: ambil budget per kategori ────────────────────────────────────────
def get_budgets() -> dict:
    """Return {kategori: budget} dari sheet Budget."""
    try:
        rows = get_budget_sheet().get_all_records()
        return {r["Kategori"]: float(str(r["Budget"]).replace(",", "") or 0) for r in rows}
    except Exception as e:
        logger.error(f"Get budget error: {e}")
        return {}

# ── Helper: hitung pengeluaran bulan ini per kategori ────────────────────────
def get_spending_this_month() -> dict:
    """Return {kategori: total_pengeluaran} bulan berjalan."""
    try:
        sheet  = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        rows   = sheet.get_all_records()
        bulan  = datetime.now(WIB).strftime("%Y-%m")
        result = {}
        for row in rows:
            if not str(row.get("Tanggal", "")).startswith(bulan):
                continue
            if row.get("Tipe") != "pengeluaran":
                continue
            kat   = row.get("Kategori", "Lainnya")
            total = float(str(row.get("Total", 0)).replace(",", "") or 0)
            result[kat] = result.get(kat, 0) + total
        return result
    except Exception as e:
        logger.error(f"Get spending error: {e}")
        return {}

# ── Helper: cek warning budget untuk satu kategori ───────────────────────────
def check_budget_warning(kategori: str) -> str | None:
    """
    Return pesan warning jika pengeluaran kategori >= 80% budget.
    Return None jika aman atau tidak ada budget untuk kategori ini.
    """
    budgets  = get_budgets()
    spending = get_spending_this_month()

    if kategori not in budgets:
        return None

    budget  = budgets[kategori]
    spent   = spending.get(kategori, 0)
    persen  = spent / budget if budget > 0 else 0

    if persen >= 1.0:
        return (
            f"🚨 *Budget {kategori} UDAH ABIS!*\n"
            f"Udah kepake: Rp{spent:,.0f} / Rp{budget:,.0f} ({persen*100:.0f}%)"
        )
    elif persen >= BUDGET_WARNING:
        sisa = budget - spent
        return (
            f"⚠️ *Budget {kategori} HAMPIR ABIS!*\n"
            f"Udah kepake: Rp{spent:,.0f} / Rp{budget:,.0f} ({persen*100:.0f}%)\n"
            f"Sisa: Rp{sisa:,.0f}"
        )
    return None

# ── Debt reminder job ──────────────────────────────────────────────────────────────
async def debt_reminders(context: ContextTypes.DEFAULT_TYPE):
    if not REMINDER_CHAT_ID:
        return
    try:
        sheet = get_debt_sheet()
        rows  = sheet.get_all_records()
        today = datetime.now(WIB).date()

        for row in rows:
            if row.get("Status") != "belum_lunas":
                continue
            jt_str = str(row.get("Jatuh Tempo", "")).strip()
            if not jt_str:
                continue

            try:
                jt = datetime.strptime(jt_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            delta = (jt - today).days  # negatif = sudah lewat

            # Kirim reminder pada: H-1, H0, H+1, H+3, H+7
            if delta not in (-7, -3, -1, 0, 1):
                continue

            tipe  = row.get("Tipe", "")
            nama  = row.get("Nama", "")
            sisa  = row.get("Sisa", 0)
            emoji = "🔴" if tipe == "utang" else "🟡"

            if delta == 1:
                ket = "⏰ *Eh jatuh tempo lo besok!*"
            elif delta == 0:
                ket = "🚨 *Woiii jatuh tempo lo HARI INI!*"
            elif delta == -1:
                ket = "⚠️ *Woilah cik udah lewat 1 hari*"
            elif delta == -3:
                ket = "⚠️ *Woilah cik udah lewat 3 hari*"
            else:
                ket = "🆘 *Woilah cik udah lewat 7 hari!*"

            arah = "ke" if tipe == "utang" else "dari"
            msg  = (
                f"{emoji} *Reminder {tipe.capitalize()}*\n\n"
                f"{ket}\n"
                f"👤 {arah.capitalize()} : {nama}\n"
                f"💰 Sisa: Rp{float(sisa):,.0f}\n"
                f"📅 Jatuh tempo: {jt_str}\n\n"
                f"Lo ketik `bayar {tipe} {nama} [jumlah]` klo ada update ya!"
            )
            await context.bot.send_message(
                chat_id=REMINDER_CHAT_ID,
                text=msg,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error(f"Debt reminder error: {e}")

# ── Weekly report job ─────────────────────────────────────────────────────────
async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    if not REMINDER_CHAT_ID:
        return
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        rows  = sheet.get_all_records()

        # Rentang minggu lalu (Senin - Minggu)
        today     = datetime.now(WIB).date()
        last_mon  = today - timedelta(days=7)
        last_sun  = today - timedelta(days=1)

        pemasukan   = 0
        pengeluaran = 0
        count       = 0
        per_kategori = {}

        for row in rows:
            tgl_str = str(row.get("Tanggal", ""))[:10]
            try:
                tgl = datetime.strptime(tgl_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (last_mon <= tgl <= last_sun):
                continue

            count += 1
            total = float(str(row.get("Total", 0)).replace(",", "") or 0)
            tipe  = row.get("Tipe", "")

            if tipe == "pemasukan":
                pemasukan += total
            else:
                pengeluaran += total
                kat = row.get("Kategori", "Lainnya")
                per_kategori[kat] = per_kategori.get(kat, 0) + total

        periode = f"{last_mon.strftime('%d %b')} - {last_sun.strftime('%d %b %Y')}"
        selisih = pemasukan - pengeluaran
        emoji   = "🟢" if selisih >= 0 else "🔴"

        msg = (
            f"📊 *Laporan Mingguan*\n"
            f"📅 {periode}\n"
            f"──────────────────\n\n"
            f"📥 Pemasukan: Rp{pemasukan:,.0f}\n"
            f"📤 Pengeluaran: Rp{pengeluaran:,.0f}\n"
            f"{emoji} Selisih: Rp{selisih:,.0f}\n\n"
        )

        if per_kategori:
            msg += "📋 *Pengeluaran per Kategori:*\n"
            kategori_emoji = {
                "Makanan & Minuman": "🍜",
                "Transportasi": "🚗",
                "Belanja": "🛍️",
                "Kesehatan": "🏥",
                "Hiburan": "🎮",
                "Tagihan": "📱",
                "Investasi": "📈",
                "Lainnya": "📦",
            }
            for kat, total in sorted(per_kategori.items(), key=lambda x: x[1], reverse=True):
                persen = total / pengeluaran * 100 if pengeluaran > 0 else 0
                emo    = kategori_emoji.get(kat, "•")
                msg   += f"  {emo} {kat}: Rp{total:,.0f} ({persen:.0f}%)\n"

        msg += f"\n📝 Total transaksi: {count}"

        await context.bot.send_message(
            chat_id=REMINDER_CHAT_ID,
            text=msg,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Weekly report error: {e}")

# ── Command: /laporan ───────────────────────────────────────────────────────
async def cmd_laporan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_weekly_report(context)

# ── Command: /start ───────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "👋 *Hayyy! Gue bot pencatat keuangan lo, soalnya lo kan pelupa AWKAWOAWKOAK*\n\n"
        "Lo tulis natural aja kayak ngechat orang ya...\n\n"
        "📌 *Transaksi biasa:*\n"
        "• `Beli nasi goreng 15000`\n"
        "• `Beli air mineral 600ml 1 buah 5000`\n"
        "• `Gaji bulan Juni 10000000`\n\n"
        "💸 *Utang/Piutang:*\n"
        "• `Utang ke Nadin Amizah 15000 buat makan, jatuh tempo 10 Juni`\n"
        "• `Minjemin Bernadya 1000000, jatuh tempo 3 hari lagi`\n"
        "• `Bayar utang ke Rusdi 7000`\n"
        "• `Lunasin utang ke Ilham`\n"
        "• `Andi udah lunasin utangnya`\n\n"
        "📋 *Perintah:*\n"
        "/ringkasan - Ringkasan bulan ini\n"
        "/utang - Daftar utang belum lunas\n"
        "/piutang - Daftar piutang belum lunas\n"
        "/bantuan - Panduan lengkap\n"
        "/utangfinal - Utang piutang final per orang\n"
        "/setbudget - Set budget bulanan per kategori\n"
        "/cekbudget - Cek status budget bulanan\n"
        "/laporan - Laporan mingguan (otomatis setiap Senin pagi)\n"
        "/kategori - Liat daftar kategori transaksi\n"
        "/tambahkategori - Tambah kategori baru\n"
        "/hapuskategori - Hapus kategori"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Command: /bantuan ─────────────────────────────────────────────────────────
async def cmd_bantuan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 *Panduan Lengkap*\n\n"
        "*Transaksi Biasa:*\n"
        "Lo tulis natural aja kayak ngechat orang, contoh:\n"
        "• `Beli bensin 50rb`\n"
        "• `Makan siang 25000`\n"
        "• `Beli kopi 15rb, gorengan 5rb, aqua 3rb`\n\n"
        "*Utang (Lo yang pinjam):*\n"
        "• `Utang ke Windah Basudara 50000 buat bensin`\n"
        "• `Pinjam ke Windahkus 100rb jatuh tempo 7 hari lagi`\n"
        "• `Bayar utang ke Tenxi 25000`\n"
        "• `Lunasin utang ke Naykilla`\n\n"
        "*Piutang (Lo yang minjemin):*\n"
        "• `Minjemin Baskara 100000`\n"
        "• `Talangin Sal Priadi 50rb jatuh tempo 2 minggu lagi`\n"
        "• `Tulus udah bayar 50000`\n"
        "• `Prabowo udah lunasin utangnya`\n\n"
        "*Reminder bakal otomatis dikirim:*\n"
        "H-1 sebelum jatuh tempo\n"
        "Tepat di hari jatuh tempo\n"
        "H+1, H+3, H+7 jika belum lunas"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Command: /ringkasan ───────────────────────────────────────────────────────
async def cmd_ringkasan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sheet = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
        rows  = sheet.get_all_records()
        bulan = datetime.now(WIB).strftime("%Y-%m")

        pemasukan = pengeluaran = count = 0
        for row in rows:
            if not str(row.get("Tanggal", "")).startswith(bulan):
                continue
            count += 1
            total = float(str(row.get("Total", 0)).replace(",", "") or 0)
            if row.get("Tipe") == "pemasukan":
                pemasukan += total
            else:
                pengeluaran += total

        saldo = pemasukan - pengeluaran
        msg = (
            f"📊 *Ringkasan {datetime.now(WIB).strftime('%B %Y')}*\n\n"
            f"📥 Pemasukan: Rp{pemasukan:,.0f}\n"
            f"📤 Pengeluaran: Rp{pengeluaran:,.0f}\n"
            f"{'🟢' if saldo >= 0 else '🔴'} Saldo: Rp{saldo:,.0f}\n\n"
            f"📝 Total transaksi: {count}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ringkasan error: {e}")
        await update.message.reply_text("❌ Gagal ngambil ringkasan. Coba lo liat log-nya!")

# ── Command: /utang ──────────────────────────────────────────────────────────
async def cmd_utang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_debt_list(update, "utang")

# ── Command: /piutang ─────────────────────────────────────────────────────────
async def cmd_piutang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show_debt_list(update, "piutang")

# ── Command: /utangfinal ─────────────────────────
async def cmd_utangfinal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sheet = get_debt_sheet()
        rows  = sheet.get_all_records()

        # Kumpulkan data per nama
        rekap = {}  # {nama: {utang: total, piutang: total}}
        for row in rows:
            if row.get("Status") != "belum_lunas":
                continue
            nama  = row.get("Nama", "").strip()
            tipe  = row.get("Tipe", "")
            sisa  = float(str(row.get("Sisa", 0)).replace(",", "") or 0)
            if not nama or sisa <= 0:
                continue

            if nama not in rekap:
                rekap[nama] = {"utang": 0, "piutang": 0}
            rekap[nama][tipe] += sisa

        if not rekap:
            await update.message.reply_text("✅ Gaada utang-piutang yang belum lunas!")
            return

        msg = "📋 *Rekap Utang-Piutang per Orang*\n"
        msg += "──────────────────\n\n"

        for nama, data in sorted(rekap.items()):
            utang  = data["utang"]   # saya yang harus bayar ke dia
            piutang = data["piutang"]  # dia yang harus bayar ke saya
            msg += f"👤 *{nama}*\n"
            if utang > 0:
                msg += f"  🔴 Lo perlu bayar ke {nama}: Rp{utang:,.0f}\n"
            if piutang > 0:
                msg += f"  🟡 {nama} perlu bayar ke lo: Rp{piutang:,.0f}\n"

            # Net
            if utang > 0 and piutang > 0:
                net = piutang - utang
                if net > 0:
                    msg += f"  ✅ *Net: {nama} masih harus bayar ke lo Rp{net:,.0f}*\n"
                elif net < 0:
                    msg += f"  ⚠️ *Net: Lo masih harus bayar ke {nama} Rp{abs(net):,.0f}*\n"
                else:
                    msg += f"  🤝 *Net: Gaada Utang-Piutang!*\n"

            msg += "\n"

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Rekap error: {e}")
        await update.message.reply_text("❌ Gagal ngambil rekap. Coba lo liat log-nya!")

# ── Command: /setbudget ───────────────────────────────────────────────────────
async def cmd_setbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kategori_list = [
        "Makanan & Minuman", "Transportasi", "Belanja",
        "Kesehatan", "Hiburan", "Tagihan", "Investasi", "Lainnya"
    ]
    msg = (
        "💰 *Set Budget Bulanan*\n\n"
        "Lo kirim pesan:\n"
        "`/setbudget [kategori] [jumlah]`\n\n"
        "Contoh:\n"
        "`/setbudget Makanan & Minuman 1000000`\n"
        "`/setbudget Transportasi 500rb`\n\n"
        "Kategori yang ada:\n"
    )
    for k in kategori_list:
        msg += f"• {k}\n"
    
    # Jika ada argumen langsung proses
    if context.args:
        # Parse argumen: semua kecuali terakhir = kategori, terakhir = jumlah
        args      = context.args
        jumlah_str = args[-1].lower().replace("rb", "000").replace("jt", "000000")
        kategori  = " ".join(args[:-1])
        try:
            jumlah = float(jumlah_str.replace(",", ""))
        except ValueError:
            await update.message.reply_text("❌ Format jumlah ngga valid. Contoh: `500000` atau `500rb`", parse_mode="Markdown")
            return

        try:
            sheet = get_budget_sheet()
            rows  = sheet.get_all_values()
            # Cari apakah kategori sudah ada
            for i, row in enumerate(rows[1:], start=2):
                if row[0].lower() == kategori.lower():
                    sheet.update_cell(i, 2, jumlah)
                    await update.message.reply_text(
                        f"✅ Budget *{kategori}* di-update: Rp{jumlah:,.0f}/bulan",
                        parse_mode="Markdown"
                    )
                    return
            # Belum ada, tambah baris baru
            sheet.append_row([kategori, jumlah])
            await update.message.reply_text(
                f"✅ Budget *{kategori}* di-set: Rp{jumlah:,.0f}/bulan",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Set budget error: {e}")
            await update.message.reply_text("❌ Gagal nyimmpen budget. Coba lo liat log-nya!")
        return

    await update.message.reply_text(msg, parse_mode="Markdown")

# ── Command: /cekbudget ───────────────────────────────────────────────────────
async def cmd_cekbudget(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        budgets  = get_budgets()
        spending = get_spending_this_month()

        if not budgets:
            await update.message.reply_text(
                "Belum ada budget yang lo set.\n"
                "Pake `/setbudget [kategori] [jumlah]` buat set budget.",
                parse_mode="Markdown"
            )
            return

        msg = f"💰 *Budget Bulan {datetime.now(WIB).strftime('%B %Y')}*\n"
        msg += "──────────────────\n\n"

        for kat, budget in sorted(budgets.items()):
            spent  = spending.get(kat, 0)
            sisa   = budget - spent
            persen = spent / budget * 100 if budget > 0 else 0

            if persen >= 100:
                icon = "🚨"
            elif persen >= 80:
                icon = "⚠️"
            else:
                icon = "✅"

            # Progress bar
            filled = int(persen / 10)
            bar    = "█" * min(filled, 10) + "░" * (10 - min(filled, 10))

            msg += (
                f"{icon} *{kat}*\n"
                f"  `{bar}` {persen:.0f}%\n"
                f"  Kepake: Rp{spent:,.0f} / Rp{budget:,.0f}\n"
                f"  Sisa: Rp{max(sisa, 0):,.0f}\n\n"
            )

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Cek budget error: {e}")
        await update.message.reply_text("❌ Gagal ngambil data budget. Coba lo liat log-nya!")

# ── Command: /kategori ────────────────────────────────────────────────────────
async def cmd_kategori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        kategori = get_kategori_list()
        msg = "📋 *Daftar Kategori*\n\n"
        for i, k in enumerate(kategori, 1):
            msg += f"{i}. {k}\n"
        msg += (
            "\n_Gunakan:_\n"
            "`/tambahkategori [nama]` buat tambah kategori baru\n"
            "`/hapuskategori [nama]` buat hapus kategori"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Kategori error: {e}")
        await update.message.reply_text("❌ Gagal ngambil daftar kategori. Coba lo liat log-nya!")

# ── Command: /tambahkategori ──────────────────────────────────────────────────
async def cmd_tambahkategori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Format: `/tambahkategori [nama kategori]`\nContoh: `/tambahkategori Pendidikan`",
            parse_mode="Markdown"
        )
        return
    nama = " ".join(context.args).strip()
    try:
        sheet    = get_kategori_sheet()
        existing = get_kategori_list()
        if nama.lower() in [k.lower() for k in existing]:
            await update.message.reply_text(f"⚠️ Kategori *{nama}* udah ada mas", parse_mode="Markdown")
            return
        sheet.append_row([nama])
        await update.message.reply_text(f"✅ Kategori *{nama}* berhasil ditambahin bos!", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Tambah kategori error: {e}")
        await update.message.reply_text("❌ Gagal nambahin kategori. Coba lo liat log-nya!")

# ── Command: /hapuskategori ───────────────────────────────────────────────────
async def cmd_hapuskategori(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Format: `/hapuskategori [nama kategori]`\nContoh: `/hapuskategori Lainnya`",
            parse_mode="Markdown"
        )
        return
    nama = " ".join(context.args).strip()
    try:
        sheet = get_kategori_sheet()
        rows  = sheet.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row[0].lower() == nama.lower():
                sheet.delete_rows(i)
                await update.message.reply_text(f"✅ Kategori *{nama}* berhasil dihapus bos!", parse_mode="Markdown")
                return
        await update.message.reply_text(f"❌ Kategori *{nama}* ga ketemu", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Hapus kategori error: {e}")
        await update.message.reply_text("❌ Gagal ngapus kategori. Coba lo liat log-nya!")

async def _show_debt_list(update: Update, tipe: str):
    try:
        sheet = get_debt_sheet()
        rows  = sheet.get_all_records()
        aktif = [r for r in rows if r.get("Tipe") == tipe and r.get("Status") == "belum_lunas"]

        emoji = "🔴" if tipe == "utang" else "🟡"
        arah  = "ke" if tipe == "utang" else "dari"
        judul = "Utang Belum Lunas" if tipe == "utang" else "Piutang Belum Lunas"

        if not aktif:
            await update.message.reply_text(f"✅ Semua {tipe} udah pada lunas!")
            return

        total_sisa = 0
        msg = f"{emoji} *{judul}*\n\n"
        for r in aktif:
            sisa = float(str(r.get("Sisa", 0)).replace(",", "") or 0)
            total_sisa += sisa
            jt   = r.get("Jatuh Tempo", "") or "tidak diset"
            msg += (
                f"👤 {arah.capitalize()}: *{r.get('Nama')}*\n"
                f"   💰 Sisa: Rp{sisa:,.0f}\n"
                f"   📅 Jatuh tempo: {jt}\n"
                f"   📝 {r.get('Keterangan', '-')}\n\n"
            )
        msg += f"──────────────\n💸 *Total: Rp{total_sisa:,.0f}*"
        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Show debt error: {e}")
        await update.message.reply_text(f"❌ Gagal ngambil data {tipe}. Coba lo liat log-nya!")

# ── Handler: pesan biasa ──────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.full_name or str(user_id)
    text     = update.message.text.strip()

    if allowed_ids and user_id not in allowed_ids:
        await update.message.reply_text("⛔ Sorry ye! Tapi, lo ini siapa? Lo ga diizinin pake bot ini.")
        return

    await update.message.reply_text("⏳ Gue proses dulu...")

    # ── Coba parse sebagai utang/piutang dulu ────────────────────────────────
    debt_list = parse_debt(text)

    if debt_list:
        for debt_data in debt_list:
            aksi = debt_data.get("aksi")

            if aksi == "baru":
                debt_id = save_debt(username, debt_data)
                if debt_id:
                    tipe  = debt_data.get("tipe", "")
                    nama  = debt_data.get("nama", "")
                    jml   = debt_data.get("jumlah", 0)
                    jt    = debt_data.get("jatuh_tempo") or "tidak diset"
                    arah  = "ke" if tipe == "utang" else "dari"
                    emoji = "🔴" if tipe == "utang" else "🟡"
                    msg = (
                        f"{emoji} *{tipe.capitalize()} berhasil dicatet nih!*\n\n"
                        f"🆔 ID: `{debt_id}`\n"
                        f"👤 {arah.capitalize()}: {nama}\n"
                        f"💰 Jumlah: Rp{jml:,.0f}\n"
                        f"📝 {debt_data.get('keterangan', '-')}\n"
                        f"📅 Jatuh tempo: {jt}\n\n"
                        f"_Reminder otomatis bakal dikirim H-1, H0, H+1, H+3, H+7_"
                    )
                    await update.message.reply_text(msg, parse_mode="Markdown")
                else:
                    await update.message.reply_text("❌ Gagal nyimpen utang/piutang. Coba lo liat log-nya!")

            elif aksi == "bayar":
                result = process_payment(debt_data)
                if result:
                    tipe   = result["tipe"]
                    emoji  = "🔴" if tipe == "utang" else "🟡"
                    status = result["status"]
                    msg = (
                        f"{emoji} *Pembayaran {tipe} berhasil dicatet nih!*\n\n"
                        f"👤 {result['nama']}\n"
                        f"💸 Dibayar: Rp{result['bayar']:,.0f}\n"
                        f"💰 Sisa   : Rp{result['sisa']:,.0f}\n"
                        f"{'✅ *LUNAS!*' if status == 'lunas' else '⏳ *BELUM LUNAS*'}"
                    )
                    await update.message.reply_text(msg, parse_mode="Markdown")
                else:
                    await update.message.reply_text(
                        "❓ Gaada utang/piutang yang cocok.\n"
                        "Coba lo cek namanya dan pastiin statusnya belum lunas.\n"
                        "Pake /utang atau /piutang buat ngecek."
                    )
        return

    # ── Parse sebagai transaksi biasa ─────────────────────────────────────────
    transactions = parse_transaction(text)

    if not transactions:
        await update.message.reply_text(
            "❓ Gue gatau ini apaan.\n"
            "Coba lo tulis lebih jelas, contoh:\n"
            "• `Beli kopi 15000`\n"
            "• `Utang ke Budi 50000`\n"
            "• `/bantuan` untuk panduan lengkap",
            parse_mode="Markdown"
        )
        return

    berhasil = 0
    pesan_items = ""
    for item in transactions:
        if save_to_sheet(username, item):
            berhasil += 1
            emoji = "📥" if item["tipe"] == "pemasukan" else "📤"
            pesan_items += f"{emoji} {item['deskripsi']} - Rp{item.get('total', 0):,.0f}\n"

    if berhasil > 0:
        msg = f"✅ *{berhasil} transaksi berhasil dicatet nih!*\n\n{pesan_items}"
        await update.message.reply_text(msg, parse_mode="Markdown")

        # Cek budget warning untuk setiap transaksi pengeluaran
        warnings = []
        for item in transactions:
            if item.get("tipe") == "pengeluaran":
                warn = check_budget_warning(item.get("kategori", ""))
                if warn and warn not in warnings:
                    warnings.append(warn)
        if warnings:
            await update.message.reply_text(
                "\n\n".join(warnings),
                parse_mode="Markdown"
            )
    else:
        await update.message.reply_text("❌ Gagal nyimpen transaksi. Coba lo liat log-nya!")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ensure_headers()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("bantuan",   cmd_bantuan))
    app.add_handler(CommandHandler("ringkasan", cmd_ringkasan))
    app.add_handler(CommandHandler("utang",    cmd_utang))
    app.add_handler(CommandHandler("piutang",   cmd_piutang))
    app.add_handler(CommandHandler("utangfinal", cmd_utangfinal))
    app.add_handler(CommandHandler("setbudget",  cmd_setbudget))
    app.add_handler(CommandHandler("cekbudget",  cmd_cekbudget))
    app.add_handler(CommandHandler("laporan", cmd_laporan))
    app.add_handler(CommandHandler("kategori",        cmd_kategori))
    app.add_handler(CommandHandler("tambahkategori",  cmd_tambahkategori))
    app.add_handler(CommandHandler("hapuskategori",   cmd_hapuskategori))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Reminder utang-piutang job — cek setiap hari jam 11.00
    job_queue = app.job_queue
    job_queue.run_daily(
        debt_reminders,
        time=datetime.strptime("11:00", "%H:%M").time().replace(tzinfo=WIB),
        name="daily_reminder"
    )

    # Laporan mingguan — setiap Senin jam 05:00
    job_queue.run_daily(
        send_weekly_report,
        time=datetime.strptime("05:00", "%H:%M").time().replace(tzinfo=WIB),
        days=(0,),  # 0 = Senin
        name="weekly_report"
    )

    logger.info("Bot yg lo bikin lagi jalan...")
    app.run_polling()

if __name__ == "__main__":
    main()