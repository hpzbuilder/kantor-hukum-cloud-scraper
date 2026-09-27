#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper_berita_haji.py — Pemungut Berita & Informasi Kredibel Haji dan Umrah (5 Tahun Terakhir)
Sumber Utama: LKBN ANTARA (Kantor Berita Negara RI)
Cakupan Waktu: 2021–2026 (5 tahun ke belakang hingga yang paling mutakhir)
Keluaran: File Markdown (.md) ber-frontmatter YAML + Database SQLite (knowledge_base/haji_umroh.db)
"""

import os
import re
import sys
import time
import random
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base"
DB_PATH = KB_DIR / "haji_umroh.db"
BERITA_DIR = KB_DIR / "Haji_Umroh" / "Berita"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
]

TAGS_CONFIG = [
    {"tag": "haji", "kategori": "Haji", "max_pages": 40},
    {"tag": "umrah", "kategori": "Umrah", "max_pages": 30},
    {"tag": "umroh", "kategori": "Umrah", "max_pages": 20},
    {"tag": "biaya-haji", "kategori": "Biaya Haji (BPIH)", "max_pages": 15},
    {"tag": "bpkh", "kategori": "Pengelolaan Dana Haji (BPKH)", "max_pages": 15},
    {"tag": "haji-2024", "kategori": "Haji 2024", "max_pages": 25},
    {"tag": "haji-2023", "kategori": "Haji 2023", "max_pages": 25},
    {"tag": "haji-2022", "kategori": "Haji 2022", "max_pages": 20},
    {"tag": "haji-2021", "kategori": "Haji 2021", "max_pages": 15},
]

def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS berita_haji (
            id TEXT PRIMARY KEY,
            judul TEXT NOT NULL,
            sumber TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            penulis TEXT,
            editor TEXT,
            tanggal_terbit TEXT,
            tahun INTEGER,
            kategori TEXT,
            ringkasan TEXT,
            kata_kunci TEXT,
            jumlah_kata INTEGER,
            path_berkas TEXT,
            created_at TEXT
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_berita_tahun ON berita_haji(tahun);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_berita_kategori ON berita_haji(kategori);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_berita_url ON berita_haji(url);")
    conn.commit()
    conn.close()

def get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.antaranews.com/"
    }

def clean_slug(title: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[\s_-]+", "-", slug).strip("-")
    return slug[:max_len]

def extract_article_detail(session: requests.Session, url: str) -> dict:
    resp = session.get(url, headers=get_headers(), timeout=15)
    if resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    
    # Judul
    h1 = soup.find("h1")
    if not h1:
        return None
    judul = h1.get_text(strip=True)
    if not judul:
        return None

    # Tanggal
    date_div = soup.find(class_=lambda c: c and "article-detail-info" in c)
    tanggal_terbit = ""
    if date_div:
        for item in date_div.find_all(["span", "li"]):
            txt = item.get_text(strip=True)
            if "wib" in txt.lower() or any(yr in txt for yr in ["2021", "2022", "2023", "2024", "2025", "2026"]):
                tanggal_terbit = txt
                break
    if not tanggal_terbit:
        time_tag = soup.find("time")
        if time_tag:
            tanggal_terbit = time_tag.get_text(strip=True)

    # Tahun
    year_match = re.search(r"202[1-6]", tanggal_terbit)
    if year_match:
        tahun = int(year_match.group(0))
    else:
        # Cek dari URL atau fallback tahun ini
        m_url_yr = re.search(r"202[1-6]", url)
        tahun = int(m_url_yr.group(0)) if m_url_yr else datetime.now().year

    # Filter batas 5 tahun (2021-2026)
    if tahun < 2021:
        return None

    # Pewarta dan Editor
    penulis = ""
    editor = ""
    author_tag = soup.find(lambda e: e.name == "p" and ("Pewarta:" in e.text or "Editor:" in e.text))
    if author_tag:
        auth_text = author_tag.get_text(strip=True)
        m_p = re.search(r"Pewarta:\s*([^E]+?)(?:Editor:|$)", auth_text)
        if m_p:
            penulis = m_p.group(1).strip()
        m_e = re.search(r"Editor:\s*([^C]+?)(?:Copyright|$)", auth_text)
        if m_e:
            editor = m_e.group(1).strip()

    # Isi Berita
    body = soup.find("div", class_=lambda c: c and "post-content" in c)
    paragraphs = []
    if body:
        for p in body.find_all("p"):
            ptxt = p.get_text(strip=True)
            if not ptxt:
                continue
            if ptxt.startswith("Pewarta:") or ptxt.startswith("Editor:") or "copyright" in ptxt.lower():
                continue
            if "baca juga:" in ptxt.lower():
                continue
            paragraphs.append(ptxt)

    if not paragraphs:
        return None

    jumlah_kata = sum(len(p.split()) for p in paragraphs)
    ringkasan = paragraphs[0]
    if len(paragraphs) > 1 and len(ringkasan.split()) < 40:
        ringkasan += " " + paragraphs[1]

    # Ekstrak tag / kata kunci
    tags = []
    tag_cloud = soup.find(class_=lambda c: c and ("tag" in c or "topic" in c))
    if tag_cloud:
        for a in tag_cloud.find_all("a"):
            t_txt = a.get_text(strip=True).lstrip("#")
            if t_txt and t_txt not in tags:
                tags.append(t_txt)

    return {
        "judul": judul,
        "tanggal_terbit": tanggal_terbit,
        "tahun": tahun,
        "penulis": penulis,
        "editor": editor,
        "paragraphs": paragraphs,
        "ringkasan": ringkasan,
        "tags": tags,
        "jumlah_kata": jumlah_kata
    }

def format_markdown(data: dict, id_artikel: str, sumber: str, url: str, kategori: str) -> str:
    clean_title = data["judul"].replace('"', '\\"')
    clean_summary = data["ringkasan"].replace('"', '\\"')
    tags_str = ", ".join(f'"{t}"' for t in data["tags"]) if data["tags"] else f'"{kategori}"'
    
    body_md = "\n\n".join(data["paragraphs"])

    md_content = f"""---
