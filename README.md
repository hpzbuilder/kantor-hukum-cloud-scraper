# Kantor Hukum Virtual — Cloud Auto-Harvest Scraper ⚖️

Sistem scraping, ekstraksi teks (PyMuPDF), dan pengkayaan metadata hukum Indonesia.
Sebagian berjalan di GitHub Actions (cron 4 jam), sebagian **wajib lokal**.

## Kenyataan akses sumber (diverifikasi 9 Okt 2026)

| Sumber | Dari GitHub Actions | Dari PC lokal | robots.txt |
|---|---|---|---|
| JDIH BPK (`peraturan.bpk.go.id`) | 403 Cloudflare | OK | mengizinkan |
| JDIH Kemenkum (`peraturan.go.id`) | diblokir WAF | OK | mengizinkan |
| OAI-PMH UNAIR / Undip | OK | OK | — |
| Putusan MA (`putusan3.mahkamahagung.go.id`) | 403 | challenge Cloudflare | **`Disallow: /` untuk semua bot** — jangan di-scrape |
| MKRI (`mkri.id`) | 403 | challenge Cloudflare | — (pakai lampiran uji materi BPK) |

## Arsitektur

1. **Cloud (cron 4 jam):** `run_cloud_batch.py` → `repo_harvest.py` (tesis/disertasi hukum UNAIR & Undip,
   urut terbaru, lewati yang sudah diproses) → `kb_metadata.py` → `kb_index.py` → rilis `.zip`.
2. **Lokal (Task Scheduler / manual):** `harvester_bpk.py` — peraturan pusat dari sitemap per tahun
   (terbaru dulu; UU/Perppu/PP/Perpres → Inpres/Keppres/Perma/POJK → peraturan menteri), metadata status
   berlaku & relasi, teks penuh PDF, plus **putusan uji materi MK** yang dilampirkan BPK.
   Dijalankan via `Kantor Hukum Virtual\panen_bpk_lokal.bat`.

```bash
python harvester_bpk.py --kb "D:/aisemua/03-Hukum dan haji umroh/Kantor Hukum Virtual/knowledge_base" --limit 150
python harvester_bpk.py --kb "<...>/knowledge_base" --statistik
```

## Aturan data

- Status `selesai` hanya bila teks penuh benar-benar terekstrak; PDF pindaian → `teks-kosong` (perlu OCR).
- Blokir (403/429 beruntun) → berhenti dengan exit code 3, bukan "sukses 0 dokumen".
