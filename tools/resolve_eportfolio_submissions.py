#!/usr/bin/env python3
"""Löser upp e-portfoliernas inlämningsreferenser via Canvas GraphQL.

    .venv/bin/python tools/resolve_eportfolio_submissions.py \
        --env .env.beta --export ~/canvas-exports/eportfolios-prod-2026-08-27.ndjson \
        --out ~/canvas-exports/submissions-beta.ndjson

Bakgrund: en `submission`-sektion i en e-portfolio bär bara ett `submission_id`.
REST kan inte slå upp det fristående -- `/api/v1/submissions/:id` ger 404, och
`/courses/:c/assignments/:a/submissions/:u` kräver kurs- och uppgifts-id som
portfolion inte bär med sig. GraphQL `legacyNode(_id:, type: Submission)` gör
det däremot i ett anrop, och en kontoadmin får läsa andras inlämningar.

Utdata är en NDJSON-rad per inlämningsreferens med kurs, uppgift, ägare,
inlämnad text/URL och bilagemetadata. Bilagefilerna laddas *inte* ned här --
kör `download_eportfolio_files.py` på utdatan för det, precis som för
portfoliernas egna bilagor.

Read-only. Rör inga inlämningar och lämnar inga betyg.
"""
import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

QUERY = """query($id: ID!){
  legacyNode(_id: $id, type: Submission){
    ... on Submission {
      _id
      submittedAt
      attempt
      submissionType
      url
      body
      assignment { _id name htmlUrl course { _id name courseCode } }
      user { _id name sortableName }
      attachments { _id displayName contentType size url createdAt }
    }
  }
}"""


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


def submission_refs(export_path):
    """(submission_id, portfolio_id, portfolio_user_id, sidnamn) ur en export."""
    refs = []
    with open(export_path) as fh:
        for line in fh:
            try:
                pf = json.loads(line)
            except ValueError:
                continue
            for page in pf.get("pages") or []:
                content = page.get("content")
                if not isinstance(content, list):
                    continue
                for sec in content:
                    if isinstance(sec, dict) and sec.get("section_type") == "submission":
                        sid = sec.get("submission_id")
                        if sid is not None:
                            refs.append((sid, pf.get("id"), pf.get("user_id"), page.get("name")))
    return refs


def resolve_file(host, sess, file_id):
    """Filmetadata via REST.

    GraphQL ger `size` som visningssträng ("853 KB") och en URL utan verifier,
    vilket varken går att storlekskontrollera eller hämta anonymt. REST
    `/api/v1/files/:id` ger byte som heltal och samma verifier-URL som
    huvudexporten, så utdatan blir utbytbar med den.
    """
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


def resolve(host, sess, sid):
    url = "https://{}/api/graphql".format(host)
    for attempt in range(3):
        try:
            r = sess.post(url, json={"query": QUERY, "variables": {"id": str(sid)}}, timeout=60)
        except Exception as exc:  # nätverksglapp -- försök igen
            if attempt == 2:
                return {"error": "request", "detail": str(exc)}
            time.sleep(1 + attempt)
            continue
        if r.status_code == 403 and attempt < 2:  # strypning
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code != 200:
            return {"error": "http", "status": r.status_code}
        try:
            data = r.json()
        except ValueError:
            return {"error": "json", "status": r.status_code}
        node = (data.get("data") or {}).get("legacyNode")
        if node:
            return node
        return {"error": "not_found", "detail": (data.get("errors") or [{}])[0].get("message")}
    return {"error": "retries"}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env", help="miljöfil (default: .env)")
    ap.add_argument("--export", required=True, help="NDJSON-export från export_eportfolios.py")
    ap.add_argument("--out", required=True, help="NDJSON att skriva")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="stanna efter N referenser (test)")
    args = ap.parse_args()

    host, sess = build_session(args.env)
    refs = submission_refs(os.path.expanduser(args.export))
    if args.limit:
        refs = refs[: args.limit]
    print("miljö: {}".format(host))
    print("inlämningsreferenser: {} i {} portfolios".format(
        len(refs), len({r[1] for r in refs})))

    started = time.time()
    out_path = os.path.expanduser(args.out)
    ok = failed = foreign = 0
    attachments = file_errors = total_bytes = 0
    with open(out_path, "w") as out, ThreadPoolExecutor(max_workers=args.threads) as pool:
        results = pool.map(lambda r: (r, resolve(host, sess, r[0])), refs)
        for (sid, pf_id, pf_user, page_name), node in results:
            row = {
                "submission_id": sid,
                "eportfolio_id": pf_id,
                "eportfolio_user_id": pf_user,
                "page_name": page_name,
            }
            if node.get("error"):
                row["error"] = node
                failed += 1
            else:
                assignment = node.get("assignment") or {}
                course = assignment.get("course") or {}
                user = node.get("user") or {}
                files = [resolve_file(host, sess, f.get("_id"))
                         for f in (node.get("attachments") or [])]
                attachments += len(files)
                file_errors += sum(1 for f in files if f.get("error"))
                total_bytes += sum(f.get("size") or 0 for f in files if not f.get("error"))
                # En grupp-inlämning kan tillhöra någon annan än portfolions ägare.
                own = str(user.get("_id") or "") == str(pf_user or "")
                if not own:
                    foreign += 1
                row.update({
                    "course_id": course.get("_id"),
                    "course_name": course.get("name"),
                    "course_code": course.get("courseCode"),
                    "assignment_id": assignment.get("_id"),
                    "assignment_name": assignment.get("name"),
                    "assignment_url": assignment.get("htmlUrl"),
                    "submitted_at": node.get("submittedAt"),
                    "attempt": node.get("attempt"),
                    "submission_type": node.get("submissionType"),
                    "url": node.get("url"),
                    "body": node.get("body"),
                    "submitter_user_id": user.get("_id"),
                    "submitter_name": user.get("name"),
                    "submitter_is_portfolio_owner": own,
                    # Nyckeln heter "files" och inte "attachments" för att
                    # download_eportfolio_files.py ska kunna köras rakt på den
                    # här filen, precis som på huvudexporten.
                    "files": files,
                })
                ok += 1
            out.write(json.dumps(row, ensure_ascii=False) + "\n")

    print("upplösta: {}/{}  misslyckade: {}".format(ok, len(refs), failed))
    print("bilagor i inlämningarna: {} ({:.0f} MB, {} kunde inte slås upp)".format(
        attachments, total_bytes / 1e6, file_errors))
    print("inlämningar som tillhör någon annan än portfolions ägare: {}".format(foreign))
    print("klart på {:.0f} s -> {}".format(time.time() - started, out_path))


if __name__ == "__main__":
    main()