id: {id_artikel}
judul: "{clean_title}"
sumber: "{sumber}"
url: "{url}"
tanggal: "{data['tanggal_terbit']}"
tahun: {data['tahun']}
kategori: "{kategori}"
penulis: "{data['penulis']}"
editor: "{data['editor']}"
kata_kunci: [{tags_str}]
jumlah_kata: {data['jumlah_kata']}
ekstraksi: "{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
---

# {data['judul']}

> **Sumber Resmi:** [{sumber}]({url})  
> **Tanggal Terbit:** {data['tanggal_terbit']}  
> **Pewarta:** {data['penulis'] or 'Redaksi'} | **Editor:** {data['editor'] or 'Redaksi'}  
> **Kategori:** {kategori}

## Ringkasan Eksekutif
{data['ringkasan']}

## Isi Berita Lengkap

{body_md}
"""
    return md_content

def harvest_antara(limit_total: int = 50, specific_tag: str = None, start_page: int = 1):
    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    session = requests.Session()
    session.headers.update(get_headers())

    tags_to_run = TAGS_CONFIG
    quota_per_tag = limit_total
    if specific_tag:
        tags_to_run = [t for t in TAGS_CONFIG if t["tag"] == specific_tag]
        if not tags_to_run:
            tags_to_run = [{"tag": specific_tag, "kategori": specific_tag.capitalize(), "max_pages": 25}]
    else:
        quota_per_tag = max(3, limit_total // len(tags_to_run))

    total_scraped = 0
    print(f"\n{'='*65}")
    print("▶ MEMANEN BERITA & INFORMASI HAJI/UMRAH (LKBN ANTARA NEWS)")
    print(f"  Target Kuota Total : {limit_total} berita | Cakupan: 2021–2026")
    print(f"  Kuota per Kategori : {quota_per_tag} berita | Halaman Mulai: {start_page}")
    print(f"{'='*65}")

    for tag_info in tags_to_run:
        if total_scraped >= limit_total:
            break

        tag = tag_info["tag"]
        kategori = tag_info["kategori"]
        max_p = tag_info["max_pages"]

        print(f"\n📂 Menelusuri Tag: #{tag} (Kategori: {kategori}, Maks Page: {max_p})")

        consecutive_dupes = 0
        tag_scraped = 0

        for page in range(start_page, max_p + 1):
            if total_scraped >= limit_total or tag_scraped >= quota_per_tag:
                break
            if consecutive_dupes >= 3:
                print(f"  ℹ Halaman #{tag} berulang/sudah dipanen semua. Melangkah ke tag berikutnya.")
                break

            page_url = f"https://www.antaranews.com/tag/{tag}" if page == 1 else f"https://www.antaranews.com/tag/{tag}/{page}"
            try:
                r = session.get(page_url, headers=get_headers(), timeout=15)
                if r.status_code != 200:
                    print(f"  ⚠ HTTP {r.status_code} pada {page_url}")
                    break

                soup = BeautifulSoup(r.text, "html.parser")
                pag = soup.find(class_=lambda c: c and "pagination" in c)
                
                # Kartu artikel di kolom utama
                cards = []
                if pag and pag.parent:
                    cards = pag.parent.select(".card__post, article, .simple-post")
                if not cards:
                    # Fallback jika struktur berbeda
                    cards = soup.select(".col-md-8 .simple-post, .col-md-8 article, .col-md-8 .card__post")

                if not cards:
                    print(f"  ℹ Halaman {page} tidak memuat kartu artikel. Selesai untuk tag #{tag}.")
                    break

                new_in_page = 0
                for card in cards:
                    if total_scraped >= limit_total:
                        break

                    a = card.find("a", href=True)
                    if not a or "/berita/" not in a["href"]:
                        continue

                    art_url = a["href"].split("?")[0] # strip tracking param
                    
                    # Cek URL apakah sudah pernah tersimpan
                    cur.execute("SELECT id FROM berita_haji WHERE url = ?", (art_url,))
                    if cur.fetchone():
                        continue

                    # ID Artikel dari nomor berita Antara
                    m_id = re.search(r"/berita/(\d+)/", art_url)
                    art_num = m_id.group(1) if m_id else str(int(time.time()))
                    id_artikel = f"antara_{art_num}"

                    # Ekstraksi detail berita
                    time.sleep(random.uniform(0.3, 0.7))
                    detail = extract_article_detail(session, art_url)
                    if not detail:
                        continue

                    # Tentukan folder output per tahun
                    tahun_folder = BERITA_DIR / str(detail["tahun"])
                    tahun_folder.mkdir(parents=True, exist_ok=True)

                    slug = clean_slug(detail["judul"])
                    md_filename = f"{art_num}_{slug}.md"
                    md_path = tahun_folder / md_filename

                    # Format & simpan Markdown
                    md_text = format_markdown(detail, id_artikel, "ANTARA News", art_url, kategori)
                    md_path.write_text(md_text, encoding="utf-8")

                    rel_path = str(md_path.relative_to(KB_DIR)).replace("\\", "/")

                    # Simpan ke SQLite
                    cur.execute("""
                        INSERT OR REPLACE INTO berita_haji (
                            id, judul, sumber, url, penulis, editor, tanggal_terbit,
                            tahun, kategori, ringkasan, kata_kunci, jumlah_kata,
                            path_berkas, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        id_artikel,
                        detail["judul"],
                        "ANTARA News",
                        art_url,
                        detail["penulis"],
                        detail["editor"],
                        detail["tanggal_terbit"],
                        detail["tahun"],
                        kategori,
                        detail["ringkasan"],
                        ", ".join(detail["tags"]),
                        detail["jumlah_kata"],
                        rel_path,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ))
                    conn.commit()

                    total_scraped += 1
                    new_in_page += 1
                    tag_scraped += 1
                    print(f"  ✔ [{detail['tahun']}] {detail['judul'][:65]}... ({detail['jumlah_kata']} kata)")

                if new_in_page == 0:
                    consecutive_dupes += 1
                else:
                    consecutive_dupes = 0

            except Exception as e:
                print(f"  ✘ Kesalahan memuat {page_url}: {e}")
                time.sleep(2)

    conn.close()
    print(f"\n✔ Selesai! Sebanyak {total_scraped} artikel berita berhasil dipanen.")

