#!/usr/bin/env python3
"""Hittar filer som bara är inbäddade i portfoliernas HTML.

    .venv/bin/python tools/resolve_embedded_files.py \
        --env .env --export ~/canvas-exports/eportfolios-prod-2026-08-27.ndjson \
        --out ~/canvas-exports/embedded-prod.ndjson

`attachment_ids` täcker bara `attachment`-sektioner. En student som i stället
lagt in en bild eller PDF mitt i en rich_text-sektion får en `<img src=
"/users/5/files/484931/preview">` i HTML:en, och det id:t syns ingenstans i
exportens metadata -- filen laddas alltså aldrig ned, och länken dör med
Canvas.

Utdatan har samma form som huvudexportens `files`, så
`download_eportfolio_files.py` kan köras rakt på den.
"""
import argparse
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

FILE_REF = re.compile(
    r'(?:href|src)="(?:https?://[^"/]+)?/(?:users/\d+/|courses/\d+/)?files/(\d+)', re.I)


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


def scan(export_path):
    """file_id -> portfolio-id som bäddar in den, för id som inte redan är kända."""
    known = set()
    found = {}
    with open(export_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                pf = json.loads(line)
            except ValueError:
                continue
            known.update(str(x) for x in (pf.get("attachment_ids") or []))
            for page in pf.get("pages") or []:
                content = page.get("content")
                blobs = []
                if isinstance(content, str):
                    blobs = [content]
                elif isinstance(content, list):
                    blobs = [s.get("content") or "" for s in content
                             if isinstance(s, dict) and s.get("section_type") in ("rich_text", "html")]
                for blob in blobs:
                    for fid in FILE_REF.findall(blob):
                        found.setdefault(fid, pf.get("id"))
    return {fid: pf for fid, pf in found.items() if fid not in known}


def resolve(host, sess, file_id):
    try:
        r = sess.get("https://{}/api/v1/files/{}".format(host, file_id), timeout=30)
    except Exception as exc:
        return {"id": file_id, "error": "request", "detail": str(exc)}
    if r.status_code != 200:
        return {"id": file_id, "error": r.status_code}
    d = r.json()
    return {"id": d.get("id"), "display_name": d.get("display_name"),
            "content-type": d.get("content-type"), "size": d.get("size"),
            "url": d.get("url"), "created_at": d.get("created_at")}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env")
    ap.add_argument("--export", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    host, sess = build_session(args.env)
    refs = scan(os.path.expanduser(args.export))
    print("miljö: {}".format(host))
    print("inbäddade filer utan attachment-sektion: {} i {} portfolios".format(
        len(refs), len(set(refs.values()))))

    ids = sorted(refs, key=int)
    with ThreadPoolExecutor(max_workers=args.threads) as pool:
        rows = list(pool.map(lambda fid: (fid, resolve(host, sess, fid)), ids))

    ok = sum(1 for _, f in rows if not f.get("error"))
    total = sum(f.get("size") or 0 for _, f in rows if not f.get("error"))
    with open(os.path.expanduser(args.out), "w", encoding="utf-8") as fh:
        for fid, f in rows:
            fh.write(json.dumps({"eportfolio_id": refs[fid], "files": [f]},
                                ensure_ascii=False) + "\n")

    print("upplösta: {}/{} ({:.1f} MB)".format(ok, len(rows), total / 1e6))
    for fid, f in rows:
        if f.get("error"):
            print("   {} -> {}".format(fid, f["error"]))
    print("-> {}".format(os.path.expanduser(args.out)))


if __name__ == "__main__":
    main()
