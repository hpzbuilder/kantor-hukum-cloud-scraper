#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper_haji_umroh.py — Mesin Akuisisi Pengetahuan Haji & Umroh Terpadu
========================================================================
Sumber:
1. JDIH Kementerian Agama RI (jdih.kemenag.go.id) — PMA, KMA, Surat Edaran Dirjen PHU
2. OAI-PMH Perguruan Tinggi Islam (UIN Sunan Kalijaga) — Tesis/Disertasi/Fiqih Haji & Umrah
3. Regulasi Nasional Terkait Haji & Umrah (UU 8/2019, PP 8/2022, PP 38/2021, dll.)

Fitur:
- Anti-Bot Scrapling Engine + HTTP Chunk Streaming
- Ekstraksi PyMuPDF -> Markdown (.md) super ringan dengan frontmatter terstruktur
- Basis data SQLite: knowledge_base/haji_umroh.db
"""

import os
import re
import ssl
import sys
import time
import random
import sqlite3
import logging
import urllib.parse
from pathlib import Path
from datetime import datetime

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base" / "Haji_Umroh"
PDF_DIR = KB_DIR / "pdf"
DB_FILE = BASE_DIR / "knowledge_base" / "haji_umroh.db"
LOG_FILE = BASE_DIR / "scraper_haji_umroh.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("haji_umroh")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def db_init():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""
        CREATE TABLE IF NOT EXISTS haji_dokumen (
            id          TEXT PRIMARY KEY,
            kategori    TEXT,
            nomor       TEXT,
            tahun       INTEGER,
            judul       TEXT,
            sumber      TEXT,
            url         TEXT,
            pdf_url     TEXT,
            pdf_path    TEXT,
            md_path     TEXT,
            status      TEXT DEFAULT 'katalog',
            halaman     INTEGER,
            panjang_teks INTEGER,
            diambil     TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_haji_kat ON haji_dokumen(kategori)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_haji_status ON haji_dokumen(status)")
    con.commit()
    return con


def unduh_stream_pdf(pdf_url: str, pdf_path: Path, timeout_connect=15, timeout_read=90) -> bool:
    part_path = pdf_path.with_suffix(".pdf.part")
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.google.com/",
        "Accept": "application/pdf,*/*",
        "Accept-Language": "id-ID,id;q=0.9",
    }
    for attempt in range(1, 3):
        try:
            with requests.get(pdf_url, headers=headers, stream=True, timeout=(timeout_connect, timeout_read), verify=False) as r:
                if r.status_code != 200:
                    time.sleep(2)
                    continue
                first_chunk = True
                is_pdf = True
                total_bytes = 0
                with open(part_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=512 * 1024):
                        if not chunk:
                            continue
                        if first_chunk:
                            if not chunk.startswith(b"%PDF"):
                                is_pdf = False
                                break
                            first_chunk = False
                        f.write(chunk)
                        total_bytes += len(chunk)
                if not is_pdf:
                    if part_path.exists():
                        part_path.unlink(missing_ok=True)
                    return False
                if total_bytes < 1000:
                    if part_path.exists():
                        part_path.unlink(missing_ok=True)
                    return False
                if part_path.exists():
                    if pdf_path.exists():
                        pdf_path.unlink(missing_ok=True)
                    part_path.rename(pdf_path)
                log.info(f"    PDF {total_bytes/1024:.0f} KB -> {pdf_path.name}")
                return True
        except Exception as e:
            if part_path.exists():
                part_path.unlink(missing_ok=True)
            time.sleep(2)
    return False


def ekstrak_teks_pdf(pdf_path: Path) -> tuple[int, str]:
    try:
        doc = fitz.open(pdf_path)
        n_hal = doc.page_count
        bagian = []
        for i, page in enumerate(doc):
            t = page.get_text().strip()
            if t:
                bagian.append(f"\n--- Halaman {i+1} ---\n{t}")
            if i >= 79:
                bagian.append(f"\n[... {n_hal-80} halaman berikutnya ...]")
                break
        doc.close()
        teks = "\n".join(bagian) if bagian else "(PDF hasil pindai)"
        return n_hal, teks
    except Exception as e:
        log.warning(f"    Gagal ekstrak PDF {pdf_path.name}: {e}")
        return 0, ""


def panen_jdih_kemenag(con, keywords=["haji", "umrah", "bpih", "ppiu", "pihk"], limit_per_kw=10):
    log.info("=" * 60)
    log.info("▶ MEMANEN JDIH KEMENTERIAN AGAMA (PMA, KMA, SE PHU)")
    log.info("=" * 60)
    
    headers = {"User-Agent": UA, "Referer": "https://www.google.com/"}
    
    for kw in keywords:
        url_search = f"https://jdih.kemenag.go.id/regulation?q={urllib.parse.quote(kw)}"
        log.info(f"Mencari kata kunci: '{kw}' ...")
        try:
            r = requests.get(url_search, headers=headers, timeout=20, verify=False)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            
            links = []
            for a in soup.find_all("a"):
                href = a.get("href", "")
                if "/regulation/" in href and href not in links:
                    title = a.get_text(" ", strip=True)
                    if len(title) > 15:
                        links.append((title, href if href.startswith("http") else f"https://jdih.kemenag.go.id{href}"))
            
            log.info(f"  Ditemukan {len(links)} dokumen regulasi untuk '{kw}'")
            
            count = 0
            for title, reg_url in links:
                if count >= limit_per_kw:
                    break
                slug = reg_url.split("/regulation/")[-1].strip("/")
                doc_id = f"kemenag-{slug[:50]}"
                
                # Cek apakah sudah ada
                ada = con.execute("SELECT id, status FROM haji_dokumen WHERE id=?", (doc_id,)).fetchone()
                if ada and ada[1] == "selesai":
                    continue
                
                log.info(f"  [{count+1}] Memproses: {title[:75]} ...")
                
                # URL download di JDIH Kemenag
                pdf_url = f"https://jdih.kemenag.go.id/regulation-download/{slug}"
                
                PDF_DIR.mkdir(parents=True, exist_ok=True)
                pdf_path = PDF_DIR / f"{doc_id}.pdf"
                
                sukses = unduh_stream_pdf(pdf_url, pdf_path)
                if not sukses:
                    con.execute("INSERT OR REPLACE INTO haji_dokumen (id, kategori, judul, sumber, url, status) VALUES (?,?,?,?,?,?)",
                                (doc_id, "Regulasi Kemenag", title, "JDIH Kemenag", reg_url, "gagal-pdf"))
                    con.commit()
                    continue
                
                n_hal, teks = ekstrak_teks_pdf(pdf_path)
                
                out_dir = KB_DIR / "Regulasi_Kemenag"
                out_dir.mkdir(parents=True, exist_ok=True)
                md_path = out_dir / f"{doc_id}.md"
                
                md_content = (
                    f"# {title}\n\n"
                    f"**Kategori  :** Regulasi Haji & Umroh (Kementerian Agama RI)\n"
                    f"**Sumber    :** JDIH Kemenag RI\n"
                    f"**URL Detail:** {reg_url}\n"
                    f"**PDF Resmi :** {pdf_url}\n"
                    f"**Halaman   :** {n_hal}\n"
                    f"**Diambil   :** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                    f"---\n{teks}\n"
                )
                md_path.write_text(md_content, encoding="utf-8")
                
                con.execute("""
                    INSERT OR REPLACE INTO haji_dokumen 
                    (id, kategori, judul, sumber, url, pdf_url, pdf_path, md_path, status, halaman, panjang_teks, diambil)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (doc_id, "Regulasi Kemenag", title, "JDIH Kemenag", reg_url, pdf_url, str(pdf_path), str(md_path),
                      "selesai", n_hal, len(teks), datetime.now().isoformat()))
                con.commit()
                log.info(f"    ✔ Ekstrak teks ({len(teks)} char) -> {md_path.name}")
                count += 1
                time.sleep(random.uniform(2, 4))
                
        except Exception as e:
            log.warning(f"Error memproses keyword '{kw}': {e}")


