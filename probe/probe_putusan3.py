"""Probe read-only putusan3.mahkamahagung.go.id dari runner GitHub Actions.
Tujuan: tahu apakah IP runner diblok, ada captcha/WAF, rate limit, dan pola URL.
Sopan: jeda 3 detik, ~15 request total, UA jujur + kontak.
"""
import json, re, sys, time, urllib.request, urllib.error

BASE = "https://putusan3.mahkamahagung.go.id"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
UA_BOT = "KHV-PDI-research/0.1 (+https://github.com/hpzbuilder/kantor-hukum-cloud-scraper)"
MARKERS = ["captcha", "cf-chl", "just a moment", "access denied", "attention required",
           "recaptcha", "hcaptcha", "forbidden", "request blocked", "too many requests"]
results = []

def hit(label, path, ua=UA_BROWSER, method="GET", read=True):
    url = path if path.startswith("http") else BASE + path
    req = urllib.request.Request(url, method=method, headers={
        "User-Agent": ua, "Accept": "text/html,application/pdf,*/*", "Accept-Language": "id,en;q=0.8"})
    t0 = time.time()
    rec = {"label": label, "url": url, "ua": "bot" if ua == UA_BOT else "browser", "method": method}
    body = b""
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            rec["status"] = r.status
            rec["headers"] = {k: v for k, v in r.getheaders()
                              if k.lower() in ("server", "content-type", "content-length", "set-cookie",
                                               "cf-ray", "location", "retry-after", "x-powered-by", "via")}
            if read and method == "GET":
                body = r.read(400_000)
    except urllib.error.HTTPError as e:
        rec["status"] = e.code
        rec["headers"] = {k: v for k, v in e.headers.items()
                          if k.lower() in ("server", "cf-ray", "retry-after", "location", "content-type")}
        try: body = e.read(50_000)
        except Exception: pass
    except Exception as e:
        rec["status"] = None
        rec["error"] = f"{type(e).__name__}: {e}"[:300]
    rec["elapsed_s"] = round(time.time() - t0, 2)
    txt = body.decode("utf-8", "ignore")
    rec["body_bytes"] = len(body)
    rec["markers"] = [m for m in MARKERS if m in txt.lower()]
    rec["title"] = (re.search(r"<title>(.*?)</title>", txt, re.S | re.I) or [None, ""])[1].strip()[:120]
    rec["putusan_links"] = len(set(re.findall(r'/direktori/putusan/[0-9a-f]{20,}\.html', txt)))
    rec["pdf_links"] = len(set(re.findall(r'/direktori/download_file/[^"\']+/pdf/[^"\']+', txt)))
    results.append(rec)
    print(json.dumps({k: rec[k] for k in ("label", "status", "elapsed_s", "body_bytes", "markers", "title", "putusan_links", "pdf_links")}, ensure_ascii=False), flush=True)
    time.sleep(3)
    return txt, rec

# 0. IP egress runner
try:
    ip = urllib.request.urlopen("https://api.ipify.org", timeout=15).read().decode()
except Exception as e:
    ip = f"unknown ({e})"
print("runner_ip:", ip)

home, _ = hit("home", "/")
robots, rrec = hit("robots", "/robots.txt")
hit("home_bot_ua", "/", ua=UA_BOT)
listing, lrec = hit("listing_kategori", "/direktori/index/pengadilan/pn-ciamis/kategori/anak-1.html")
hit("listing_page2", "/direktori/index/pengadilan/pn-ciamis/kategori/anak-1/page/2.html")
search, srec = hit("search", "/search.html?q=wanprestasi+haji")
detail_links = re.findall(r'(/direktori/putusan/[0-9a-f]{20,}\.html)', listing) or re.findall(r'(/direktori/putusan/[0-9a-f]{20,}\.html)', search)
detail = ""
pdf_urls = []
if detail_links:
    detail, drec = hit("detail", detail_links[0])
    pdf_urls = re.findall(r'(/direktori/download_file/[^"\']+/pdf/[^"\']+)', detail)
    zip_urls = re.findall(r'(/direktori/download_file/[^"\']+/zip/[^"\']+)', detail)
    if pdf_urls:
        hit("pdf_head", pdf_urls[0], method="GET", read=False)
# 3 request beruntun cepat (1 dtk) untuk melihat rate limit ringan
for i in range(3):
    t = time.time(); hit(f"burst_{i}", "/direktori/index/pengadilan/pn-ciamis/kategori/anak-1.html"); 

out = {"runner_ip": ip, "robots_txt": robots[:4000], "detail_sample": detail_links[:1],
       "pdf_sample": pdf_urls[:1], "results": results}
json.dump(out, open("probe/probe_result.json", "w"), ensure_ascii=False, indent=2)
print("selesai")
