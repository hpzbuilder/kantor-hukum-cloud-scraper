#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
harvester_bpk.py — Pemanen Peraturan & Putusan Uji Materi MK dari JDIH BPK RI
==============================================================================
Sumber: https://peraturan.bpk.go.id (robots.txt mengizinkan /Details, /Download,
/DownloadUjiMateri; yang dilarang hanya /Admin, /Identity, /Account, /Manage).

Cara kerja (urut dari yang TERBARU):
  1. Antrean diisi dari sitemap resmi per tahun (sitemap-peraturan-YYYY.xml).
  2. Prioritas: tier 1 (UUD/UU/Perppu/PP/Perpres) → tier 2 (Inpres/Keppres/
     Perma/PBI/POJK/peraturan lembaga) → tier 3 (peraturan menteri & badan).
     Peraturan daerah dilewati kecuali --tingkat semua.
  3. Tiap dokumen: metadata lengkap (status berlaku, subjek, bidang, tanggal),
     relasi (mengubah/dicabut/...), daftar putusan uji materi MK + amar,
     unduh PDF resmi, ekstrak teks penuh (PyMuPDF) → .md siap indeks FTS5.
  4. PDF putusan uji materi MK ikut diunduh & diekstrak (Putusan_MK/BPK_UjiMateri).

Kejujuran data:
  - Status 'selesai' HANYA bila teks penuh berhasil diekstrak.
  - PDF hasil pindai tanpa teks → 'teks-kosong' (perlu OCR), bukan 'selesai'.
  - Bila situs memblokir (403/429 berturut-turut) → berhenti, exit code 3.
    IP datacenter (GitHub Actions) DIBLOKIR Cloudflare BPK; jalankan dari
    komputer lokal.

Pakai:
  python harvester_bpk.py --kb "D:/.../Kantor Hukum Virtual/knowledge_base" --limit 40
  python harvester_bpk.py --kb ... --tahun-mulai 2026 --tahun-akhir 2015 --limit 100
  python harvester_bpk.py --kb ... --statistik
