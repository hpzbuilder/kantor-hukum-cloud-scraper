#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scraper_jdih_kemenkum.py — Akuisisi dokumen hukum resmi JDIH Kemenkum RI
========================================================================
Sumber : https://jdih.kemenkum.go.id  (robots.txt: Allow / kecuali /backend,
         /profile, /site/login — semua path yang kami pakai DIIZINKAN)
Katalog: sitemap.xml resmi (90.000+ dokumen) — bukan endpoint terlarang.

Arsitektur:
  1. KATALOG  : baca sitemap.xml -> simpan metadata ke SQLite
                (knowledge_base/katalog.db) — fondasi database kuat.
  2. UNDUH    : ambil halaman detail (judul + link PDF) lalu PDF resmi.
  3. EKSTRAK  : PyMuPDF -> teks -> .md per dokumen.
  4. SOPAN    : jeda manusiawi (5-12 dtk), istirahat panjang tiap 25 dok,
                resume otomatis (yang sudah ada dilewati).

Pakai:
  python scraper_jdih_kemenkum.py --katalog                 # bangun/refresh katalog
  python scraper_jdih_kemenkum.py --unduh --limit 20        # unduh 20 dokumen
  python scraper_jdih_kemenkum.py --unduh --tipe uu,pp --limit 10
