#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_metadata.py — Lapisan metadata & taksonomi hukum (kelas enterprise)
======================================================================
Mengambil metadata terstruktur dari halaman detail JDIH Kemenkum dan
menyimpannya ke `knowledge_base/metadata.db` (TERPISAH dari katalog.db agar
tidak bentrok dengan Agent 1).

Yang diambil per dokumen:
  status (Berlaku/Dicabut/Diubah), tanggal penetapan & pengundangan,
  tempat terbit, bahasa, BIDANG HUKUM (taksonomi), subjek, T.E.U badan,
  pemrakarsa, sumber LN/TLN, jumlah halaman.

Pakai:
  python kb_metadata.py --limit 60          # ambil metadata N dokumen
  python kb_metadata.py --lapor             # laporan taksonomi enterprise
  python kb_metadata.py --bidang "Hukum Dagang"   # daftar dokumen 1 bidang
"""

import re
import ssl
import sys
import time
import json
import html
import random
import sqlite3
import logging
import urllib.request
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base"
DB_FILE = KB_DIR / "metadata.db"
KATALOG = KB_DIR / "katalog.db"
LOG_FILE = BASE_DIR / "kb_metadata.log"

JEDA = (4, 9)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("meta")

LABEL_RE = re.compile(
    r'<label class="dokumen-view-label">([^<]+)</label>\s*'
    r'<(?:p|div)[^>]*class="dokumen-view-value[^"]*"[^>]*>(.*?)</(?:p|div)>', re.S)
SUBJEK_RE = re.compile(r'subjek-badge[^>]*>(.*?)</span>', re.S)
TEU_RE = re.compile(r'<table[^>]*teu-table[^>]*>(.*?)</table>', re.S)
TEU_ROW = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
# Blok "Keterangan Status": badge jenis relasi + tautan + keterangan
REL_BLOK = re.compile(
    r'font-weight-600">([^<]+)</div>\s*<div class="text-dark">\s*'
    r'<a class="dokumen-view-link" href="([^"]+)"[^>]*>(.*?)</a>'
    r'(?:\s*<span class="text-muted small">\s*\((.*?)\)\s*</span>)?', re.S)
# Blok "Peraturan Terkait": daftar tautan
TERKAIT_BLOK = re.compile(
    r'Peraturan Terkait\s*</h3>\s*<ul[^>]*>(.*?)</ul>', re.S)
TERKAIT_LINK = re.compile(r'href="([^"]+)"[^>]*>(.*?)</a>', re.S)


def bersih(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip(" |")
    return re.sub(r"^[-–]\s*$", "", s).strip()


def get(url, timeout=45):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, f"__ERR__{type(e).__name__}"


def db_init():
    con = sqlite3.connect(DB_FILE)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS meta (
        id INTEGER PRIMARY KEY, tipe TEXT, nomor TEXT, tahun TEXT, judul TEXT,
        slug TEXT, url TEXT, status TEXT, tanggal_penetapan TEXT,
        tanggal_pengundangan TEXT, tahun_terbit TEXT, tempat_terbit TEXT,
        bahasa TEXT, bidang_hukum TEXT, subjek TEXT, teu_badan TEXT,
        pemrakarsa TEXT, sumber TEXT, halaman TEXT, diambil TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS relasi (
        id INTEGER PRIMARY KEY AUTOINCREMENT, dari_id INTEGER, jenis TEXT,
        ke_teks TEXT, url TEXT, keterangan TEXT)""")
    # migrasi aman bila tabel lama belum punya kolom keterangan
    cols = {r[1] for r in con.execute("PRAGMA table_info(relasi)")}
    if "keterangan" not in cols:
        con.execute("ALTER TABLE relasi ADD COLUMN keterangan TEXT")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bidang ON meta(bidang_hukum)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_status ON meta(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tipe ON meta(tipe)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tahun ON meta(tahun)")
    con.commit()
    return con


def urai(h, url):
    """Urai metadata dari HTML halaman detail JDIH."""
    d = {}
    for lbl, val in LABEL_RE.findall(h):
        lbl, val = bersih(lbl), bersih(val)
        if lbl and val:
            d[lbl] = val

    ld = {}
    m = LD_RE.search(h)
    if m:
        try:
            ld = json.loads(m.group(1))
        except Exception:
            ld = {}

    subjek = [bersih(x) for x in SUBJEK_RE.findall(h)]
    subjek = [s for s in subjek if s and s.lower() not in ("tidak ada data", "-")]

    teu = ""
    mt = TEU_RE.search(h)
    if mt:
        sel = [bersih(x) for x in TEU_ROW.findall(mt.group(1))]
        sel = [x for x in sel if x and x != ":"]
        teu = " ".join(sel)[:300]

    def ambil(*nama):
        for n in nama:
            for k, v in d.items():
                if k.lower() == n.lower():
                    return v
        return ""

    return {
        "status": ambil("Status"),
        "tanggal_penetapan": ambil("Tanggal Penetapan"),
        "tanggal_pengundangan": ambil("Tanggal Pengundangan"),
        "tahun_terbit": ambil("Tahun Terbit"),
        "tempat_terbit": ambil("Tempat Terbit"),
        "bahasa": ambil("Bahasa"),
        "bidang_hukum": ambil("Bidang Hukum"),
        "sumber": ambil("Sumber", "Sumber LN"),
        "pemrakarsa": ambil("Pemrakarsa"),
        "halaman": ambil("Jumlah Halaman", "Halaman"),
        "subjek": "; ".join(subjek)[:400],
        "teu_badan": teu,
        "judul_ld": ld.get("name", ""),
        "tanggal_ld": ld.get("legislationDate", ""),
    }


def urai_relasi(h):
    """Ambil (peraturan_terkait, keterangan_status) dari halaman detail."""
    terkait = []
    mt = TERKAIT_BLOK.search(h)
    if mt:
        blok = mt.group(1)
        if "tidak tersedia" not in blok.lower():
            for href, teks in TERKAIT_LINK.findall(blok):
                t = bersih(teks)
                if t and "tidak tersedia" not in t.lower():
                    terkait.append((t, href))
    status = []
    for jenis, href, teks, ket in REL_BLOK.findall(h):
        status.append((bersih(jenis).lower(), bersih(teks), href, bersih(ket or "")))
    return terkait, status


def proses(con, row):
    did, slug, tipe, nomor, tahun, judul, url = row
    if not url:
        return None
    if not url.startswith("http"):
        url = "https://jdih.kemenkum.go.id" + url
    st, h = get(url)
    if st != 200 or not isinstance(h, str):
        log.warning(f"  gagal ({st}) {slug}")
        return None
    m = urai(h, url)
    if not m["tanggal_penetapan"] and m["tanggal_ld"]:
        m["tanggal_penetapan"] = m["tanggal_ld"]
    if not m["judul_ld"]:
        m["judul_ld"] = judul
    con.execute("""INSERT INTO meta (id,tipe,nomor,tahun,judul,slug,url,status,
        tanggal_penetapan,tanggal_pengundangan,tahun_terbit,tempat_terbit,bahasa,
        bidang_hukum,subjek,teu_badan,pemrakarsa,sumber,halaman,diambil)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status=excluded.status,
          tanggal_penetapan=excluded.tanggal_penetapan,
          tanggal_pengundangan=excluded.tanggal_pengundangan,
          tahun_terbit=excluded.tahun_terbit, tempat_terbit=excluded.tempat_terbit,
          bahasa=excluded.bahasa, bidang_hukum=excluded.bidang_hukum,
          subjek=excluded.subjek, teu_badan=excluded.teu_badan,
          pemrakarsa=excluded.pemrakarsa, sumber=excluded.sumber,
          halaman=excluded.halaman, diambil=excluded.diambil""",
        (did, tipe, nomor, tahun, m["judul_ld"] or judul, slug, url, m["status"],
         m["tanggal_penetapan"], m["tanggal_pengundangan"], m["tahun_terbit"],
         m["tempat_terbit"], m["bahasa"], m["bidang_hukum"], m["subjek"],
         m["teu_badan"], m["pemrakarsa"], m["sumber"], m["halaman"],
         datetime.now().isoformat()))
    # --- graf relasi antar-peraturan ---
    terkait, status_rel = urai_relasi(h)
    con.execute("DELETE FROM relasi WHERE dari_id=?", (did,))
    n_rel = 0
    for jenis, teks, href, ket in status_rel:
        con.execute("""INSERT INTO relasi (dari_id,jenis,ke_teks,url,keterangan)
                       VALUES (?,?,?,?,?)""", (did, jenis, teks, href, ket))
        n_rel += 1
    for teks, href in terkait:
        con.execute("""INSERT INTO relasi (dari_id,jenis,ke_teks,url,keterangan)
                       VALUES (?,?,?,?,?)""", (did, "terkait", teks, href, ""))
        n_rel += 1

    con.commit()
    log.info(f"  [{tipe} {nomor}/{tahun}] status={m['status'] or '-'} | "
             f"bidang={m['bidang_hukum'] or '-'} | relasi={n_rel} | "
             f"{m['tanggal_penetapan'] or '-'}")
    return m


def lapor(con):
    print("\n" + "=" * 66)
    print("  TAKSONOMI HUKUM — LAPORAN KELAS ENTERPRISE")
    print("=" * 66)
    tot = con.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    print(f"\n  Dokumen ber-metadata : {tot:,}")

    print("\n  -- Sebaran BIDANG HUKUM (taksonomi) --")
    for b, n in con.execute("""SELECT COALESCE(NULLIF(bidang_hukum,''),'(belum diisi)'),
                               COUNT(*) c FROM meta GROUP BY 1 ORDER BY c DESC LIMIT 25"""):
        print(f"     {b[:46]:48} {n:>6,}")

    print("\n  -- Sebaran STATUS --")
    for s, n in con.execute("""SELECT COALESCE(NULLIF(status,''),'(kosong)'), COUNT(*)
                               FROM meta GROUP BY 1 ORDER BY 2 DESC LIMIT 10"""):
        print(f"     {s[:30]:32} {n:>6,}")

    print("\n  -- Sebaran per TIPE --")
    for t, n in con.execute("SELECT tipe, COUNT(*) FROM meta GROUP BY 1 ORDER BY 2 DESC LIMIT 15"):
        print(f"     {t:12} {n:>6,}")

    print("\n  -- Kelengkapan field (%) --")
    for f in ["tanggal_penetapan", "tanggal_pengundangan", "tempat_terbit",
              "bahasa", "bidang_hukum", "subjek", "teu_badan", "sumber"]:
        n = con.execute(f"SELECT COUNT(*) FROM meta WHERE COALESCE({f},'')<>''").fetchone()[0]
        print(f"     {f:22} {n*100//max(tot,1):>3}%  ({n:,})")

    print("\n  -- Graf RELASI antar-peraturan --")
    nr = con.execute("SELECT COUNT(*) FROM relasi").fetchone()[0]
    print(f"     total relasi: {nr:,}")
    for jenis, n in con.execute("SELECT jenis, COUNT(*) FROM relasi GROUP BY 1 ORDER BY 2 DESC LIMIT 8"):
        print(f"     {jenis:22} {n:>6,}")
    print("\n  contoh:")
    for a, jenis, b in con.execute("""SELECT m.judul, r.jenis, r.ke_teks FROM relasi r
                                      JOIN meta m ON m.id=r.dari_id LIMIT 6"""):
        print(f"     {a[:40]:42} --{jenis}--> {b[:46]}")
    print()


def main():
    args = sys.argv[1:]
    con = db_init()

    if "--lapor" in args:
        lapor(con)
        return

    if "--bidang" in args:
        b = args[args.index("--bidang") + 1]
        print(f"\n  Dokumen bidang '{b}':")
        for j, t, n, th in con.execute("""SELECT judul,tipe,nomor,tahun FROM meta
                                          WHERE bidang_hukum LIKE ? ORDER BY tahun DESC LIMIT 40""",
                                       (f"%{b}%",)):
            print(f"    [{t} {n}/{th}] {j[:78]}")
        return

    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 60
    status = args[args.index("--status") + 1].split(",") if "--status" in args else ["selesai"]
    ulang = "--ulang" in args  # paksa proses ulang (mis. untuk mengisi graf relasi)

    kat = sqlite3.connect(KATALOG)
    sudah = set() if ulang else {r[0] for r in con.execute("SELECT id FROM meta")}
    q = f"""SELECT id, slug, tipe, nomor, tahun, judul, url FROM dokumen
            WHERE status IN ({','.join('?'*len(status))}) AND url IS NOT NULL
            ORDER BY id"""
    rows = [r for r in kat.execute(q, status) if r[0] not in sudah][:limit]
    log.info("=" * 60)
    log.info(f"=== Metadata JDIH — {len(rows)} dokumen (status={','.join(status)}) ===")
    log.info("=" * 60)

    ok = 0
    for i, r in enumerate(rows, 1):
        try:
            if proses(con, r):
                ok += 1
        except Exception as e:
            log.error(f"  error: {type(e).__name__}: {e}")
        if i % 25 == 0:
            log.info(f"  ... istirahat (sudah {i})")
            time.sleep(random.uniform(60, 120))
        else:
            time.sleep(random.uniform(*JEDA))

    log.info(f"\n=== SELESAI — {ok}/{len(rows)} metadata tersimpan ===")
    log.info(f"    total meta di DB: "
             f"{con.execute('SELECT COUNT(*) FROM meta').fetchone()[0]:,}")
    lapor(con)


if __name__ == "__main__":
    main()