"""

import argparse
import hashlib
import logging
import random
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://peraturan.bpk.go.id"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
MAKS_PDF_MB = 80
EXIT_DIBLOKIR = 3

log = logging.getLogger("harvester_bpk")

# ------------------------------------------------------------------ prioritas
TIER1 = ("uud", "uu", "perppu", "pp", "perpres")
TIER2 = ("inpres", "keppres", "perma", "peraturan-ma", "peraturan-mk", "pbi",
         "peraturan-bi", "pojk", "peraturan-ojk", "peraturan-kpu", "peraturan-bawaslu",
         "peraturan-bpk", "peraturan-ky", "peraturan-dpr", "peraturan-mpr", "peraturan-dpd")
POLA_DAERAH = re.compile(r"(^|-)(kab|kota|prov)(-|$)|^(perda|perbup|perwali|pergub|perkada|qanun)")


POLA_PROVINSI = re.compile(r"^(pergub|perda-prov|perdasus|perdasi|qanun-prov|perkada-prov)|(^|-)prov(-|$)")


def tier_daerah(slug: str) -> int:
    """Peraturan daerah: 8 = tingkat provinsi (diprioritaskan), 9 = kabupaten/kota/desa."""
    return 8 if POLA_PROVINSI.search(slug.split("/")[-1]) else 9


def tier_dari_slug(slug: str) -> int | None:
    """None = dilewati (peraturan daerah). Lebih kecil = lebih penting."""
    nama = slug.split("/")[-1]
    m = re.match(r"(.+?)-no-", nama)
    prefix = m.group(1) if m else nama.split("-")[0]
    if POLA_DAERAH.search(prefix):
        return None
    if prefix in TIER1:
        return 1
    if prefix in TIER2 or any(prefix.startswith(p) for p in TIER2):
        return 2
    if prefix.startswith("keputusan"):
        return 4
    return 3


# ------------------------------------------------------------------ HTTP
class Diblokir(Exception):
    pass


class Klien:
    def __init__(self, jeda=(2.0, 4.0)):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept-Language": "id,en;q=0.8"})
        self.jeda = jeda
        self.gagal_beruntun = 0

    def tidur(self, faktor=1.0):
        time.sleep(random.uniform(*self.jeda) * faktor)

    def get(self, url, stream=False, timeout=45):
        try:
            r = self.s.get(url, timeout=timeout, stream=stream)
        except requests.RequestException as e:
            self.gagal_beruntun += 1
            log.warning(f"  koneksi gagal: {type(e).__name__} {url}")
            if self.gagal_beruntun >= 3:
                raise Diblokir(f"3 kegagalan koneksi beruntun ({e})")
            return None
        if r.status_code in (403, 429) or (r.status_code == 503 and "just a moment" in r.text[:3000].lower()):
            self.gagal_beruntun += 1
            log.warning(f"  HTTP {r.status_code} {url}")
            if self.gagal_beruntun >= 3:
                raise Diblokir(f"HTTP {r.status_code} beruntun — kemungkinan IP diblokir")
            self.tidur(3)
            return None
        self.gagal_beruntun = 0
        return r


# ------------------------------------------------------------------ DB
SKEMA = """
CREATE TABLE IF NOT EXISTS peraturan (
    bpk_id INTEGER PRIMARY KEY,
    slug TEXT, url TEXT, tier INTEGER, tahun_sitemap INTEGER, lastmod TEXT,
    bentuk TEXT, bentuk_singkat TEXT, nomor TEXT, tahun TEXT, judul TEXT,
    teu TEXT, tempat_penetapan TEXT, tgl_penetapan TEXT, tgl_pengundangan TEXT,
    tgl_berlaku TEXT, sumber TEXT, subjek TEXT, status_berlaku TEXT, bidang TEXT,
    materi_pokok TEXT, pdf_url TEXT, pdf_path TEXT, md_path TEXT,
    halaman INTEGER, panjang_teks INTEGER, sha256 TEXT,
    status TEXT DEFAULT 'antre', catatan TEXT, diambil TEXT
);
CREATE INDEX IF NOT EXISTS idx_per_antre ON peraturan(status, tier, tahun_sitemap);
CREATE TABLE IF NOT EXISTS relasi (
    bpk_id INTEGER, jenis TEXT, target_bpk_id INTEGER, target_label TEXT,
    target_judul TEXT, keterangan TEXT,
    UNIQUE(bpk_id, jenis, target_bpk_id, target_label)
);
CREATE TABLE IF NOT EXISTS uji_materi (
    um_id INTEGER PRIMARY KEY, bpk_id INTEGER, label_uu TEXT, nomor_putusan TEXT,
    amar_ringkas TEXT, pdf_url TEXT, pdf_path TEXT, md_path TEXT,
    halaman INTEGER, panjang_teks INTEGER, sha256 TEXT,
    status TEXT DEFAULT 'antre', diambil TEXT
);
CREATE TABLE IF NOT EXISTS sitemap_tahun (
    tahun INTEGER PRIMARY KEY, total INTEGER, diantrekan INTEGER, dibaca_pada TEXT
);
"""


def db_buka(kb: Path) -> sqlite3.Connection:
    con = sqlite3.connect(kb / "bpk.db", timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SKEMA)
    return con


# ------------------------------------------------------------------ sitemap
def isi_antrean(con, klien, tahun_mulai, tahun_akhir, semua_tingkat, segarkan_hari=3):
    for th in range(tahun_mulai, tahun_akhir - 1, -1):
        baris = con.execute("SELECT dibaca_pada FROM sitemap_tahun WHERE tahun=?", (th,)).fetchone()
        tahun_berjalan = th >= datetime.now().year - 1
        if baris and not tahun_berjalan:
            continue
        if baris and tahun_berjalan:
            umur = (datetime.now() - datetime.fromisoformat(baris[0])).days
            if umur < segarkan_hari:
                continue
        r = klien.get(f"{BASE}/sitemap-peraturan-{th}.xml", timeout=90)
        if r is None or r.status_code != 200:
            log.warning(f"  sitemap {th} tidak terbaca")
            continue
        blok = re.findall(r"<url>(.*?)</url>", r.text, re.S)
        n_baru = 0
        for b in blok:
            loc = re.search(r"<loc>(.*?)</loc>", b)
            if not loc:
                continue
            url = loc.group(1).strip()
            m = re.search(r"/Details/(\d+)/([^/?#]+)", url)
            if not m:
                continue
            tier = tier_dari_slug(m.group(2))
            if tier is None and not semua_tingkat:
                continue
            lm = re.search(r"<lastmod>(.*?)</lastmod>", b)
            cur = con.execute(
                "INSERT OR IGNORE INTO peraturan (bpk_id, slug, url, tier, tahun_sitemap, lastmod) "
                "VALUES (?,?,?,?,?,?)",
                (int(m.group(1)), m.group(2), url, tier or tier_daerah(m.group(2)), th, lm.group(1) if lm else None))
            n_baru += cur.rowcount
        con.execute("INSERT OR REPLACE INTO sitemap_tahun VALUES (?,?,?,?)",
                    (th, len(blok), n_baru, datetime.now().isoformat(timespec="seconds")))
        con.commit()
        log.info(f"  sitemap {th}: {len(blok)} entri, {n_baru} baru masuk antrean")
        klien.tidur(0.5)


# ------------------------------------------------------------------ parsing
def teks(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def parse_detail(html):
    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    for lab in soup.select("div.col-lg-3.fw-bold"):
        nilai = lab.find_next_sibling("div")
        if nilai is not None:
            meta[teks(lab)] = teks(nilai)

    materi = ""
    relasi, uji = [], []
    for h in soup.find_all("h4"):
        judul_h = teks(h).upper()
        card = h.find_parent("div", class_="card")
        if card is None:
            continue
        if "MATERI POKOK" in judul_h:
            badan = card.select_one("div.container") or card
            materi = teks(badan)
        elif "STATUS" in judul_h:
            jenis = None
            for el in card.select("div.col-12.fw-semibold, li"):
                if el.name == "div":
                    jenis = teks(el).rstrip(" :").strip()
                    continue
                a = el.find("a", href=True)
                if not a or not jenis:
                    continue
                mid = re.search(r"/Details/(\d+)", a["href"])
                ket = el.find("br")
                ket_teks = teks(ket.find_next("span")) if ket else ""
                judul_t = teks(el).replace(teks(a), "", 1).replace("tentang", "", 1).strip()
                if ket_teks:
                    judul_t = judul_t.replace(ket_teks, "").strip()
                relasi.append((jenis, int(mid.group(1)) if mid else None, teks(a), judul_t[:500], ket_teks[:1000]))
        elif "UJI MATERI" in judul_h:
            for a in card.select("a.download-file[data-kategori=UjiMateri]"):
                blok = a.find_parent("div", class_="row")
                amar = teks(blok.select_one("div.col-12 + div.col-12 span")) if blok else ""
                if not amar and blok:
                    sp = blok.find_all("span")
                    amar = teks(sp[-1]) if sp else ""
                uji.append({"um_id": int(a.get("data-id")), "nomor": teks(a),
                            "label_uu": a.get("data-label", ""), "pdf_url": BASE + a["href"],
                            "amar": amar})

    pdf = None
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("/Download/"):
            pdf = BASE + a["href"]
            break
    return meta, materi, relasi, uji, pdf


# ------------------------------------------------------------------ PDF
LIGATUR = {"\u019f": "ti", "\ufb01": "fi", "\ufb02": "fl", "\ufb00": "ff", "\ufb03": "ffi", "\ufb04": "ffl"}


def unduh_pdf(klien, url, tujuan: Path):
    r = klien.get(url, stream=True, timeout=120)
    if r is None or r.status_code != 200:
        return False, f"HTTP {getattr(r, 'status_code', 'none')}"
    tujuan.parent.mkdir(parents=True, exist_ok=True)
    tmp = tujuan.with_suffix(".part")
    total = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(256 * 1024):
            total += len(chunk)
            if total > MAKS_PDF_MB * 1024 * 1024:
                f.close()
                tmp.unlink(missing_ok=True)
                return False, f"lebih dari {MAKS_PDF_MB} MB"
            f.write(chunk)
    with open(tmp, "rb") as f:
        if f.read(5) != b"%PDF-":
            tmp.unlink(missing_ok=True)
            return False, "bukan PDF"
    tmp.replace(tujuan)
    return True, ""


def ekstrak_pdf(path: Path):
    import fitz
    doc = fitz.open(path)
    bagian = []
    for i, hal in enumerate(doc, 1):
        t = hal.get_text("text")
        for k, v in LIGATUR.items():
            t = t.replace(k, v)
        bagian.append(f"--- Halaman {i} ---\n{t.strip()}")
    n = doc.page_count
    doc.close()
    isi = "\n\n".join(bagian)
    return isi, n


def sha256(path: Path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def nama_aman(s, maks=90):
    return re.sub(r"[^a-zA-Z0-9_.\-]+", "_", s)[:maks].strip("_")


# ------------------------------------------------------------------ dedup JDIH
PETA_JDIH = {"uu": "uu", "pp": "pp", "perpres": "perpres", "perppu": "perppu",
             "inpres": "inpres", "keppres": "keppres"}


def sudah_di_jdih(katalog, bentuk_singkat, nomor, tahun):
    if katalog is None:
        return None
    tipe = PETA_JDIH.get((bentuk_singkat or "").lower())
    if not tipe or not nomor or not tahun:
        return None
    r = katalog.execute(
        "SELECT md_path FROM dokumen WHERE tipe=? AND nomor=? AND tahun=? AND status='selesai' LIMIT 1",
        (tipe, str(nomor).strip(), str(tahun).strip())).fetchone()
    return r[0] if r else None


# ------------------------------------------------------------------ proses
def proses_peraturan(con, klien, kb: Path, row, katalog, simpan_pdf_tier=1):
    bpk_id, slug, url, tier = row
    sekarang = datetime.now().isoformat(timespec="seconds")
    r = klien.get(url)
    if r is None or r.status_code != 200:
        con.execute("UPDATE peraturan SET status='gagal-halaman', diambil=? WHERE bpk_id=?", (sekarang, bpk_id))
        return "gagal-halaman"
    meta, materi, relasi, uji, pdf_url = parse_detail(r.text)
    g = meta.get
    judul = g("Judul") or slug
    bentuk_s = g("Bentuk Singkat")
    nomor, tahun = g("Nomor"), g("Tahun")

    con.execute("""UPDATE peraturan SET bentuk=?, bentuk_singkat=?, nomor=?, tahun=?, judul=?, teu=?,
        tempat_penetapan=?, tgl_penetapan=?, tgl_pengundangan=?, tgl_berlaku=?, sumber=?, subjek=?,
        status_berlaku=?, bidang=?, materi_pokok=?, pdf_url=?, diambil=? WHERE bpk_id=?""",
                (g("Bentuk"), bentuk_s, nomor, tahun, judul, g("T.E.U."), g("Tempat Penetapan"),
                 g("Tanggal Penetapan"), g("Tanggal Pengundangan"), g("Tanggal Berlaku"), g("Sumber"),
                 g("Subjek"), g("Status"), g("Bidang"), materi[:4000], pdf_url, sekarang, bpk_id))
    for jenis, tid, tlabel, tjudul, ket in relasi:
        con.execute("INSERT OR IGNORE INTO relasi VALUES (?,?,?,?,?,?)", (bpk_id, jenis, tid, tlabel, tjudul, ket))
    for u in uji:
        con.execute("""INSERT OR IGNORE INTO uji_materi (um_id, bpk_id, label_uu, nomor_putusan, amar_ringkas, pdf_url)
            VALUES (?,?,?,?,?,?)""", (u["um_id"], bpk_id, u["label_uu"], u["nomor"], u["amar"][:4000], u["pdf_url"]))

    dup = sudah_di_jdih(katalog, bentuk_s, nomor, tahun)
    if dup:
        con.execute("UPDATE peraturan SET status='duplikat-jdih', md_path=?, catatan='teks sudah ada dari JDIH Kemenkum; metadata & relasi BPK disimpan' WHERE bpk_id=?",
                    (dup, bpk_id))
        con.commit()
        return "duplikat-jdih"
    if not pdf_url:
        con.execute("UPDATE peraturan SET status='tanpa-pdf' WHERE bpk_id=?", (bpk_id,))
        con.commit()
        return "tanpa-pdf"

    folder = (bentuk_s or "lain").lower().replace(" ", "_")
    sub = kb / "Regulasi_Nasional" / "BPK" / nama_aman(folder, 30) / str(tahun or "tanpa-tahun")
    pdf_path = sub / f"{bpk_id}_{nama_aman(slug, 70)}.pdf"
    klien.tidur()
    ok, alasan = unduh_pdf(klien, pdf_url, pdf_path)
    if not ok:
        con.execute("UPDATE peraturan SET status='gagal-pdf', catatan=? WHERE bpk_id=?", (alasan, bpk_id))
        con.commit()
        return "gagal-pdf"
    try:
        isi, n_hal = ekstrak_pdf(pdf_path)
    except Exception as e:
        con.execute("UPDATE peraturan SET status='gagal-pdf', pdf_path=?, catatan=? WHERE bpk_id=?",
                    (str(pdf_path.relative_to(kb)), f"ekstraksi: {e}"[:300], bpk_id))
        con.commit()
        return "gagal-pdf"
    murni = re.sub(r"--- Halaman \d+ ---|\s", "", isi)
    status = "selesai" if len(murni) >= max(300, 150 * n_hal * 0.3) else "teks-kosong"

    md_path = pdf_path.with_suffix(".md")
    relasi_md = "\n".join(f"- **{j}** {tl} — {tj}" + (f" _({k})_" if k else "") for j, _, tl, tj, k in relasi)
    uji_md = "\n".join(f"- Putusan MK {u['nomor']}: {u['amar'][:600]}" for u in uji)
    bentuk_tampil = g("Bentuk") or bentuk_s or "-"
    if bentuk_s and f"({bentuk_s})" not in bentuk_tampil:
        bentuk_tampil += f" ({bentuk_s})"
    kepala = f"""# {judul}

