#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ingest_putusan_indolaw.py — Impor korpus putusan pengadilan terbuka (indo-law, UI)
==================================================================================
Sumber data:
  - Korpus asli: github.com/ir-nlp-csui/indo-law (Universitas Indonesia) — 22.630
    putusan pidana dari Direktori Putusan MA, teks bersih + anotasi 11 bagian.
  - Salinan parquet: huggingface.co/datasets/Azzindani/ID_Supreme_Court_Parquet
    (supreme_court.parquet, ~372 MB, label lisensi cc-by-4.0).
  Putusan pengadilan tidak dilindungi hak cipta (UU 28/2014 Pasal 42); atribusi
  korpus tetap dicantumkan di setiap berkas.

Keluaran:
  knowledge_base/putusan_terbuka.db            (tabel putusan + bagian)
  knowledge_base/Putusan_MA/indo-law/<klasifikasi>/<nomor>.md   (siap indeks FTS5)

Pakai (parquet harus sudah diunduh ke folder data terpisah):
  python ingest_putusan_indolaw.py --parquet D:/data_unduhan/indolaw/supreme_court.parquet --kb <...>/knowledge_base
  python ingest_putusan_indolaw.py ... --limit 200      # uji coba
"""

import argparse
import ast
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ATRIBUSI = ("Korpus indo-law (ir-nlp-csui, Universitas Indonesia) via HF "
            "Azzindani/ID_Supreme_Court_Parquet; sumber asli Direktori Putusan MA RI")
URUTAN = ["kepala_putusan", "identitas", "riwayat_penahanan", "riwayat_perkara",
          "riwayat_dakwaan", "riwayat_tuntutan", "riwayat_tuntutan", "fakta", "fakta_hukum",
          "pertimbangan_hukum", "amar_putusan", "penutup"]
JUDUL_BAGIAN = {
    "kepala_putusan": "Kepala Putusan", "identitas": "Identitas Terdakwa",
    "riwayat_penahanan": "Riwayat Penahanan", "riwayat_perkara": "Riwayat Perkara",
    "riwayat_dakwaan": "Dakwaan", "riwayat_tuntutan": "Tuntutan", "fakta": "Fakta",
    "fakta_hukum": "Fakta Hukum", "pertimbangan_hukum": "Pertimbangan Hukum",
    "amar_putusan": "Amar Putusan", "penutup": "Penutup",
}
# Teks indo-law sudah dinormalisasi (huruf kecil, tanda baca dibuang):
# "putusan nomor 267 pid sus 2014 pn bk" -> 267/Pid.Sus/2014/PN Bk
POLA_NOMOR = re.compile(
    r"(?:nomor|no)\s*:?\s*(\d+)\s*[/ ]\s*(pid(?:\s*[./]?\s*(?:sus|b|c|s|pra|anak|tpk|lh))*(?:\s*anak)?)"
    r"\s*[/ ]\s*((?:19|20)\d{2})\s*[/ ]\s*(pn\s+[a-z]{1,5}(?:\s+(?!demi\b|yang\b|dan\b)[a-z]{2,4}\b)?)", re.I)
POLA_PENGADILAN = re.compile(
    r"(pengadilan\s+(?:negeri|tinggi|agama|militer)\s+[a-z]+(?:\s+[a-z]+)??)"
    r"(?=\s+(?:yang|demi|putusan|nomor|no|dengan|tersebut)\b)", re.I)


def susun_nomor(m):
    if not m:
        return ""
    jenis = ".".join(w.capitalize() for w in re.split(r"[\s./]+", m.group(2)) if w)
    pn = m.group(4).split()
    pn_s = "PN " + " ".join(w.capitalize() for w in pn[1:]) if pn and pn[0].lower() == "pn" else m.group(4)
    return f"{m.group(1)}/{jenis}/{m.group(3)}/{pn_s}"



SKEMA = """
CREATE TABLE IF NOT EXISTS putusan (
    id TEXT PRIMARY KEY, sumber TEXT, klasifikasi TEXT, sub_klasifikasi TEXT,
    nomor TEXT, pengadilan TEXT, tahun TEXT, amar TEXT, md_path TEXT,
    panjang_teks INTEGER, diimpor TEXT
);
CREATE TABLE IF NOT EXISTS bagian (
    id TEXT, urut INTEGER, tag TEXT, isi TEXT, PRIMARY KEY (id, urut)
);
CREATE INDEX IF NOT EXISTS idx_putusan_klas ON putusan(klasifikasi, tahun);
"""


def urai_paragraf(nilai):
    if isinstance(nilai, dict):
        d = nilai
    elif isinstance(nilai, str):
        try:
            d = json.loads(nilai)
        except ValueError:
            d = ast.literal_eval(nilai)
    else:
        d = dict(nilai)
    tags, vals = list(d.get("tag", [])), list(d.get("value", []))
    return [(t, (v or "").strip()) for t, v in zip(tags, vals)]


def nama_aman(s, maks=80):
    return re.sub(r"[^a-zA-Z0-9_.\-]+", "_", s)[:maks].strip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--kb", required=True)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    import pyarrow.parquet as pq

    kb = Path(a.kb).resolve()
    con = sqlite3.connect(kb / "putusan_terbuka.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SKEMA)
    sudah = {r[0] for r in con.execute("SELECT id FROM putusan")}

    pf = pq.ParquetFile(a.parquet)
    n_baru = n_lewat = 0
    sekarang = datetime.now().isoformat(timespec="seconds")
    for batch in pf.iter_batches(batch_size=500, columns=["id", "klasifikasi", "sub_klasifikasi", "paragraphs"]):
        for row in batch.to_pylist():
            if a.limit and n_baru >= a.limit:
                break
            pid = str(row["id"])
            if pid in sudah:
                n_lewat += 1
                continue
            bagian = urai_paragraf(row["paragraphs"])
            kepala = " ".join(v for t, v in bagian if t in ("kepala_putusan", "identitas"))[:3000]
            m_no = POLA_NOMOR.search(kepala)
            nomor = susun_nomor(m_no)
            m_pn = POLA_PENGADILAN.search(kepala)
            pengadilan = re.sub(r"\s+", " ", m_pn.group(1)).strip().title() if m_pn else ""
            tahun = m_no.group(3) if m_no else ""
            amar = " ".join(v for t, v in bagian if t == "amar_putusan")
            klas = row.get("klasifikasi") or "lain"

            sub = kb / "Putusan_MA" / "indo-law" / nama_aman(klas, 40)
            sub.mkdir(parents=True, exist_ok=True)
            md = sub / f"{nama_aman(nomor or pid)}_{pid[:8]}.md"
            judul = f"Putusan {pengadilan or 'Pengadilan'} Nomor {nomor or pid}"
            isi_bagian = "\n\n".join(f"## {JUDUL_BAGIAN.get(t, t)}\n{v}" for t, v in bagian if v)
            md.write_text(f"""# {judul}