"""

import re
import ssl
import sys
import json
import time
import random
import sqlite3
import logging
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- konfigurasi
BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base" / "Regulasi_Nasional"
PDF_DIR = KB_DIR / "pdf"
DB_FILE = BASE_DIR / "knowledge_base" / "katalog.db"
SITEMAP_CACHE = BASE_DIR / "_probe" / "kemenkum_sitemap.xml"
LOG_FILE = BASE_DIR / "scraper_jdih_kemenkum.log"

SITE = "https://jdih.kemenkum.go.id"

# Urutan prioritas tipe dokumen (yang paling sering dibutuhkan kantor hukum)
PRIORITAS_TIPE = ["uud", "uu", "perppu", "pp", "perpres", "inpres", "keppres", "permen", "permenkum",
                  "kepmen", "se", "putusan", "perda", "pergub", "perbup", "perwali",
                  "staatsblad", "statuten",
                  "hukum", "analisis", "naskah", "penelitian", "himpunan", "laporan"]

# jeda manusiawi (dioptimasi aman bersama Scrapling anti-bot)
JEDA_HALAMAN = (3, 6)       # antar halaman detail
JEDA_PDF = (2, 4)           # antar unduhan PDF
ISTIRAHAT_TIAP = 50         # dokumen
ISTIRAHAT_DETIK = (20, 45)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("jdih")


# ---------------------------------------------------------------- util HTTP
try:
    from scrapling import Fetcher
    _scrapling_fetcher = Fetcher()
    log.info("Scrapling Anti-Bot Engine aktif untuk JDIH Kemenkum.")
except Exception as _e:
    _scrapling_fetcher = None
    log.info(f"Scrapling tidak aktif ({_e}), menggunakan urllib.")

def http_get(url: str, timeout: int = 60) -> tuple[int | None, bytes]:
    if _scrapling_fetcher is not None:
        try:
            page = _scrapling_fetcher.get(url, timeout=timeout)
            return page.status, page.body
        except Exception:
            pass  # fallback ke urllib jika ada issue
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/pdf,*/*",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        log.warning(f"    HTTP error {url[:90]}: {type(e).__name__}")
        return None, b""


def unduh_stream_pdf(pdf_url: str, pdf_path: Path, timeout_connect: int = 15, timeout_read: int = 90) -> bool:
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
                    log.warning(f"    HTTP {r.status_code} saat unduh PDF (percobaan {attempt})")
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
                                log.warning(f"    bukan biner PDF (header: {chunk[:20]!r})")
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
                    log.warning(f"    Ukuran PDF terlalu kecil ({total_bytes} B)")
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
            log.warning(f"    Stream PDF error: {type(e).__name__} {e} (percobaan {attempt})")
            if part_path.exists():
                part_path.unlink(missing_ok=True)
            time.sleep(2)
    return False


def jeda(rng, label=""):
    t = random.uniform(*rng)
    log.info(f"    ... jeda {t:.1f}s {label}")
    time.sleep(t)


# ---------------------------------------------------------------- database
def db_init() -> sqlite3.Connection:
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("""
        CREATE TABLE IF NOT EXISTS dokumen (
            id          TEXT PRIMARY KEY,
            slug        TEXT,
            tipe        TEXT,
            nomor       TEXT,
            tahun       INTEGER,
            judul       TEXT,
            url         TEXT,
            pdf_url     TEXT,
            pdf_path    TEXT,
            md_path     TEXT,
            status      TEXT DEFAULT 'katalog',
            ukuran_pdf  INTEGER,
            halaman     INTEGER,
            panjang_teks INTEGER,
            diambil     TEXT
        )""")
    # Migrasi additive & aman: kolom klasifikasi ulang tipe (lihat
    # fix_katalog_tipe.py). Kolom `tipe` ASLI tidak diubah; scraper memakai
    # COALESCE(tipe_benar, tipe) sehingga perilaku lama tetap utuh.
    _kol = {r[1] for r in con.execute("PRAGMA table_info(dokumen)")}
    for _nama, _tipe in (("tipe_benar", "TEXT"), ("tipe_sumber", "TEXT"),
                         ("tipe_diperbaiki_pada", "TEXT")):
        if _nama not in _kol:
            con.execute(f"ALTER TABLE dokumen ADD COLUMN {_nama} {_tipe}")
            log.info(f"  migrasi: kolom '{_nama}' ditambahkan ke katalog.db")
    con.commit()
    con.execute("CREATE INDEX IF NOT EXISTS idx_tipe ON dokumen(tipe)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tahun ON dokumen(tahun)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_status ON dokumen(status)")
    con.commit()
    return con


SLUG_RE = re.compile(r"^/dokumen/(?P<id>\d+)-(?P<slug>[a-z0-9\-]+)$")


def parse_slug(slug: str) -> tuple[str, str, int | None]:
    """Dari slug 'uu-1-2026' -> (tipe='uu', nomor='1', tahun=2026)."""
    m = re.match(r"^([a-z]+(?:-[a-z]+)*?)-?(\d+)?-?(\d{4})$", slug)
    if m:
        tipe = m.group(1) or ""
        nomor = m.group(2) or ""
        tahun = int(m.group(3))
        return tipe, nomor, tahun
    m2 = re.match(r"^([a-z]+)", slug)
    return (m2.group(1) if m2 else slug), "", None


# ---------------------------------------------------------------- katalog
def bangun_katalog(con: sqlite3.Connection, refresh: bool = False) -> int:
    log.info("=" * 60)
    log.info("TAHAP 1 — Membangun katalog dari sitemap resmi")
    log.info("=" * 60)
    SITEMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)

    if refresh or not SITEMAP_CACHE.exists():
        log.info("  mengunduh sitemap.xml ...")
        st, body = http_get(f"{SITE}/sitemap.xml", timeout=90)
        if st != 200 or not body:
            log.error(f"  gagal unduh sitemap (status {st})")
            return 0
        SITEMAP_CACHE.write_bytes(body)
        log.info(f"  sitemap tersimpan ({len(body)/1024:.0f} KB)")

    xml = SITEMAP_CACHE.read_text(encoding="utf-8", errors="replace")
    locs = re.findall(r"<loc>(.*?)</loc>", xml)
    log.info(f"  {len(locs)} URL di sitemap")

    rows, seen = [], set()
    for loc in locs:
        path = urllib.parse.urlparse(loc).path
        m = SLUG_RE.match(path)
        if not m:
            continue
        doc_id = m.group("id")
        if doc_id in seen:
            continue
        seen.add(doc_id)
        slug = m.group("slug")
        tipe, nomor, tahun = parse_slug(slug)
        rows.append((doc_id, slug, tipe, nomor, tahun, loc))

    con.executemany(
        """INSERT INTO dokumen (id, slug, tipe, nomor, tahun, url, status)
           VALUES (?,?,?,?,?,?, 'katalog')
           ON CONFLICT(id) DO UPDATE SET
             slug=excluded.slug, tipe=excluded.tipe,
             nomor=excluded.nomor, tahun=excluded.tahun, url=excluded.url""",
        rows)
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM dokumen").fetchone()[0]
    log.info(f"  katalog: {len(rows)} dokumen diproses, total di DB = {n}")
    return len(rows)


# ---------------------------------------------------------------- unduh
def ambil_daftar(con, tipe_filter=None, limit=None) -> list[tuple]:
    """Ambil daftar dokumen yang belum diunduh.

    Dedupe per `slug`: katalog JDIH memuat banyak entri kembar (judul sama,
    ID berbeda) — tanpa dedupe, ratusan slot terbuang untuk dokumen yang sama.
    """
    inner = "status='katalog'"
    p = []
    # tipe efektif = tipe_benar (hasil klasifikasi ulang dari slug) bila ada,
    # jika tidak jatuh ke kolom `tipe` yang lama. Kolom `tipe` TIDAK diubah,
    # jadi perilaku lama tetap utuh untuk baris yang belum diklasifikasi ulang.
    # (ditambahkan 2026-09-22: 39.789 baris katalog tersembunyi karena kolom
    #  `tipe` berisi kata pertama judul, bukan jenis regulasi)
    tipe_ekspresi = "COALESCE(d.tipe_benar, d.tipe)"
    # untuk subquery dalam (tanpa alias `d`) — pakai nama kolom polos
    tipe_polos = "COALESCE(tipe_benar, tipe)"
    if tipe_filter:
        inner += f" AND {tipe_polos} IN ({','.join('?' * len(tipe_filter))})"
        p += list(tipe_filter)

    urut = " ".join(f"WHEN '{t}' THEN {i}" for i, t in enumerate(PRIORITAS_TIPE))
    q = f"""
        SELECT d.id, d.slug, {tipe_ekspresi}, d.nomor, d.tahun, d.url
        FROM dokumen d
        JOIN (SELECT slug, MAX(id) AS mid FROM dokumen
              WHERE {inner} GROUP BY slug) x ON d.id = x.mid
        ORDER BY CASE {tipe_ekspresi} {urut} ELSE 99 END,
                 d.tahun DESC, d.id DESC"""
    if limit:
        q += f" LIMIT {int(limit)}"
    return con.execute(q, p).fetchall()


def unduh_dokumen(con, row) -> bool:
    doc_id, slug, tipe, nomor, tahun, url = row
    log.info(f"  [{tipe or '?'} {nomor or ''}/{tahun or '?'}] {slug[:60]}")

    st, body = http_get(url, timeout=45)
    if st != 200 or not body:
        con.execute("UPDATE dokumen SET status='gagal-halaman' WHERE id=?", (doc_id,))
        con.commit()
        return False

    html = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    judul = ""
    h1 = soup.find("h1")
    if h1:
        judul = h1.get_text(" ", strip=True)
    if not judul and soup.title:
        judul = re.sub(r"\s*-\s*JDIH.*$", "", soup.title.get_text(strip=True)).strip()
    if not judul:
        judul = slug

    # link PDF resmi (hindari file abstrak yang diawali "ab")
    pdf_url = None
    for a in soup.select("a[href]"):
        href = a["href"]
        if "/dokumen/download" not in href:
            continue
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        nama = (q.get("id") or [""])[0]
        if not nama or nama.lower().startswith("ab"):
            continue  # lewati abstrak
        pdf_url = href if href.startswith("http") else SITE + href
        break
    if not pdf_url:
        con.execute("UPDATE dokumen SET status='tanpa-pdf', judul=? WHERE id=?",
                    (judul, doc_id))
        con.commit()
        return False

    con.execute("UPDATE dokumen SET judul=?, pdf_url=? WHERE id=?", (judul, pdf_url, doc_id))
    con.commit()

    # unduh PDF
    nama_file = re.sub(r'[\\/*?:"<>|]', "_", f"{tipe or 'doc'}-{nomor or doc_id}-{tahun or ''}")[:70]
    pdf_path = PDF_DIR / f"{nama_file}.pdf"
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    if not pdf_path.exists():
        sukses_pdf = unduh_stream_pdf(pdf_url, pdf_path)
        if not sukses_pdf:
            con.execute("UPDATE dokumen SET status='gagal-pdf' WHERE id=?", (doc_id,))
            con.commit()
            return False
        jeda(JEDA_PDF)

    # ekstrak teks
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        log.warning(f"    PDF rusak: {e}")
        con.execute("UPDATE dokumen SET status='pdf-rusak' WHERE id=?", (doc_id,))
        con.commit()
        return False

    n_hal = doc.page_count
    bagian, total = [], 0
    for i, page in enumerate(doc):
        t = page.get_text().strip()
        if t:
            bagian.append(f"\n--- Halaman {i+1} ---\n{t}")
            total += len(t)
        if i >= 79:
            bagian.append(f"\n[... {n_hal-80} halaman berikutnya tidak diekstrak ...]")
            break
    doc.close()

    teks = "\n".join(bagian) if bagian else "(PDF hasil pindai — tidak ada lapisan teks)"

    md_path = KB_DIR / (tipe or "lain") / f"{nama_file}.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        f"# {judul}\n\n"
        f"**Tipe      :** {tipe.upper() if tipe else '-'} No. {nomor or '-'} Tahun {tahun or '-'}\n"
        f"**Kategori  :** Regulasi Nasional (JDIH Kemenkum RI)\n"
        f"**URL Sumber:** {url}\n"
        f"**PDF Resmi :** {pdf_url}\n"
        f"**Halaman   :** {n_hal}\n"
        f"**Diambil   :** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n{teks}\n",
        encoding="utf-8")

    con.execute("""UPDATE dokumen SET judul=?, pdf_path=?, md_path=?, status='selesai',
                   ukuran_pdf=?, halaman=?, panjang_teks=?, diambil=?
                   WHERE id=?""",
                (judul, str(pdf_path), str(md_path), pdf_path.stat().st_size,
                 n_hal, len(teks), datetime.now().isoformat(), doc_id))
    con.commit()
    log.info(f"    teks {len(teks)} char -> {md_path.relative_to(BASE_DIR)}")
    return True


# ---------------------------------------------------------------- main
def main():
    args = sys.argv[1:]
    limit = None
    tipe_filter = None
    if "--limit" in args:
        limit = int(args[args.index("--limit") + 1])
    if "--tipe" in args:
        tipe_filter = [t.strip() for t in args[args.index("--tipe") + 1].split(",")]

    log.info("=" * 60)
    log.info("=== JDIH Kemenkum Scraper — Mulai ===")
    log.info("=" * 60)

    con = db_init()

    if "--katalog" in args or con.execute("SELECT COUNT(*) FROM dokumen").fetchone()[0] == 0:
        bangun_katalog(con, refresh="--refresh" in args)

    # Tandai entri kembar (slug sama) sebagai duplikat agar tidak diulang-ulang.
    # Satu entri per slug tetap berstatus 'katalog' untuk diunduh.
    dup = con.execute("""
        UPDATE dokumen SET status='duplikat'
        WHERE status='katalog' AND slug IS NOT NULL AND id NOT IN (
            SELECT MAX(id) FROM dokumen WHERE status='katalog'
              AND slug IS NOT NULL GROUP BY slug)""").rowcount
    if dup:
        log.info(f"  {dup} entri kembar (slug sama) ditandai 'duplikat' — dilewati")
    con.commit()

    if "--unduh" in args:
        rows = ambil_daftar(con, tipe_filter, limit)
        log.info(f"\nTAHAP 2 — Mengunduh {len(rows)} dokumen")
        ok = 0
        for i, row in enumerate(rows, 1):
            try:
                if unduh_dokumen(con, row):
                    ok += 1
            except Exception as e:
                log.error(f"  error tak terduga: {e}")
                con.execute("UPDATE dokumen SET status='error' WHERE id=?", (row[0],))
                con.commit()
            jeda(JEDA_HALAMAN, "(antar halaman)")
            if i % ISTIRAHAT_TIAP == 0:
                jeda(ISTIRAHAT_DETIK, f"(istirahat setelah {i} dokumen)")
        log.info(f"\n=== SELESAI — {ok}/{len(rows)} dokumen berhasil ===")

    # ringkasan status
    log.info("\n--- Ringkasan DB ---")
    for st, n in con.execute("SELECT status, COUNT(*) FROM dokumen GROUP BY status ORDER BY 2 DESC"):
        log.info(f"    {st:16} {n}")
    con.close()


if __name__ == "__main__":
    main()
