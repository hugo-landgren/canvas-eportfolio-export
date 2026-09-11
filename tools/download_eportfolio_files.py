"""Ladda ned bilagefilerna som en e-portfolioexport bara refererar till.

Kör:
    .venv/bin/python tools/download_eportfolio_files.py \
        --export /sokvag/eportfolios-prod.ndjson \
        --dest /sokvag/attachments --env .env

Exporten (`tools/export_eportfolios.py --resolve-files`) sparar filmetadata med
en nedladdnings-URL, men inte filen. URL:en bär en `verifier`-token som går ut,
så det här skriptet faller tillbaka på `/files/:id/download` med Bearer-token
när verifieraren avvisas -- då spelar det ingen roll hur gammal exporten är.

Filerna namnges `<attachment_id>-<filnamn>`: id:t garanterar unikhet (samma
`display_name` återkommer flitigt, t.ex. "Scanned Work Card-1.pdf") och gör att
en fil kan spåras tillbaka till sin sektion i NDJSON-filen.

Körningen kan återupptas. En fil vars storlek på disk redan matchar `size` i
exporten hoppas över, så avbryt och kör om utan att tänka på det.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHUNK = 1 << 16
UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


def safe_name(attachment_id, display_name):
    name = UNSAFE.sub("_", (display_name or "fil").strip())[:120]
    return "{}-{}".format(attachment_id, name or "fil")


def load_files(export_path):
    """Plocka ut varje upplöst bilaga, avdubblad på attachment_id.

    Samma fil kan sitta i flera portfolios; vi vill ha den en gång på disk.
    """
    seen = {}
    with open(export_path, encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            for f in row.get("files", []):
                if "error" in f or not f.get("id"):
                    continue
                seen.setdefault(f["id"], f)
    return list(seen.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", required=True, help="NDJSON från export_eportfolios.py")
    ap.add_argument("--dest", required=True, help="katalog att lägga filerna i")
    ap.add_argument("--env", default=".env", help="miljöfil, för token-fallbacken")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    from dotenv import load_dotenv
    load_dotenv(args.env, override=True)
    from canvas_api import settings

    s = settings()
    os.makedirs(args.dest, exist_ok=True)

    files = load_files(args.export)
    total_bytes = sum(f.get("size") or 0 for f in files)
    print("miljö      :", s.canvas_host)
    print("bilagor    : {} st, {:.0f} MB".format(len(files), total_bytes / 1e6))
    print("mål        :", args.dest)

    local = threading.local()

    def session():
        if not hasattr(local, "s"):
            local.s = requests.Session()
        return local.s

    lock = threading.Lock()
    stats = Counter()
    manifest = []
    t0 = time.time()

    def fetch(f):
        """Hämta en fil; verifier-URL först, Bearer-token som reserv."""
        sess = session()
        attempts = [("verifier", f.get("url"), {})]
        if s.canvas_token:
            attempts.append((
                "token",
                "https://{}/api/v1/files/{}/download?download_frd=1".format(
                    s.canvas_host, f["id"]),
                {"Authorization": "Bearer {}".format(s.canvas_token)},
            ))
        last = None
        for how, url, headers in attempts:
            if not url:
                continue
            try:
                r = sess.get(url, headers=headers, stream=True, timeout=120)
            except requests.RequestException as exc:
                last = str(exc)
                continue
            if r.status_code == 200:
                return how, r, None
            last = "HTTP {}".format(r.status_code)
            r.close()
        return None, None, last

    def one(f):
        path = os.path.join(args.dest, safe_name(f["id"], f.get("display_name")))
        expected = f.get("size")
        if os.path.exists(path) and expected and os.path.getsize(path) == expected:
            with lock:
                stats["hoppade_over"] += 1
            return

        how, r, err = fetch(f)
        if r is None:
            with lock:
                stats["misslyckade"] += 1
                manifest.append({"id": f["id"], "status": "fel", "detalj": err})
                print("  FEL {} {}: {}".format(f["id"], f.get("display_name"), err), flush=True)
            return

        tmp = path + ".part"
        written = 0
        try:
            with open(tmp, "wb") as out:
                for chunk in r.iter_content(CHUNK):
                    out.write(chunk)
                    written += len(chunk)
        finally:
            r.close()

        # Ofullständig fil är värre än ingen fil -- den ser klar ut vid en omkörning.
        if expected and written != expected:
            os.remove(tmp)
            with lock:
                stats["fel_storlek"] += 1
                manifest.append({"id": f["id"], "status": "fel_storlek",
                                 "vantat": expected, "fick": written})
                print("  STORLEK {} {}: väntade {} fick {}".format(
                    f["id"], f.get("display_name"), expected, written), flush=True)
            return

        os.replace(tmp, path)
        with lock:
            stats["hamtade"] += 1
            stats["byte"] += written
            stats["via_" + how] += 1
            manifest.append({"id": f["id"], "status": "ok", "fil": os.path.basename(path),
                             "byte": written, "content_type": f.get("content-type")})
            done = stats["hamtade"] + stats["hoppade_over"]
            if done % 50 == 0:
                el = time.time() - t0
                print("  {:>4}/{} | {:.0f} MB | {:.1f} fil/s".format(
                    done, len(files), stats["byte"] / 1e6, done / el), flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(one, files))

    manifest_path = os.path.join(args.dest, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(sorted(manifest, key=lambda m: m["id"]), fh, ensure_ascii=False, indent=2)

    el = time.time() - t0
    print("\n--- klart på {:.0f}s ---".format(el))
    print("hämtade     :", stats["hamtade"])
    print("hoppade över:", stats["hoppade_over"])
    print("misslyckade :", stats["misslyckade"] + stats["fel_storlek"])
    print("via verifier:", stats["via_verifier"], "| via token:", stats["via_token"])
    print("totalt      : {:.0f} MB".format(stats["byte"] / 1e6))
    print("manifest    :", manifest_path)


if __name__ == "__main__":
    main()
