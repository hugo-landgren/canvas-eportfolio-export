"""Engångsexport av alla e-portfolios ur Canvas.

Kör:
    .venv/bin/python tools/export_eportfolios.py --env .env --max-id 9000

Varför id-iteration och inte ett användarsvep
---------------------------------------------
Det finns inget konto-index för e-portfolios -- varken `/accounts/:id/eportfolios`
eller `/eportfolios` existerar (404). Den dokumenterade vägen är
`/users/:user_id/eportfolios`, alltså ett anrop per användare. På ett lärosäte
med 70 000+ användarkonton, varav ~2 % har någon portfolio, är det åtta timmar
för att mestadels få tomma svar.

`GET /eportfolios/:id` fungerar däremot direkt för en kontoadmin, och id-rymden
är tät och slutar strax efter högsta befintliga portfolio. Att gå 1..max_id
kostar ~8 500 anrop i stället för 70 000 och tar ~8 minuter med åtta trådar.
Kontrollerat mot ett användarsvep av 1 001 användare: id-iterationen fick med
samtliga 40 aktiva portfolios därifrån.

Statuskoder vid id-iteration
----------------------------
200  portfolion finns och är läsbar
403  portfolion är raderad (`workflow_state: "deleted"`)
404  id:t har aldrig använts

Raderade portfolios går alltså inte att nå den här vägen, och inte heller deras
sidor: `/eportfolios/:id/pages` svarar 403 för dem även när metadatan är
åtkomlig via `/users/:user_id/eportfolios?include[]=deleted`. Behövs de måste
exporten gå via användarsvepet -- se `--via-users`.

Vad som inte kommer med
-----------------------
`section_type: "submission"` ger bara ett `submission_id`, och det id:t går inte
att slå upp fristående (`/submissions/:id` -> 404). Ska inlämningarna med krävs
kurs- och uppgifts-id, vilket portfolion inte bär med sig.

`section_type: "attachment"` ger ett `attachment_id` som däremot löser sig fint
via `/files/:id` -- använd `--resolve-files` för att lägga filmetadata och
nedladdnings-URL i utdatan. URL:erna innehåller en `verifier`-token som går ut,
så ladda ned filerna i samma veva som exporten körs.
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv


def build_client(env_file):
    load_dotenv(env_file, override=True)
    from canvas_api import settings
    from canvas_api import CanvasClient

    s = settings()
    if not s.canvas_configured:
        sys.exit("CANVAS_API_TOKEN/CANVAS_HOST saknas i {}".format(env_file))
    return s, lambda: CanvasClient(s.canvas_host, s.canvas_token, s.canvas_account_id)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", default=".env", help="miljöfil att läsa (default: .env)")
    ap.add_argument("--out", default="eportfolios.ndjson", help="utfil, NDJSON")
    ap.add_argument("--max-id", type=int, default=0,
                    help="högsta portfolio-id att pröva (0 = leta upp det automatiskt)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--resolve-files", action="store_true",
                    help="slå upp varje attachment_id via /files/:id")
    ap.add_argument("--via-users", action="store_true",
                    help="svep kontots användare i stället; långsamt, men får med raderade")
    args = ap.parse_args()

    s, make_client = build_client(args.env)
    from canvas_api import ApiError

    print("miljö: {} ({})".format(s.canvas_host, s.environment))
    if not s.is_beta:
        print("VARNING: detta är produktion. Exporten är read-only, men dubbelkolla.")

    local = threading.local()

    def client():
        if not hasattr(local, "c"):
            local.c = make_client()
        return local.c

    lock = threading.Lock()
    stats = Counter()
    sections = Counter()
    t0 = time.time()
    out = open(args.out, "w", encoding="utf-8")

    def collect_pages(c, epid):
        try:
            return list(c.paginate("/eportfolios/{}/pages".format(epid))), None
        except ApiError as e:
            return [], e.status

    def annotate(ep, pages):
        """Räkna sektionstyper och plocka ut referenser som pekar utanför portfolion."""
        attachments, submissions = [], []
        for p in pages:
            content = p.get("content")
            if isinstance(content, list):
                for sec in content:
                    if not isinstance(sec, dict):
                        # Tomma sidor kommer tillbaka som ["No Content Added Yet"].
                        sections["_platshallare"] += 1
                        continue
                    kind = sec.get("section_type")
                    sections[kind] += 1
                    if kind == "attachment":
                        attachments.append(sec.get("attachment_id"))
                    elif kind == "submission":
                        submissions.append(sec.get("submission_id"))
            elif content is None:
                sections["_tom"] += 1
            else:
                sections["_platshallare"] += 1
        ep["pages"] = pages
        ep["attachment_ids"] = attachments
        ep["submission_ids"] = submissions
        return ep

    def resolve_files(c, ep):
        files = []
        for aid in ep["attachment_ids"]:
            try:
                files.append(c.get("/files/{}".format(aid)))
            except ApiError as e:
                files.append({"id": aid, "error": e.status})
                stats["file:%s" % e.status] += 1
        ep["files"] = files

    def write(ep):
        with lock:
            out.write(json.dumps(ep, ensure_ascii=False) + "\n")
            stats["portfolios"] += 1
            stats["sidor"] += len(ep["pages"])
            done = stats["portfolios"] + stats["saknas"]
            if done % 500 == 0:
                out.flush()
                el = time.time() - t0
                print("  {:>6} klara | {} portfolios | {:.1f}/s".format(
                    done, stats["portfolios"], done / el), flush=True)

    def by_id(epid):
        c = client()
        try:
            ep = c.get("/eportfolios/{}".format(epid))
        except ApiError as e:
            with lock:
                stats["saknas"] += 1
                stats["meta:%s" % e.status] += 1
            return
        pages, perr = collect_pages(c, epid)
        annotate(ep, pages)
        if perr:
            ep["pages_error"] = perr
            with lock:
                stats["pages:%s" % perr] += 1
        if args.resolve_files:
            resolve_files(c, ep)
        write(ep)

    def find_max_id(c):
        """Dubbla uppåt tills id:t inte längre finns, binärsök sedan gränsen."""
        hi = 1024
        while hi < (1 << 22):
            try:
                c.get("/eportfolios/{}".format(hi))
            except ApiError as e:
                if e.status == 404:
                    break
            hi *= 2
        lo = 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            try:
                c.get("/eportfolios/{}".format(mid))
                lo = mid
            except ApiError as e:
                if e.status == 403:  # raderad, men id:t är utdelat -- räknas som träff
                    lo = mid
                else:
                    hi = mid - 1
        return lo

    if args.via_users:
        c = make_client()
        n = 0
        for u in c.account_users():
            n += 1
            try:
                eps = c.get("/users/{}/eportfolios".format(u["id"]), {"include[]": "deleted"})
            except ApiError as e:
                with lock:
                    stats["lista:%s" % e.status] += 1
                continue
            for ep in eps:
                pages, perr = collect_pages(c, ep["id"])
                annotate(ep, pages)
                if perr:
                    ep["pages_error"] = perr
                ep["owner"] = {k: u.get(k) for k in
                               ("id", "name", "sortable_name", "login_id", "sis_user_id", "email")}
                if args.resolve_files:
                    resolve_files(c, ep)
                write(ep)
            if n % 1000 == 0:
                print("  {} användare svepta".format(n), flush=True)
        stats["användare"] = n
    else:
        max_id = args.max_id or find_max_id(make_client())
        print("högsta portfolio-id:", max_id)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(by_id, range(1, max_id + 1)))

    out.close()
    el = time.time() - t0
    print("\n--- klart på {:.0f}s ---".format(el))
    print("portfolios   :", stats["portfolios"])
    print("sidor        :", stats["sidor"])
    print("statuskoder  :", {k: v for k, v in stats.items() if ":" in k})
    print("sektionstyper:", dict(sections))
    print("utfil        : {} ({} byte)".format(args.out, os.path.getsize(args.out)))


if __name__ == "__main__":
    main()
