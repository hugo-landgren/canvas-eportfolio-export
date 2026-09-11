#!/usr/bin/env python3
"""Slår upp namnen bakom portfoliernas user_id.

    .venv/bin/python tools/resolve_eportfolio_owners.py \
        --env .env --export ~/canvas-exports/eportfolios-prod-2026-08-27.ndjson \
        --out ~/canvas-exports/owners-prod.json

Id-iterationen i `export_eportfolios.py` är snabb just för att den går på
portfolio-id och aldrig frågar efter användaren, så posterna bär bara ett
`user_id`. Det duger för migrering men inte för någon som ska hitta en viss
students portfolio.

Utdatan är ett JSON-objekt `{user_id: {name, sortable_name, login_id}}`.
`login_id` är CID:t och tas med för att det är det handläggare faktiskt söker
på. E-postadresser utelämnas medvetet -- de finns i huvudexporten för den som
behöver dem, och behöver inte spridas till fler filer.

Read-only.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv


def build_session(env_file):
    load_dotenv(env_file, override=True)
    from canvas_api import settings

    s = settings()
    if not s.canvas_configured:
        sys.exit("CANVAS_API_TOKEN/CANVAS_HOST saknas i {}".format(env_file))
    import requests

    sess = requests.Session()
    sess.headers.update({"Authorization": "Bearer {}".format(s.canvas_token)})
    return s.canvas_host, sess


def owner_ids(export_path):
    ids = set()
    with open(export_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                uid = json.loads(line).get("user_id")
            except ValueError:
                continue
            if uid is not None:
                ids.add(uid)
    return sorted(ids)


def fetch(host, sess, uid):
    for attempt in range(3):
        try:
            r = sess.get("https://{}/api/v1/users/{}".format(host, uid), timeout=30)
        except Exception:
            if attempt == 2:
                return uid, {"error": "request"}
            time.sleep(1 + attempt)
            continue
        if r.status_code == 403 and attempt < 2:  # strypning
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code != 200:
            # 404 = användaren är raderad ur Canvas; portfolion kan finnas kvar
            return uid, {"error": r.status_code}
        d = r.json()
        return uid, {"name": d.get("name"), "sortable_name": d.get("sortable_name"),
                     "login_id": d.get("login_id")}
    return uid, {"error": "retries"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    host, sess = build_session(args.env)
    ids = owner_ids(os.path.expanduser(args.export))
    print("miljö: {}".format(host))
    print("unika ägare: {}".format(len(ids)))

    started = time.time()
    owners = {}
    missing = 0
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        for n, (uid, row) in enumerate(pool.map(lambda u: fetch(host, sess, u), ids), 1):
            owners[str(uid)] = row
            if row.get("error"):
                missing += 1
            if n % 500 == 0:
                print("   {}/{} | {:.0f} uppslag/s".format(
                    n, len(ids), n / max(time.time() - started, 0.001)))

    with open(os.path.expanduser(args.out), "w", encoding="utf-8") as fh:
        json.dump(owners, fh, ensure_ascii=False, indent=1)

    print("upplösta: {}/{}  utan träff: {}".format(len(ids) - missing, len(ids), missing))
    print("klart på {:.0f} s -> {}".format(time.time() - started, os.path.expanduser(args.out)))


if __name__ == "__main__":
    main()