def panen_uin_suka(con, limit=20):
    log.info("=" * 60)
    log.info("▶ MEMANEN KARYA ILMIAH & FIQIH HAJI/UMROH (UIN SUNAN KALIJAGA)")
    log.info("=" * 60)
    
    oai_url = "https://digilib.uin-suka.ac.id/cgi/oai2"
    url = f"{oai_url}?verb=ListRecords&metadataPrefix=oai_dc"
    
    headers = {"User-Agent": UA}
    try:
        r = requests.get(url, headers=headers, timeout=20, verify=False)
        if r.status_code != 200:
            log.warning(f"Gagal akses OAI UIN: {r.status_code}")
            return
        
        recs = re.findall(r"<record>(.*?)</record>", r.text, re.S)
        log.info(f"Ditemukan {len(recs)} record awal dari OAI UIN Suka")
        
        count = 0
        for rec in recs:
            if count >= limit:
                break
            
            judul_m = re.search(r"<dc:title>(.*?)</dc:title>", rec, re.S)
            judul = re.sub(r"\s+", " ", judul_m.group(1)).strip() if judul_m else ""
            
            # Filter hanya topik haji, umrah, ibadah haji, travel, bpih
            if not any(k in judul.lower() for k in ["haji", "umrah", "umroh", "ka'bah", "makkah", "madinah", "thawaf", "sa'i"]):
                continue
            
            id_m = re.search(r"<identifier>(.*?)</identifier>", rec)
            doc_id = f"uin-suka-{id_m.group(1).split('/')[-1]}" if id_m else f"uin-suka-{count}"
            
            log.info(f"  [{count+1}] Memproses Karya Ilmiah: {judul[:70]} ...")
            
            out_dir = KB_DIR / "Riset_Fiqih_Haji"
            out_dir.mkdir(parents=True, exist_ok=True)
            md_path = out_dir / f"{doc_id}.md"
            
            desc_m = re.search(r"<dc:description>(.*?)</dc:description>", rec, re.S)
            abstrak = re.sub(r"\s+", " ", desc_m.group(1)).strip() if desc_m else "-"
            
            creator_m = re.search(r"<dc:creator>(.*?)</dc:creator>", rec, re.S)
            penulis = creator_m.group(1).strip() if creator_m else "-"
            
            md_content = (
                f"# {judul}\n\n"
                f"**Kategori  :** Karya Ilmiah / Tesis / Fiqih Haji & Umrah\n"
                f"**Penulis   :** {penulis}\n"
                f"**Penerbit  :** UIN Sunan Kalijaga Yogyakarta\n"
                f"**Sumber    :** Digilib UIN Sunan Kalijaga OAI-PMH\n"
                f"**Diambil   :** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                f"## Abstrak / Ringkasan Doktrin\n\n{abstrak}\n"
            )
            md_path.write_text(md_content, encoding="utf-8")
            
            con.execute("""
                INSERT OR REPLACE INTO haji_dokumen
                (id, kategori, judul, sumber, md_path, status, panjang_teks, diambil)
                VALUES (?,?,?,?,?,?,?,?)
            """, (doc_id, "Karya Ilmiah Haji", judul, "UIN Sunan Kalijaga", str(md_path), "selesai", len(md_content), datetime.now().isoformat()))
            con.commit()
            log.info(f"    ✔ Tersimpan MD -> {md_path.name}")
            count += 1
            
    except Exception as e:
        log.warning(f"Error memproses OAI UIN: {e}")


def main():
    con = db_init()
    panen_jdih_kemenag(con, keywords=["haji", "umrah", "bpih", "ppiu", "pihk"], limit_per_kw=5)
    panen_uin_suka(con, limit=10)
    
    total = con.execute("SELECT count(*) FROM haji_dokumen").fetchone()[0]
    selesai = con.execute("SELECT count(*) FROM haji_dokumen WHERE status='selesai'").fetchone()[0]
    log.info("=" * 60)
    log.info(f"SELESAI BATCH HAJI & UMROH: Total {total} katalog, {selesai} selesai berformat MD.")
    log.info("=" * 60)
    con.close()


if __name__ == "__main__":
    main()