**Tipe      :** {bentuk_tampil} No. {nomor or '-'} Tahun {tahun or '-'}
**Kategori  :** Regulasi Nasional (JDIH BPK RI)
**Status    :** {g('Status') or '-'}
**Ditetapkan:** {g('Tanggal Penetapan') or '-'} | **Diundangkan:** {g('Tanggal Pengundangan') or '-'} | **Berlaku:** {g('Tanggal Berlaku') or '-'}
**Sumber    :** {g('Sumber') or '-'}
**Subjek    :** {g('Subjek') or '-'} | **Bidang:** {g('Bidang') or '-'}
**URL Sumber:** {url}
**PDF Resmi :** {pdf_url}
**Halaman   :** {n_hal}
**Diambil   :** {datetime.now().strftime('%Y-%m-%d %H:%M')}
"""
    if materi:
        kepala += f"\n## Materi Pokok\n{materi[:3000]}\n"
    if relasi_md:
        kepala += f"\n## Status & Relasi\n{relasi_md}\n"
    if uji_md:
        kepala += f"\n## Uji Materi (Mahkamah Konstitusi)\n{uji_md}\n"
    if status == "teks-kosong":
        kepala += "\n> CATATAN: PDF hasil pindai — teks belum terekstrak (perlu OCR).\n"
    md_path.write_text(kepala + "\n---\n\n" + isi, encoding="utf-8")

    hash_pdf = sha256(pdf_path)
    pdf_simpan = str(pdf_path.relative_to(kb))
    catatan = None
    if tier > simpan_pdf_tier and status == "selesai":
        # Hemat disk: PDF ~98% ukuran. Teks + sha256 + URL resmi cukup untuk tier rendah;
        # PDF pindaian (teks-kosong) tetap disimpan untuk OCR.
        pdf_path.unlink(missing_ok=True)
        pdf_simpan, catatan = None, "PDF tidak disimpan (hemat disk); unduh ulang dari pdf_url, cek sha256"
    con.execute("""UPDATE peraturan SET pdf_path=?, md_path=?, halaman=?, panjang_teks=?, sha256=?, status=?,
        catatan=? WHERE bpk_id=?""", (pdf_simpan, str(md_path.relative_to(kb)), n_hal,
                                      len(murni), hash_pdf, status, catatan, bpk_id))
    con.commit()
    return status


def proses_uji_materi(con, klien, kb: Path, batas):
    rows = con.execute("""SELECT u.um_id, u.nomor_putusan, u.label_uu, u.amar_ringkas, u.pdf_url, p.judul
        FROM uji_materi u LEFT JOIN peraturan p ON p.bpk_id=u.bpk_id
        WHERE u.status='antre' ORDER BY u.um_id DESC LIMIT ?""", (batas,)).fetchall()
    hasil = {}
    for um_id, nomor, label, amar, pdf_url, judul_uu in rows:
        # Lampiran "Uji Materi" BPK memuat putusan MK (PUU/SKLN...) DAN putusan MA (hak uji materiil: "12 P/HUM/2024").
        ma = bool(re.search(r"HUM", nomor, re.I))
        lembaga = "Mahkamah Agung" if ma else "Mahkamah Konstitusi"
        sub = kb / ("Putusan_MA" if ma else "Putusan_MK") / "BPK_UjiMateri"
        pdf_path = sub / f"um{um_id}_{nama_aman(nomor.replace('/', '-'), 60)}.pdf"
        klien.tidur()
        ok, alasan = unduh_pdf(klien, pdf_url, pdf_path)
        sekarang = datetime.now().isoformat(timespec="seconds")
        if not ok:
            con.execute("UPDATE uji_materi SET status='gagal-pdf', diambil=? WHERE um_id=?", (sekarang, um_id))
            hasil["gagal-pdf"] = hasil.get("gagal-pdf", 0) + 1
            continue
        try:
            isi, n_hal = ekstrak_pdf(pdf_path)
        except Exception:
            con.execute("UPDATE uji_materi SET status='gagal-pdf', diambil=? WHERE um_id=?", (sekarang, um_id))
            continue
        murni = re.sub(r"--- Halaman \d+ ---|\s", "", isi)
        status = "selesai" if len(murni) >= 300 else "teks-kosong"
        md_path = pdf_path.with_suffix(".md")
        md_path.write_text(f"""# Putusan {lembaga} Nomor {nomor}

