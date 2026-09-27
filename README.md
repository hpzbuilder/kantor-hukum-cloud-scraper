# Kantor Hukum Virtual — Cloud Auto-Harvest Scraper ⚖️

Sistem scraping, ekstraksi teks (PyMuPDF), dan pengkayaan metadata hukum Indonesia yang berjalan 100% otomatis di Cloud (GitHub Actions Scheduled Runner) secara gratis tanpa membebani komputer lokal.

## Arsitektur

1. **Jadwal Otomatis (Cron):** Berjalan berkala setiap 4 jam.
2. **Worker 1 (Regulasi Nasional):** Mengunduh regulasi prioritas tebal (`perpres`, `permen`, `inpres`) langsung dari JDIH Kemenkumham via HTTP Chunk Streaming Engine.
3. **Worker 2 (Repositori Akademik):** Memanen tesis dan disertasi hukum dari UNAIR Repository (Subject K = Law).
4. **Koordinator:** Mengekstrak struktur kanonikal dan menyegarkan indeks FTS5 (`indeks.db`).
5. **Auto-Release:** Membundel seluruh data matang ke format `.zip` di GitHub Releases untuk siap diunduh sekali klik.
