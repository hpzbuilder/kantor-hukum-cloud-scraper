#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_cloud_batch.py — Orkestrator Batch Cloud (GitHub Actions Worker)
Menjalankan siklus akuisisi regulasi & repositori hukum, menyegarkan metadata
dan indeks FTS5, lalu membundel hasil ke dalam zip siap pakai.
"""

import sys
import os
import time
import zipfile
import subprocess
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent

def run_step(cmd_args, label):
    print(f"\n{'='*60}")
    print(f"▶ MEMULAI: {label}")
    print(f"{'='*60}")
    t0 = time.time()
    try:
        proc = subprocess.run([sys.executable] + cmd_args, cwd=str(BASE_DIR), check=False)
        print(f"✔ SELESAI ({time.time()-t0:.1f}s) — Exit Code: {proc.returncode}")
        return proc.returncode == 0
    except Exception as e:
        print(f"✘ GAGAL: {e}")
        return False

def main():
    print("============================================================")
    print("   KANTOR HUKUM VIRTUAL — CLOUD AUTO-HARVEST RUNNER")
    print(f"   Waktu Mulai: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("============================================================")

    # 1. Unduh Regulasi Nasional (Agent 1)
    # Agent 1 (JDIH Kemenkum) DINONAKTIFKAN di cloud sejak 2026-10-08:
    # JDIH menolak IP runner GitHub -> 0 dokumen baru, ribuan 'gagal-halaman'.
    # Jalankan Agent 1 dari PC lokal (IP Indonesia).
    gagal = []

    # 2. Unduh Repositori Akademik / Tesis UNAIR (Agent 2)
    if not run_step(["repo_harvest.py", "--sumber", "UNAIR Repository (Hukum)", "--limit", "30"], 
             "Agent 2 — Repositori Akademik (Tesis/Disertasi Hukum UNAIR)"):
        gagal.append("Agent 2 — Repositori Akademik (Tesis/Disertasi Hukum UNAIR)")

    # 3. Pengayaan Metadata & Relasi Hukum
    if not run_step(["kb_metadata.py", "--limit", "40"], 
             "Koordinator — Ekstraksi Metadata & Taksonomi Kanonikal"):
        gagal.append("Koordinator — Ekstraksi Metadata & Taksonomi Kanonikal")

    # 4. Bangun Indeks FTS5
    run_step(["kb_index.py", "--build"], 
             "Koordinator — Rebuild FTS5 Search Index")

    # 5. Kompresi Paket Hasil Siap Pakai
    print(f"\n{'='*60}")
    print("📦 MEMBUAT ARSIP PAKET DATA SIAP PAKAI (.ZIP)")
    print(f"{'='*60}")
    
    kb_dir = BASE_DIR / "knowledge_base"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"kantor_hukum_data_{ts}.zip"
    zip_path = BASE_DIR / zip_name

    file_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(kb_dir):
            for file in files:
                fp = Path(root) / file
                # Jangan sertakan file .part temporer jika ada
                if file.endswith(".part"):
                    continue
                rel_path = fp.relative_to(BASE_DIR)
                zf.write(fp, arcname=str(rel_path))
                file_count += 1
    
    zip_size_mb = zip_path.stat().st_size / (1024 * 1024)
    print(f"Arsip Berhasil Dibuat: {zip_path.name}")
    print(f"Total File Didalamnya : {file_count} berkas")
    print(f"Ukuran File Zip       : {zip_size_mb:.2f} MB")

    # Tulis output untuk GitHub Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"zip_name={zip_name}\n")
            f.write(f"zip_path={zip_path}\n")
            f.write(f"zip_size_mb={zip_size_mb:.2f}\n")
            f.write(f"file_count={file_count}\n")
            f.write(f"timestamp={ts}\n")
            f.write(f"failed_steps={' | '.join(gagal)}\n")

    if gagal:
        print("\n✘ LANGKAH GAGAL: " + "; ".join(gagal))

if __name__ == "__main__":
    main()