def print_stats():
    init_db(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM berita_haji")
    total_berita = cur.fetchone()[0]

    cur.execute("SELECT tahun, COUNT(*) FROM berita_haji GROUP BY tahun ORDER BY tahun DESC")
    by_tahun = cur.fetchall()

    cur.execute("SELECT kategori, COUNT(*) FROM berita_haji GROUP BY kategori ORDER BY COUNT(*) DESC")
    by_kategori = cur.fetchall()

    cur.execute("SELECT SUM(jumlah_kata) FROM berita_haji")
    total_kata = cur.fetchone()[0] or 0

    conn.close()

    print("\n" + "="*65)
    print("📊 STATISTIK KNOWLEDGE BASE BERITA HAJI & UMRAH (SQL VERIFIED)")
    print("="*65)
    print(f"Total Berita Terkumpul : {total_berita} artikel")
    print(f"Total Kata Teks Penuh   : {total_kata:,} kata")
    print(f"Lokasi Database SQLite : {DB_PATH.name}")
    print(f"Folder Berkas Markdown : knowledge_base/Haji_Umroh/Berita/")

    print("\n▶ Sebaran Berdasarkan Tahun (5 Tahun Terakhir):")
    if by_tahun:
        for th, cnt in by_tahun:
            print(f"  - Tahun {th}: {cnt} artikel")
    else:
        print("  (Belum ada data berita)")

    print("\n▶ Sebaran Berdasarkan Kategori:")
    if by_kategori:
        for kat, cnt in by_kategori:
            print(f"  - {kat}: {cnt} artikel")
    else:
        print("  (Belum ada data)")
    print("="*65)

def main():
    parser = argparse.ArgumentParser(description="Scraper Berita & Informasi Haji & Umrah (ANTARA News)")
    parser.add_argument("--limit", type=int, default=30, help="Jumlah artikel yang ingin dipanen")
    parser.add_argument("--tag", type=str, default=None, help="Tag spesifik (haji, umrah, biaya-haji, bpkh, haji-2024, dll)")
    parser.add_argument("--start-page", type=int, default=1, help="Halaman awal pagination")
    parser.add_argument("--stats", action="store_true", help="Tampilkan statistik database")
    args = parser.parse_args()

    if args.stats:
        print_stats()
        return

    harvest_antara(limit_total=args.limit, specific_tag=args.tag, start_page=args.start_page)
    print_stats()

if __name__ == "__main__":
    main()