**Tipe      :** {"Putusan MA (Hak Uji Materiil)" if ma else "Putusan MK (Pengujian Undang-Undang)"}
**Kategori  :** Putusan {lembaga} (via JDIH BPK RI)
**Peraturan Diuji:** {label} — {judul_uu or '-'}
**Amar (ringkas BPK):** {amar or '-'}
**PDF Resmi :** {pdf_url}
**Halaman   :** {n_hal}
**Diambil   :** {datetime.now().strftime('%Y-%m-%d %H:%M')}

---

{isi}""", encoding="utf-8")
        con.execute("""UPDATE uji_materi SET pdf_path=?, md_path=?, halaman=?, panjang_teks=?, sha256=?,
            status=?, diambil=? WHERE um_id=?""", (str(pdf_path.relative_to(kb)), str(md_path.relative_to(kb)),
                                                  n_hal, len(murni), sha256(pdf_path), status, sekarang, um_id))
        con.commit()
        hasil[status] = hasil.get(status, 0) + 1
        log.info(f"  [UM {status}] Putusan {'MA' if ma else 'MK'} {nomor} ({n_hal} hlm)")
    return hasil


# ------------------------------------------------------------------ main
def statistik(con):
    print("== bpk.db — peraturan per status ==")
    for st, n in con.execute("SELECT status, COUNT(*) FROM peraturan GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {st:15} {n:>7}")
    print("== antrean per tier (belum diproses) ==")
    for t, n in con.execute("SELECT tier, COUNT(*) FROM peraturan WHERE status='antre' GROUP BY 1 ORDER BY 1"):
        print(f"   tier {t}: {n}")
    print("== teks penuh 'selesai' per bentuk ==")
    for b, n in con.execute("SELECT bentuk_singkat, COUNT(*) FROM peraturan WHERE status='selesai' GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
        print(f"   {b or '-':15} {n:>6}")
    print("== putusan uji materi MK ==")
    for st, n in con.execute("SELECT status, COUNT(*) FROM uji_materi GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {st:15} {n:>7}")
    print(f"== relasi tersimpan: {con.execute('SELECT COUNT(*) FROM relasi').fetchone()[0]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kb", default=str(Path(__file__).resolve().parent / "knowledge_base"))
    ap.add_argument("--limit", type=int, default=40, help="jumlah peraturan diproses per run")
    ap.add_argument("--limit-uji-materi", type=int, default=10)
    ap.add_argument("--tahun-mulai", type=int, default=datetime.now().year)
    ap.add_argument("--tahun-akhir", type=int, default=2015)
    ap.add_argument("--tier-min", type=int, default=1,
                    help="tier terendah yang diproses. Pembagian tugas 2 laptop: L1 = 1..2, L2 = 3..9")
    ap.add_argument("--tier-maks", type=int, default=3,
                    help="1=UU/Perppu/PP/Perpres, 2=+Inpres/Keppres/Perma/POJK/PBI, 3=+peraturan menteri/badan, "
                         "4=+keputusan, 8=+peraturan provinsi, 9=+kabupaten/kota/desa (8-9 butuh --tingkat semua)")
    ap.add_argument("--tingkat", choices=["pusat", "semua"], default="pusat")
    ap.add_argument("--katalog", default=None, help="katalog.db JDIH untuk dedup (default: <kb>/katalog.db)")
    ap.add_argument("--jeda-min", type=float, default=2.0)
    ap.add_argument("--jeda-maks", type=float, default=4.0)
    ap.add_argument("--simpan-pdf-tier", type=int, default=1,
                    help="simpan PDF hanya untuk tier <= N (default 1: UU/Perppu/PP/Perpres); 9 = semua")
    ap.add_argument("--log", default=None, help="berkas log (default: <induk --kb>/harvester_bpk.log). "
                    "Beri nama berbeda bila menjalankan beberapa proses paralel.")
    ap.add_argument("--statistik", action="store_true")
    a = ap.parse_args()

    kb = Path(a.kb).resolve()
    kb.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(Path(a.log) if a.log else kb.parent / "harvester_bpk.log", encoding="utf-8")])
    con = db_buka(kb)
    if a.statistik:
        statistik(con)
        return 0

    kpath = Path(a.katalog) if a.katalog else kb / "katalog.db"
    katalog = sqlite3.connect(f"file:{kpath}?mode=ro", uri=True) if kpath.exists() else None

    klien = Klien((a.jeda_min, a.jeda_maks))
    hitung = {}
    kode = 0
    try:
        log.info(f"=== BPK: isi antrean dari sitemap {a.tahun_mulai}→{a.tahun_akhir} ===")
        isi_antrean(con, klien, a.tahun_mulai, a.tahun_akhir, a.tingkat == "semua")
        rows = con.execute("""SELECT bpk_id, slug, url, tier FROM peraturan
            WHERE status='antre' AND tier BETWEEN ? AND ? AND tahun_sitemap BETWEEN ? AND ?
            ORDER BY tier, tahun_sitemap DESC, bpk_id DESC LIMIT ?""",
                           (a.tier_min, a.tier_maks, a.tahun_akhir, a.tahun_mulai, a.limit)).fetchall()
        log.info(f"=== BPK: memproses {len(rows)} peraturan (urut terbaru, prioritas UU/PP/Perpres) ===")
        for i, row in enumerate(rows, 1):
            st = proses_peraturan(con, klien, kb, row, katalog, a.simpan_pdf_tier)
            hitung[st] = hitung.get(st, 0) + 1
            log.info(f"  [{i}/{len(rows)}] {st:14} {row[1]}")
            klien.tidur()
        if a.limit_uji_materi > 0:
            log.info("=== BPK: putusan uji materi MK ===")
            for k, v in proses_uji_materi(con, klien, kb, a.limit_uji_materi).items():
                hitung[f"uji-materi:{k}"] = v
    except Diblokir as e:
        log.error(f"✘ DIBLOKIR: {e}. Jalankan dari komputer lokal (IP residensial).")
        kode = EXIT_DIBLOKIR
    finally:
        con.commit()

    log.info("=== RINGKASAN RUN (dihitung dari hasil nyata) ===")
    for k, v in sorted(hitung.items()):
        log.info(f"   {k:24} {v}")
    n_selesai = con.execute("SELECT COUNT(*) FROM peraturan WHERE status='selesai'").fetchone()[0]
    n_um = con.execute("SELECT COUNT(*) FROM uji_materi WHERE status='selesai'").fetchone()[0]
    log.info(f"   TOTAL bpk.db: {n_selesai} peraturan teks penuh, {n_um} putusan MK teks penuh")
    con.close()
    return kode


if __name__ == "__main__":
    sys.exit(main())