**Tipe      :** Putusan Pengadilan ({klas})
**Kategori  :** Putusan MA / Peradilan Umum (korpus terbuka)
**Pengadilan:** {pengadilan or '-'} | **Tahun:** {tahun or '-'}
**Sumber    :** {ATRIBUSI}
**Diimpor   :** {sekarang[:16].replace('T', ' ')}

---

{isi_bagian}
""", encoding="utf-8")
            con.execute("INSERT OR REPLACE INTO putusan VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, "indo-law/HF", klas, row.get("sub_klasifikasi"), nomor, pengadilan, tahun,
                         amar[:4000], str(md.relative_to(kb)), sum(len(v) for _, v in bagian), sekarang))
            con.executemany("INSERT OR REPLACE INTO bagian VALUES (?,?,?,?)",
                            [(pid, i, t, v) for i, (t, v) in enumerate(bagian)])
            n_baru += 1
        con.commit()
        if a.limit and n_baru >= a.limit:
            break
        print(f"  ... {n_baru} diimpor")

    tot = con.execute("SELECT COUNT(*) FROM putusan").fetchone()[0]
    print(f"Selesai: {n_baru} putusan baru, {n_lewat} sudah ada. Total putusan_terbuka.db: {tot}")
    for k, n in con.execute("SELECT klasifikasi, COUNT(*) FROM putusan GROUP BY 1 ORDER BY 2 DESC"):
        print(f"   {k:25} {n}")
    con.close()


if __name__ == "__main__":
    main()
