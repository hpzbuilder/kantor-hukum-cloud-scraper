#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kb_index.py — Indeks pencarian & API query untuk database Kantor Hukum Virtual
==============================================================================
Membuat indeks FTS5 (full-text search) atas seluruh knowledge_base sehingga
aplikasi/agent bisa mencari cepat lintas ribuan dokumen.

Pakai:
  python kb_index.py --build              # (re)bangun indeks
  python kb_index.py --cari "kepailitan"  # cari dokumen
  python kb_index.py --stat               # statistik database
  python kb_index.py --json "korupsi"     # output JSON (untuk aplikasi)
"""

import re
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base"
DB_FILE = KB_DIR / "katalog.db"
IDX_FILE = KB_DIR / "indeks.db"
JDB = KB_DIR / "jurnal.db"


def _koneksi_katalog():
    if not DB_FILE.exists():
        print("katalog.db belum ada — jalankan scraper_jdih_kemenkum.py --katalog")
        sys.exit(1)
    return sqlite3.connect(DB_FILE)


def build():
    """Bangun indeks FTS5 dari semua file .md di knowledge_base."""
    IDX_FILE.parent.mkdir(parents=True, exist_ok=True)
    idx = sqlite3.connect(IDX_FILE)
    idx.execute("PRAGMA journal_mode=WAL")
    idx.execute("DROP TABLE IF EXISTS dokumen_fts")
    idx.execute("""CREATE VIRTUAL TABLE dokumen_fts USING fts5(
        path, kategori, judul, isi, tokenize='unicode61 remove_diacritics 2')""")

    n = 0
    for md in KB_DIR.rglob("*.md"):
        try:
            teks = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = md.relative_to(KB_DIR)
        kategori = rel.parts[0] if len(rel.parts) > 1 else "lain"
        m = re.match(r"^#\s*(.+)$", teks, re.M)
        judul = m.group(1).strip() if m else md.stem
        isi = teks[:200_000]
        rel_path = str(rel).replace("\\", "/")
        idx.execute("INSERT INTO dokumen_fts (path, kategori, judul, isi) VALUES (?,?,?,?)",
                    (rel_path, kategori, judul, isi))
        n += 1
        if n % 500 == 0:
            print(f"  ... {n} berkas diindeks")
    idx.commit()
    idx.close()
    print(f"Selesai: {n} berkas diindeks ke {IDX_FILE.name}")


def cari(kueri, batas=10, sebagai_json=False):
    if not IDX_FILE.exists():
        print("indeks belum ada — jalankan: python kb_index.py --build")
        sys.exit(1)
    idx = sqlite3.connect(IDX_FILE)
    try:
        rows = idx.execute("""
            SELECT path, kategori, judul,
                   snippet(dokumen_fts, 3, '[', ']', ' … ', 24) AS kutipan,
                   bm25(dokumen_fts) AS skor
            FROM dokumen_fts WHERE dokumen_fts MATCH ?
            ORDER BY skor LIMIT ?""", (kueri, batas)).fetchall()
    except sqlite3.OperationalError:
        # kueri tidak valid untuk FTS -> fallback LIKE
        rows = idx.execute("""
            SELECT path, kategori, judul, substr(isi,1,200), 0
            FROM dokumen_fts WHERE isi LIKE ? LIMIT ?""",
            (f"%{kueri}%", batas)).fetchall()
    idx.close()

    hasil = [{"path": r[0], "kategori": r[1], "judul": r[2],
              "kutipan": r[3], "skor": round(r[4], 3) if isinstance(r[4], float) else r[4]}
             for r in rows]
    if sebagai_json:
        print(json.dumps({"kueri": kueri, "jumlah": len(hasil), "hasil": hasil},
                         ensure_ascii=False, indent=1))
    else:
        if not hasil:
            print(f"Tidak ada hasil untuk: {kueri}")
        for i, h in enumerate(hasil, 1):
            print(f"\n{i}. [{h['kategori']}] {h['judul'][:90]}")
            print(f"   {h['path']}")
            kutipan = re.sub(r"[ \t]+", " ", h["kutipan"])[:220]
            print(f"   …{kutipan}…")
    return hasil


def stat():
    kat = _koneksi_katalog()
    print("=" * 62)
    print("STATISTIK DATABASE KANTOR HUKUM VIRTUAL")
    print("=" * 62)

    print("\n-- Katalog regulasi (katalog.db) --")
    try:
        total = kat.execute("SELECT COUNT(*) FROM dokumen").fetchone()[0]
        print(f"   total dokumen terkatalog : {total:,}")
        for st, n in kat.execute("SELECT status, COUNT(*) FROM dokumen GROUP BY status ORDER BY 2 DESC"):
            print(f"   {st:16} : {n:,}")
        print("\n   per tipe (10 teratas):")
        for t, n in kat.execute("""SELECT tipe, COUNT(*) FROM dokumen
                                   GROUP BY tipe ORDER BY 2 DESC LIMIT 10"""):
            print(f"     {t or '-':14} {n:,}")
        print("\n   per tahun (8 terbaru):")
        for th, n in kat.execute("""SELECT tahun, COUNT(*) FROM dokumen
                                    WHERE tahun IS NOT NULL
                                    GROUP BY tahun ORDER BY tahun DESC LIMIT 8"""):
            print(f"     {th} : {n:,}")
    except sqlite3.OperationalError as e:
        print("   (belum ada data)", e)

    print("\n-- Jurnal hukum (jurnal.db) --")
    if JDB.exists():
        j = sqlite3.connect(JDB)
        try:
            print(f"   total artikel   : {j.execute('SELECT COUNT(*) FROM artikel').fetchone()[0]:,}")
            for st, n in j.execute("SELECT status, COUNT(*) FROM artikel GROUP BY status ORDER BY 2 DESC"):
                print(f"   {st:16} : {n:,}")
            print("   per jurnal:")
            for jn, n in j.execute("""SELECT jurnal, COUNT(*) FROM artikel
                                      WHERE status='selesai' GROUP BY jurnal
                                      ORDER BY 2 DESC"""):
                print(f"     {jn[:34]:36} {n:,}")
        except sqlite3.OperationalError as e:
            print("   (belum ada data)", e)
        j.close()
    else:
        print("   (belum ada — jalankan scraper_jurnal.py)")

    print("\n-- Berkas teks lengkap (.md) --")
    total_md = 0
    for d in sorted(KB_DIR.iterdir()):
        if not d.is_dir():
            continue
        n = len(list(d.rglob("*.md")))
        if n:
            print(f"   {d.name:24} {n:,} berkas")
            total_md += n
    print(f"   {'TOTAL':24} {total_md:,} berkas")

    ukuran = sum(f.stat().st_size for f in KB_DIR.rglob("*") if f.is_file())
    print(f"\n   ukuran knowledge_base   : {ukuran/1024/1024:.1f} MB")
    if IDX_FILE.exists():
        print(f"   indeks pencarian        : {IDX_FILE.stat().st_size/1024/1024:.1f} MB")
    kat.close()


if __name__ == "__main__":
    a = sys.argv[1:]
    if "--build" in a:
        build()
    elif "--cari" in a:
        cari(a[a.index("--cari") + 1])
    elif "--json" in a:
        cari(a[a.index("--json") + 1], sebagai_json=True)
    else:
        stat()
