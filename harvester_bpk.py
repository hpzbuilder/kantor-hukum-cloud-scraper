#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvester_bpk.py — Cloud Harvester Resmi Peraturan & Putusan Uji Materi BPK RI
=============================================================================
Mengambil peraturan dan putusan uji materiil terbaru dari portal resmi BPK RI
(https://peraturan.bpk.go.id). Server BPK ramah terhadap request (tanpa blokir
Cloudflare agresif) sehingga sangat handal dijalankan di GitHub Actions runner.
"""

import os
import re
import ssl
import sys
import time
import urllib.request
import sqlite3
import logging
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base"
KATALOG_DB = KB_DIR / "katalog.db"
REGULASI_DIR = KB_DIR / "Regulasi_Nasional"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("harvester_bpk")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

def init_db():
    KATALOG_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(KATALOG_DB)
    con.execute("""CREATE TABLE IF NOT EXISTS dokumen (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE,
        tipe TEXT,
        nomor TEXT,
        tahun TEXT,
        judul TEXT,
        url TEXT,
        status TEXT,
        sumber TEXT,
        terakhir_diperbarui TEXT
    )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bpk_slug ON dokumen(slug)")
    con.commit()
    return con

def crawl_bpk(limit_pages=3):
    con = init_db()
    REGULASI_DIR.mkdir(parents=True, exist_ok=True)

    base_url = "https://peraturan.bpk.go.id"
    # sort=4 -> urutkan dari yang terbaru diundangkan/diunggah
    search_urls = [
        ("Peraturan Terbaru", "https://peraturan.bpk.go.id/Search?sort=4"),
        ("Uji Materi & Putusan", "https://peraturan.bpk.go.id/Search?keywords=putusan&sort=4")
    ]

    total_baru = 0

    for label, base_search in search_urls:
        log.info(f"=== Menelusuri BPK: {label} ===")
        for page in range(1, limit_pages + 1):
            target_url = f"{base_search}&p={page}" if page > 1 else base_search
            try:
                req = urllib.request.Request(target_url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, context=CTX, timeout=15) as resp:
                    html = resp.read().decode("utf-8", "ignore")
                
                soup = BeautifulSoup(html, "html.parser")
                # Temukan link ke detail
                links = soup.find_all("a", href=True)
                detail_links = {}
                for a in links:
                    href = a["href"]
                    if href.startswith("/Details/"):
                        txt = a.get_text(strip=True)
                        if txt and len(txt) > 5 and href not in detail_links:
                            detail_links[href] = txt

                if not detail_links:
                    log.info(f"  Halaman {page}: Tidak ada item ditemukan.")
                    break

                log.info(f"  Halaman {page}: Menemukan {len(detail_links)} dokumen.")

                for rel_url, title in detail_links.items():
                    full_url = base_url + rel_url
                    slug = re.sub(r'[^a-zA-Z0-9_\-]', '_', rel_url.replace('/Details/', ''))[:100]

                    # Cek apakah sudah ada di DB
                    ada = con.execute("SELECT id FROM dokumen WHERE slug=?", (slug,)).fetchone()
                    if ada:
                        continue

                    # Parsing tipe, nomor, tahun dari judul jika memungkinkan
                    tipe = "Peraturan / Putusan"
                    nomor = "-"
                    tahun = str(datetime.now().year)

                    m_nomor = re.search(r'(?:Nomor|No\.?)\s*([0-9A-Za-z\/\.\-]+)', title, re.IGNORECASE)
                    if m_nomor:
                        nomor = m_nomor.group(1).strip()
                    m_tahun = re.search(r'Tahun\s*([12][0-9]{3})', title, re.IGNORECASE)
                    if m_tahun:
                        tahun = m_tahun.group(1).strip()

                    # Tulis ringkasan .md
                    md_path = REGULASI_DIR / f"{slug[:70]}.md"

                    # Simpan ke katalog.db
                    con.execute("""INSERT OR IGNORE INTO dokumen 
                        (slug, tipe, nomor, tahun, judul, url, md_path, status, diambil, tipe_sumber)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (slug, tipe, nomor, tahun, title, full_url, str(md_path.relative_to(BASE_DIR)), "selesai", datetime.now().isoformat(), "BPK RI")
                    )
                    con.commit()
                    total_baru += 1

                    if not md_path.exists():
                        isi_md = f"""# {title}

**Tipe / Klasifikasi:** {tipe}  
**Nomor:** {nomor} | **Tahun:** {tahun}  
**Sumber Resmi:** [Database Peraturan BPK RI]({full_url})  
**Waktu Akuisisi:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  

---

### Informasi Dokumen
Dokumen ini diakuisisi secara otomatis dari repositori resmi JDIH/Database Peraturan BPK RI.
Teks dan berkas asli tersimpan di peladen resmi negara dan terindeks dalam sistem FTS5 Kantor Hukum Virtual.
"""
                        md_path.write_text(isi_md, encoding="utf-8")

                time.sleep(1)
            except Exception as e:
                log.warning(f"  Gagal mengambil {target_url}: {e}")
                break

    con.close()
    log.info(f"✔ Panen BPK Selesai: {total_baru} dokumen baru berhasil disimpan ke katalog!")
    return total_baru

if __name__ == "__main__":
    c = crawl_bpk(limit_pages=2)
    print(f"Total baru: {c}")
