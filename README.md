# Mechanical Engineering Career Bootcamp 2026
### Jurusan Teknik Mesin — Politeknik Negeri Ujung Pandang (PNUP)

> **Website Pendaftaran, E-Ticket QR Code, dan Panel Admin Manajemen Peserta & Absensi**  
> Dibangun mandiri (*zero-dependency*), database internal SQLite otomatis, tanpa XAMPP / Laragon / Supabase.

---

## 📌 Ringkasan Proyek

Website ini dirancang khusus untuk kegiatan **Mechanical Engineering Career Bootcamp 2026**:  
*Pembekalan Karier & Kesiapan Dunia Kerja Untuk Alumni Baru dan Calon Alumni Jurusan Teknik Mesin Politeknik Negeri Ujung Pandang*.

### Keunggulan Utama:
1. **Mandiri & Zero-Dependency**: Hanya membutuhkan instalasi Python 3 bawaan. Tidak perlu web server pihak ketiga (Apache/Nginx/XAMPP/Laragon) maupun layanan cloud eksternal (Supabase/Firebase/MySQL).
2. **Database SQLite Otomatis**: Berkas database tersimpan secara lokal dan otomatis dibuat di `data/bootcamp.db` saat aplikasi pertama kali dijalankan.
3. **Sistem Desain Resmi (CampusKit via DESIGN.md & MCP)**: Mengimplementasikan sistem desain **CampusKit** dari [designmd.ai/chef/campuskit](https://designmd.ai/chef/campuskit) dengan palet marun & emas institusional, tipografi resmi (*Vollkorn* serif & *DM Sans*), dan logo Politeknik Negeri Ujung Pandang.
4. **E-Ticket & Absensi QR Code**:
   - Peserta memperoleh tiket digital dengan QR Code unik (`MEB26.<kode>`).
   - Tiket dapat **diunduh sebagai gambar PNG** beresolusi tinggi langsung dari browser atau dicetak.
   - Panitia dapat melakukan absensi secara instan melalui pemindaian kamera (laptop/smartphone), unggah foto QR, atau menggunakan alat pemindai barcode USB.
5. **Panel Admin Lengkap**:
   - Dasbor analitik pendaftar dan kehadiran real-time.
   - Manajemen peserta (pencarian instan, filter status, filter kehadiran, tambah, ubah, hapus).
   - Ekspor data pendaftar ke **CSV** (dengan proteksi formula Excel injection).
   - Pencadangan (*backup*) berkas database SQLite `.db` langsung dari antarmuka web.
   - Pengaturan kuota, tanggal acara, narahubung, dan sakelar buka/tutup pendaftaran.

---

## 🚀 Panduan Memulai Cepat

### Cara 1: Menggunakan Launcher Windows (Paling Mudah)
Cukup **klik ganda** berkas `start.bat` pada folder proyek. Server akan aktif otomatis.

### Cara 2: Melalui Terminal / Command Prompt
Buka terminal di folder proyek (`d:\acad\SMT7\bootcamp`), lalu jalankan:

```bash
python server.py
```

Server akan berjalan pada:
- **Halaman Pendaftaran Publik**: [http://localhost:8000/](http://localhost:8000/)
- **Cek / Unduh Tiket Peserta**: [http://localhost:8000/ticket](http://localhost:8000/ticket)
- **Panel Masuk Panitia**: [http://localhost:8000/admin](http://localhost:8000/admin)
- **Akses Jaringan LAN**: `http://<IP-Komputer-Anda>:8000/` (otomatis ditampilkan di konsol server).

---

## 🔑 Akun Masuk Panitia (Default)

Database awal telah disiapkan dengan akun admin default:

| Keterangan | Nilai |
|---|---|
| **URL Login** | [http://localhost:8000/admin](http://localhost:8000/admin) |
| **Username** | `admin` |
| **Password** | `PNUPmesin2026!` |

> 💡 **Tips Keamanan**: Anda dapat mengubah kata sandi kapan saja melalui menu **Pengaturan → Keamanan & Data → Ganti Kata Sandi** di panel admin.

Jika lupa kata sandi, reset melalui terminal dengan perintah:
```bash
python server.py --reset-admin admin
```

---

## 🛠️ Perintah CLI & Opsi Server

Server `server.py` dilengkapi berbagai opsi bawaan:

```bash
# Mengganti port (contoh port 8080)
python server.py --port 8080

# Mengisi 40 peserta demo untuk simulasi pengujian
python server.py --seed-demo 40

# Menghapus semua peserta demo
python server.py --purge-demo

# Menjalankan dengan HTTPS (diperlukan jika pemindai kamera diakses lewat jaringan LAN HP)
python server.py --cert cert.pem --key key.pem
```

---

## 🎨 Integrasi Design System (designmd MCP & tokens)

Sistem desain diunduh dari [designmd.ai/chef/campuskit](https://designmd.ai/chef/campuskit) dan dikompilasi oleh modul `tools/designmd.py`:

```
tools/designmd.py
 ├── fetch  : Mengunduh spesifikasi terbaru ke design/DESIGN.md
 ├── tokens : Mengompilasi DESIGN.md menjadi variabel CSS di static/css/tokens.css
 ├── sync   : Melakukan fetch + tokens secara berurutan
 └── mcp    : Berjalan sebagai server MCP (Model Context Protocol via stdio)
```

### Menjalankan Sinkronisasi Desain:
```bash
python tools/designmd.py sync chef/campuskit
```

### Penggunaan sebagai MCP Server:
Berkas `mcp_config.json` telah disediakan di root proyek. AI IDE (Antigravity/Cursor/Claude Code) dapat langsung membaca tool `designmd_fetch`, `designmd_build_tokens`, dan `designmd_inspect`.

---

## 📁 Struktur Berkas

```
bootcamp/
├── start.bat                # Shortcut peluncur cepat di Windows
├── server.py                # Server HTTP, API REST, auth & SQLite backend
├── mcp_config.json          # Konfigurasi Model Context Protocol (MCP)
├── README.md                # Dokumentasi lengkap
│
├── data/
│   └── bootcamp.db          # Database SQLite lokal (otomatis dibuat)
│
├── design/
│   └── DESIGN.md            # Salinan spesifikasi CampusKit design system
│
├── tools/
│   └── designmd.py          # Tool downloader, compiler tokens, & MCP stdio
│
└── static/                  # Berkas statis antarmuka web
    ├── index.html           # Landing page pendaftaran publik & lookup tiket
    ├── ticket.html          # Halaman e-ticket peserta & canvas generator PNG
    ├── admin.html           # Panel admin SPA (Dashboard, Peserta, Absensi, Pengaturan)
    ├── css/
    │   ├── tokens.css       # Design tokens (warna, font, spacing, radius, shadow)
    │   ├── base.css         # Komponen tombol, kartu, input, chip, tabel, modal
    │   ├── site.css         # Tata letak landing page & e-ticket
    │   └── admin.css        # Tata letak dashboard & scanner absensi
    ├── js/
    │   ├── common.js        # Utilitas bersama (API fetch, icons, format WITA, modal)
    │   ├── site.js          # Logika pendaftaran & cek tiket
    │   ├── ticket.js        # Render tiket, generate PNG, cetak
    │   └── admin.js         # Dasbor analitik, manajemen tabel, scanner QR
    ├── fonts/               # Self-hosted woff2 (Vollkorn, DM Sans, Source Code Pro)
    ├── img/
    │   ├── logo-pnup.png    # Logo resmi Politeknik Negeri Ujung Pandang
    │   └── gears.svg        # Aksen grafis teknik mesin
    └── vendor/
        ├── qrcode.js        # Generator QR Code mandiri (offline)
        └── jsQR.js          # Pemindai dan decoder QR Code (offline)
```

---

## 📱 Panduan Alur Absensi QR Panitia

1. Buka [http://localhost:8000/admin](http://localhost:8000/admin) lalu masuk menggunakan akun panitia.
2. Klik menu **Absensi QR**.
3. Pilih metode absensi sesuai kondisi di lapangan:
   - **Kamera Langsung**: Klik tombol **Mulai Pindai** dan arahkan kamera ke QR Code peserta.
   - **Unggah / Foto QR**: Klik **Foto QR** untuk mengambil gambar menggunakan kamera ponsel jika browser membatasi WebRTC video stream.
   - **Barcode Scanner USB / Manual**: Klik kolom input, arahkan scanner USB ke tiket peserta (atau ketik NIM / Nomor Registrasi seperti `MEB26-0001` lalu tekan Enter).
4. Sistem otomatis memvalidasi tiket:
   - Suara nada ganda + bingkai hijau: **Check-in Berhasil**.
   - Suara nada peringatan + bingkai kuning: **Sudah Hadir Sebelumnya** (menampilkan waktu dan nama petugas).
   - Suara nada kesalahan + bingkai merah: **Kode Tidak Ditemukan**.

---

*Dikembangkan untuk Jurusan Teknik Mesin, Politeknik Negeri Ujung Pandang (PNUP) — 2026.*
