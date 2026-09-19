# IDX Financial Data Pipeline

Pipeline pengambilan dan pemrosesan laporan keuangan emiten IDX (Bursa Efek Indonesia):

1. **Master data** — sinkronisasi sektor/industri dan jumlah saham beredar per ticker (via yfinance) → `emitens.csv`
2. **IDX download** — download file XBRL laporan keuangan per ticker/tahun/kuartal dari situs IDX → `XBRL/`
3. **XBRL parsing** — ekstraksi metrik keuangan dari file XBRL sesuai taxonomy mapping → `financial_reports.csv`

## Struktur folder

```
conten/
├── data/
│   ├── watchlist.csv          # daftar ticker yang diproses
│   ├── taxonomy.csv           # mapping istilah umum -> tag XBRL, per taxonomy_type
│   ├── emitens.csv            # dihasilkan oleh step Master data
│   ├── financial_reports.csv  # dihasilkan oleh step XBRL parsing
│   └── XBRL/{year}/{Q1,Q2,Q3}/{TICKER}_{year}_{period}.xbrl
├── model/
│   ├── master-data.py
│   ├── fetch_idx.py
│   └── arelle_loader.py
├── requirements.txt
└── run.py
```

## Instalasi

```bash
pip install -r requirements.txt
```

Isi `requirements.txt`:

```
cloudscraper
requests
beautifulsoup4
lxml
yfinance
```

## Menjalankan pipeline

Dari folder `conten`:

```bash
python run.py
```

Ini menjalankan ketiga step berurutan: Master data → IDX download → XBRL parsing.

## Skip module (step) tertentu

| Flag | Efek |
|---|---|
| `--skip-master` | Lewati step Master data (sync sektor & saham beredar) |
| `--skip-fetch` | Lewati step IDX download |
| `--skip-parse` | Lewati step XBRL parsing |

Bisa dikombinasikan bebas. Contoh:

```bash
# Cuma download + parse, tanpa sync master data
python run.py --skip-master

# Cuma parsing ulang XBRL yang sudah ada, tanpa download atau sync ulang
python run.py --skip-master --skip-fetch

# Cuma download, tanpa sync master data atau parsing
python run.py --skip-master --skip-parse
```

> Catatan: kalau `--skip-master` dipakai (dan `--skip-parse` tidak), `run.py` akan cek dulu apakah `data/emitens.csv` sudah ada — kalau belum, akan error minta jalankan step Master data dulu.

## Membatasi rentang tahun

Berlaku untuk step IDX download dan XBRL parsing (Master data tidak terpengaruh, tapi punya `--shares-start-year` sendiri kalau dipanggil langsung):

```bash
python run.py --start-year 2023 --end-year 2026
```

Default: `--start-year 2023`, `--end-year <tahun berjalan>`.

## Ganti lokasi data / watchlist

```bash
# Data dir custom (default: <folder run.py>/data)
python run.py --data-dir "D:\lokasi\lain\data"

# Watchlist custom (default: <data-dir>/watchlist.csv)
python run.py --watchlist "D:\lokasi\lain\watchlist_khusus.csv"
```

## Kombinasi lengkap

Semua flag bisa digabung:

```bash
python run.py --skip-master --start-year 2024 --end-year 2026 --data-dir "D:\data-idx"
```

## File input yang dibutuhkan

- `data/watchlist.csv` — wajib kolom `ticker`. Kolom lain (`priority`, `added_at`, `is_processed`) opsional dan didukung oleh `fetch_idx.py` untuk urutan proses dan skip ticker yang sudah selesai.
- `data/taxonomy.csv` — wajib kolom `taxonomy_type`, `common_term`, `xbrl_tag`. Kolom lain (`id`) diabaikan.

## Output

- `data/emitens.csv` — daftar issuer + taxonomy_type + saham beredar
- `data/XBRL/...` — file XBRL mentah per ticker/tahun/kuartal
- `data/financial_reports.csv` — hasil akhir: metrik keuangan per ticker/tahun/periode
- `data/failed_downloads.csv` — log ticker yang gagal didownload setelah retry (kalau ada)
