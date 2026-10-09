#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
repo_harvest.py — Pemanen repositori akademik (buku, tesis, disertasi, skripsi)
==============================================================================
Memakai OAI-PMH (protokol resmi pemanenan metadata) pada repositori
institusi Indonesia, lalu mengunduh PDF karya ilmiahnya.

Pakai:
  python repo_harvest.py --daftar
  python repo_harvest.py --sumber undip --limit 20
  python repo_harvest.py --limit 10 --db knowledge_base/repo.db
"""

import re
import ssl
import sys
import time
import json
import random
import sqlite3
import logging
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime

import fitz  # PyMuPDF
import requests

BASE_DIR = Path(__file__).resolve().parent
KB_DIR = BASE_DIR / "knowledge_base" / "Repositori"
PDF_DIR = KB_DIR / "pdf"
DB_FILE = BASE_DIR / "knowledge_base" / "repo.db"
LOG_FILE = BASE_DIR / "repo_harvest.log"

for _i, _a in enumerate(sys.argv):
    if _a == "--db" and _i + 1 < len(sys.argv):
        DB_FILE = Path(sys.argv[_i + 1])

JEDA_HALAMAN = (3, 6)
JEDA_PDF = (2, 4)
ISTIRAHAT_TIAP = 40
ISTIRAHAT = (20, 45)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

# Repositori institusi (eprints / OAI) — karya ilmiah hukum & terkait
SUMBER = [
    {"nama": "UNAIR Repository (Hukum)", "oai": "https://repository.unair.ac.id/cgi/oai2",
     "penerbit": "Universitas Airlangga",
     "set": "7375626A656374733D4B"},  # Subject K = Law (General, Civil, Criminal, Constitutional, etc.)
    {"nama": "Undip Repository (Hukum)", "oai": "https://eprints.undip.ac.id/cgi/oai2",
     "penerbit": "Universitas Diponegoro",
     "set": "6469766973696F6E733D6661635F6C6177"},  # Division = Faculty of Law
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("repo")

# ------------------------------------------------------------------ util
try:
    from scrapling import Fetcher
    _scrapling_fetcher = Fetcher()
    log.info("Scrapling Anti-Bot Engine aktif untuk Repositori Akademik.")
except Exception as _e:
    _scrapling_fetcher = None
    log.info(f"Scrapling tidak aktif ({_e}), menggunakan urllib.")

def get(url, timeout=60, biner=False):
    if _scrapling_fetcher is not None:
        try:
            page = _scrapling_fetcher.get(url, timeout=timeout)
            return page.status, (page.body if biner else page.body.decode("utf-8", "replace"))
        except Exception:
            pass  # fallback ke urllib jika ada kendala
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "id-ID,id;q=0.9",
        "Accept": "text/html,application/pdf,application/xml,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            data = r.read()
            return r.status, (data if biner else data.decode("utf-8", "replace"))
    except Exception as e:
        return None, (b"" if biner else f"__ERR__{type(e).__name__}")


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


def bersih(s):
    return re.sub(r'[\\/*?:"<>|]', "_", re.sub(r"\s+", " ", s or "")).strip()[:85]


def db_init():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_FILE)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS karya (
        id TEXT PRIMARY KEY, sumber TEXT, penerbit TEXT, jenis TEXT,
        judul TEXT, penulis TEXT, tahun TEXT, url TEXT, pdf_url TEXT,
        abstrak TEXT, subjek TEXT, pdf_path TEXT, md_path TEXT,
        status TEXT DEFAULT 'meta', halaman INTEGER, panjang_teks INTEGER, diambil TEXT)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_sumber ON karya(sumber)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_status ON karya(status)")
    con.commit()
    return con


def list_sets(oai_url):
    st, xml = get(f"{oai_url}?verb=ListSets")
    if st != 200 or not isinstance(xml, str):
        return []
    return re.findall(r"<setSpec>([^<]+)</setSpec>", xml)


MAKS_REKAM_OAI = 6000

# Set "Subject K" UNAIR ikut memuat tesis non-hukum yang salah label (mis. kedokteran,
# pangan). Saring dengan kosakata hukum pada judul/subjek agar korpus tetap relevan.
KATA_HUKUM = re.compile(
    r"hukum|undang|peraturan|perda|putusan|pengadilan|hakim|pidana|perdata|sengketa|"
    r"perjanjian|kontrak|akta|notaris|ppat|tanggung gugat|tanggung jawab|wanprestasi|"
    r"perlindungan|kewenangan|yuridis|legal|konstitusi|perseroan|kepailitan|pailit|"
    r"waris|perkawinan|cerai|hak (atas|milik|cipta|merek|paten)|tanah|pajak|korupsi|"
    r"tindak|sanksi|gugatan|arbitrase|mediasi|ketenagakerjaan|pekerja|konsumen|"
    r"law|jurisdic|liability|court|statute", re.I)


def relevan_hukum(a):
    teks = f"{a.get('judul', '')} {a.get('abstrak', '')[:300]}"
    return bool(KATA_HUKUM.search(teks))


def harvest(oai_url, maks, set_spec=None, dari=None):
    url = f"{oai_url}?verb=ListRecords&metadataPrefix=oai_dc"
    if set_spec:
        url += f"&set={urllib.parse.quote(set_spec)}"
    if dari:
        url += f"&from={dari}"
    out = []
    while url and len(out) < maks:
        st, xml = get(url)
        if st != 200 or not isinstance(xml, str) or xml.startswith("__ERR__"):
            log.warning(f"    OAI gagal (status {st})")
            break
        recs = re.findall(r"<record>(.*?)</record>", xml, re.S)
        if not recs:
            break
        out.extend(recs)
        tok = re.search(r"<resumptionToken[^>]*>([^<]+)</resumptionToken>", xml)
        if not tok or not tok.group(1).strip():
            break
        url = (f"{oai_url}?verb=ListRecords&resumptionToken="
               f"{urllib.parse.quote(tok.group(1).strip())}")
        jeda((2, 4), "(halaman OAI berikutnya)")
    return out


def tag(rec, name):
    return [re.sub(r"\s+", " ", m).strip()
            for m in re.findall(rf"<{name}[^>]*>(.*?)</{name}>", rec, re.S)]


JENIS_KATA = {
    "disertasi": "disertasi", "dissertation": "disertasi",
    "tesis": "tesis", "thesis": "tesis", "master": "tesis",
    "skripsi": "skripsi", "undergraduate": "skripsi",
    "buku": "buku", "book": "buku", "monograf": "buku",
    "jurnal": "artikel", "article": "artikel", "journal": "artikel",
    "laporan": "laporan", "report": "laporan",
}


PDF_BUANG = re.compile(r"cover|sampul|halaman[ _%20-]*depan|daftar[ _%20-]*(isi|pustaka)|"
                       r"lampiran|pengesahan|kata[ _%20-]*pengantar|pernyataan", re.I)
PDF_ISI = re.compile(r"bab|full|isi|tesis|thesis|skripsi|disertasi|chapter", re.I)
MAKS_PDF_PER_KARYA = 4
MIN_TEKS_SELESAI = 5000


def urutkan_pdf(pdfs, eprint_id):
    """Urutan unduh: berkas teks penuh ({id}.pdf / bab / isi) dulu, buang sampul & lampiran.
    Bila hanya tersisa sampul/abstrak, tetap ambil berkas pertama (status 'teks-pendek')."""
    nama = lambda u: urllib.parse.unquote(u.rsplit("/", 1)[-1]).lower()
    utama = [u for u in pdfs if eprint_id and nama(u) == f"{eprint_id}.pdf"]
    isi = [u for u in pdfs if u not in utama and not PDF_BUANG.search(nama(u))]
    isi.sort(key=lambda u: (not PDF_ISI.search(nama(u)), nama(u)))
    hasil = utama + isi
    return hasil or pdfs[:1]


def parse_record(rec, sumber):
    judul = (tag(rec, "dc:title") or [""])[0]
    if not judul:
        return None
    ids = tag(rec, "dc:identifier")
    # ID unik = nomor eprint di header OAI (oai:host:3145 -> "3145"). Dulu diambil dari
    # nama berkas PDF ("1.COVER.pdf") sehingga banyak tesis saling menimpa.
    m_oai = re.search(r"<identifier>[^<]*:(\d+)</identifier>", rec)
    eprint_id = m_oai.group(1) if m_oai else ""
    kandidat = [i for i in ids + tag(rec, "dc:relation")
                if i.startswith("http") and not i.lower().endswith(".pdf")]
    landing = next((i for i in kandidat if eprint_id and re.search(rf"/{eprint_id}/?$", i)), "") \
        or next((i for i in kandidat if i.rstrip("/").count("/") >= 3), "")
    pdfs = urutkan_pdf([i for i in ids if i.lower().endswith(".pdf")], eprint_id)
    if not landing and not pdfs:
        return None
    teks_jenis = " ".join(tag(rec, "dc:type") + tag(rec, "dc:description")[:1]).lower()
    jenis = next((v for k, v in JENIS_KATA.items() if k in teks_jenis), "")
    if not jenis:
        jenis = next((v for k, v in JENIS_KATA.items() if k in judul.lower()), "karya")
    return {
        "id": eprint_id or bersih((landing or pdfs[0]).rstrip("/").rsplit("/", 1)[-1]) or bersih(judul),
        "sumber": sumber["nama"], "penerbit": sumber["penerbit"], "jenis": jenis,
        "judul": judul, "penulis": "; ".join(tag(rec, "dc:creator")[:4]),
        "tahun": (tag(rec, "dc:date") or [""])[0][:10],
        "url": landing or pdfs[0], "pdf_url": pdfs[0] if pdfs else "", "pdfs": pdfs,
        "abstrak": re.sub(r"\s+", " ", (tag(rec, "dc:description") or [""])[0])[:1500],
        "subjek": "; ".join(tag(rec, "dc:subject")[:6]),
    }


def cari_pdf(landing):
    st, h = get(landing, timeout=45)
    if st != 200 or not isinstance(h, str):
        return None
    m = re.search(r'href="([^"]+\.pdf)"', h, re.I)
    return m.group(1) if m else None


def proses(con, a, meta_saja):
    log.info(f"  [{a['sumber']}] ({a['jenis']}) {a['judul'][:66]}")

    if meta_saja:
        simpan(con, a, "meta")
        return True

    pdfs = a.get("pdfs") or []
    if not pdfs:
        cari = cari_pdf(a["url"])
        pdfs = [cari] if cari else []
    if not pdfs:
        simpan(con, a, "tanpa-pdf")
        return False

    PDF_DIR.mkdir(parents=True, exist_ok=True)
    bagian, n_hal, dipakai, pdf_paths = [], 0, [], []
    for k, url_pdf in enumerate(pdfs[:MAKS_PDF_PER_KARYA]):
        pdf_path = PDF_DIR / (f"{a['id']}.pdf" if k == 0 else f"{a['id']}_{k}.pdf")
        if not pdf_path.exists():
            if not unduh_stream_pdf(url_pdf, pdf_path):
                continue
            jeda(JEDA_PDF)
        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            log.warning(f"    PDF rusak: {e}")
            continue
        n_hal += doc.page_count
        for i, page in enumerate(doc):
            t = page.get_text().strip()
            if t:
                bagian.append(t)
            if i >= 59:
                break
        doc.close()
        dipakai.append(url_pdf)
        pdf_paths.append(str(pdf_path))
    if not dipakai:
        simpan(con, a, "gagal-pdf")
        return False
    teks = "\n\n".join(bagian)
    pdf_url = " | ".join(dipakai)
    a["pdf_url"] = dipakai[0]

    folder = KB_DIR / bersih(a["sumber"])
    folder.mkdir(parents=True, exist_ok=True)
    md_path = folder / f"{bersih(a['judul'])}.md"
    md_path.write_text(
        f"# {a['judul']}\n\n"
        f"**Jenis     :** {a['jenis']}\n"
        f"**Repositori:** {a['sumber']} — {a['penerbit']}\n"
        f"**Penulis   :** {a['penulis'] or '-'}\n"
        f"**Tahun     :** {a['tahun'] or '-'}\n"
        f"**Subjek    :** {a['subjek'] or '-'}\n"
        f"**URL       :** {a['url']}\n"
        f"**PDF       :** {pdf_url}\n"
        f"**Halaman   :** {n_hal}\n"
        f"**Diambil   :** {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        f"## Abstrak\n\n{a['abstrak'] or '-'}\n\n---\n\n{teks}\n",
        encoding="utf-8")

    a["pdf_path"], a["md_path"] = " | ".join(pdf_paths), str(md_path)
    # Hanya sampul/abstrak yang tersedia → jangan diklaim teks penuh.
    status = "selesai" if len(teks) >= MIN_TEKS_SELESAI else "teks-pendek"
    simpan(con, a, status, n_hal, len(teks))
    log.info(f"    teks {len(teks)} char -> {md_path.name}")
    return True


def simpan(con, a, status="meta", halaman=None, panjang=None):
    con.execute("""INSERT INTO karya (id,sumber,penerbit,jenis,judul,penulis,tahun,url,
                    pdf_url,abstrak,subjek,pdf_path,md_path,status,halaman,panjang_teks,diambil)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                     pdf_url=excluded.pdf_url, pdf_path=excluded.pdf_path,
                     md_path=excluded.md_path, halaman=excluded.halaman,
                     panjang_teks=excluded.panjang_teks, diambil=excluded.diambil""",
                (a["id"], a["sumber"], a["penerbit"], a["jenis"], a["judul"], a["penulis"],
                 a["tahun"], a["url"], a.get("pdf_url"), a["abstrak"], a["subjek"],
                 a.get("pdf_path"), a.get("md_path"), status, halaman, panjang,
                 datetime.now().isoformat()))
    con.commit()


def main():
    args = sys.argv[1:]
    if "--daftar" in args:
        for s in SUMBER:
            print(f"  {s['nama']:26} {s['oai']}")
        return

    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 5
    pilih = args[args.index("--sumber") + 1].lower() if "--sumber" in args else None
    meta_saja = "--meta-saja" in args
    dari = args[args.index("--tahun") + 1] + "-01-01" if "--tahun" in args else None

    log.info("=" * 60)
    log.info(f"=== Pemanen Repositori Akademik — {limit}/sumber ===")
    log.info("=" * 60)

    con = db_init()
    ok = 0
    for s in [x for x in SUMBER if not pilih or pilih in x["nama"].lower()]:
        log.info(f"\n--- {s['nama']} ---")
        sets = list_sets(s["oai"])
        log.info(f"    set tersedia: {len(sets)}")
        # Panen seluruh set (dibatasi MAKS_REKAM_OAI), buang yang sudah final di DB,
        # lalu ambil yang terbaru. Dulu: selalu 30 rekaman yang sama → 0 karya baru.
        recs = harvest(s["oai"], maks=MAKS_REKAM_OAI, set_spec=s.get("set"), dari=dari)
        final = {r[0] for r in con.execute(
            "SELECT id FROM karya WHERE status IN ('selesai','teks-pendek','tanpa-pdf','gagal-pdf')")}
        calon = [a for a in (parse_record(r, s) for r in recs)
                 if a and a["id"] not in final and relevan_hukum(a)]
        calon.sort(key=lambda a: a.get("tahun") or "", reverse=True)
        log.info(f"    rekaman OAI: {len(recs)} | belum diproses: {len(calon)} | diambil: {min(limit, len(calon))}")
        for i, a in enumerate(calon[:limit], 1):
            try:
                if proses(con, a, meta_saja):
                    ok += 1
            except Exception as e:
                log.error(f"  error: {type(e).__name__}: {e}")
            jeda(JEDA_HALAMAN, "(antar karya)")
            if i % ISTIRAHAT_TIAP == 0:
                jeda(ISTIRAHAT, f"(istirahat setelah {i})")

    log.info("\n" + "=" * 60)
    log.info(f"=== SELESAI — {ok} karya diproses ===")
    for st, n in con.execute("SELECT status, COUNT(*) FROM karya GROUP BY status ORDER BY 2 DESC"):
        log.info(f"    {st:12} {n}")
    con.close()


if __name__ == "__main__":
    main()
