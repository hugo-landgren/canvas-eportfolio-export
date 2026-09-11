#!/usr/bin/env python3
"""Renderar e-portfolioexporten till ett läsbart HTML-arkiv.

    .venv/bin/python tools/render_eportfolio_archive.py \
        --export ~/canvas-exports/eportfolios-prod-2026-08-27.ndjson \
        --submissions ~/canvas-exports/submissions-prod-2026-08-27.ndjson \
        --owners ~/canvas-exports/owners-prod-2026-08-27.json \
        --files ~/canvas-exports/attachments \
        --files ~/canvas-exports/submission-attachments \
        --files ~/canvas-exports/embedded-attachments \
        --out ~/canvas-exports/arkiv

NDJSON + filkatalog duger för maskinell migrering, men inte för en människa som
vill läsa vad som faktiskt fanns i en portfolio. Det här skriptet gör en sida
per portfolio plus ett sökbart index, med bilagor och inlämningar länkade till
filerna på disk.

Arkivet länkar **relativt** till filkatalogerna i stället för att kopiera dem --
de är ~1,9 GB. Flytta hela `canvas-exports/` som en enhet så håller länkarna.

Ingen nätverkstrafik: allt kommer från filerna på disk.
"""
import argparse
import html
import json
import os
import re
import sys
from collections import defaultdict

# Canvas-HTML är användarskapad. Arkivet öppnas lokalt från filsystemet, så
# skript och händelseattribut plockas bort -- annars kör man tio år gammal
# främmande JavaScript i sin egen webbläsare.
SCRIPT = re.compile(r"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
ON_ATTR = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
JS_URL = re.compile(r"(href|src)\s*=\s*(\"|')\s*javascript:[^\"']*(\"|')", re.IGNORECASE)

PLACEHOLDERS = {"nothing entered yet", "inget har angetts än", "no content added yet"}

CSS = """
:root { --bg:#fff; --fg:#1a1a1a; --muted:#666; --line:#e2e2e2; --accent:#0b5fff; --card:#fafafa; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#16181c; --fg:#e8e8e8; --muted:#9aa0a6; --line:#2c2f36; --accent:#7aa7ff; --card:#1d2026; }
}
* { box-sizing: border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--bg); color:var(--fg);
  font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
.wrap { max-width: 62rem; margin: 0 auto; }
a { color: var(--accent); }
h1 { font-size:1.6rem; margin:0 0 .25rem; } h2 { font-size:1.15rem; margin:2rem 0 .5rem; }
.meta { color:var(--muted); font-size:.9rem; margin-bottom:1.5rem; }
.page { border:1px solid var(--line); border-radius:10px; padding:1rem 1.25rem; margin:1rem 0; background:var(--card); }
.page > h2 { margin-top:0; }
.section { margin:1rem 0; }
.section img, .body img { max-width:100%; height:auto; }
.sub { border-left:3px solid var(--accent); padding:.5rem 0 .5rem 1rem; }
.sub .k { color:var(--muted); font-size:.85rem; }
.file { display:inline-block; padding:.35rem .6rem; border:1px solid var(--line);
  border-radius:6px; margin:.2rem .3rem .2rem 0; background:var(--bg); text-decoration:none; }
.empty { color:var(--muted); font-style:italic; }
.warn { color:#b45309; }
table { border-collapse:collapse; width:100%; font-size:.92rem; }
th,td { text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line); vertical-align:top; }
th { position:sticky; top:0; background:var(--bg); cursor:pointer; }
#q { width:100%; padding:.6rem .8rem; font-size:1rem; margin:1rem 0; border:1px solid var(--line);
  border-radius:8px; background:var(--card); color:var(--fg); }
.tablewrap { overflow-x:auto; }
.filters { display:flex; gap:.75rem; align-items:center; flex-wrap:wrap; margin-bottom:1rem; }
.filters label { color:var(--muted); font-size:.9rem; }
.filters input[type=date], .filters button { padding:.35rem .5rem; border:1px solid var(--line);
  border-radius:6px; background:var(--card); color:var(--fg); font:inherit; font-size:.9rem; }
.filters button { cursor:pointer; }
.pill { font-size:.78rem; padding:.1rem .45rem; border:1px solid var(--line); border-radius:999px; color:var(--muted); }
"""


ROOT_LINK = re.compile(r'((?:href|src)=")(/[^"]*)(")', re.IGNORECASE)
FILE_PATH = re.compile(r"^/(?:users/\d+/|courses/\d+/)?files/(\d+)")
PF_PATH = re.compile(r"^/eportfolios/(\d+)")


def rewrite_links(raw, ctx):
    """Peka om Canvas-interna länkar mot arkivet.

    Studenternas HTML är full av rotrelativa länkar (`/eportfolios/123/...`,
    `/users/5/files/99/preview`). I ett arkiv som öppnas från filsystemet pekar
    de på ingenting. De som går att matcha mot något vi har på disk skrivs om
    till lokala sökvägar; resten görs absoluta mot Canvas, så de åtminstone är
    meningsfulla så länge instansen finns kvar.
    """
    def sub(m):
        pre, path, post = m.group(1), m.group(2), m.group(3)
        fm = FILE_PATH.match(path)
        if fm and fm.group(1) in ctx["manifest"]:
            row = ctx["manifest"][fm.group(1)]
            return "{}{}/{}{}".format(pre, row["rel"], row["fil"], post)
        pm = PF_PATH.match(path)
        if pm and int(pm.group(1)) in ctx["ids"]:
            return "{}{}.html{}".format(pre, pm.group(1), post)
        return "{}{}{}{}".format(pre, ctx["host"], path, post)
    return ROOT_LINK.sub(sub, raw)


def clean_html(raw, ctx=None):
    if not raw:
        return ""
    out = SCRIPT.sub("", raw)
    out = ON_ATTR.sub("", out)
    out = JS_URL.sub(r"\1=\2#\3", out)
    if ctx:
        out = rewrite_links(out, ctx)
    return out


def is_placeholder(content):
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip().lower() in PLACEHOLDERS or not content.strip()
    if isinstance(content, list) and len(content) == 1 and isinstance(content[0], str):
        return content[0].strip().lower() in PLACEHOLDERS
    return False


def load_manifests(dirs, link_from):
    """id -> filpost för allt som faktiskt ligger på disk, oavsett katalog.

    Bilagor, inlämningsfiler och inbäddade filer ligger i var sin katalog men
    delar id-rymd, så de slås ihop till ett uppslag. Varje post får med sin
    sökväg relativt `link_from`, så länkarna pekar rätt oavsett var katalogen
    ligger.
    """
    merged = {}
    for path in dirs:
        path = os.path.expanduser(path)
        mf = os.path.join(path, "manifest.json")
        if not os.path.exists(mf):
            print("varning: {} saknar manifest.json, hoppas över".format(path))
            continue
        rel = os.path.relpath(path, link_from)
        with open(mf, encoding="utf-8") as fh:
            rows = json.load(fh)
        for r in rows:
            if r.get("status") == "ok" and r.get("fil"):
                merged[str(r["id"])] = dict(r, rel=rel)
    return merged


def esc(v):
    return html.escape("" if v is None else str(v))


def file_link(manifest, file_id, display_name=None, content_type=None):
    """Länk till filen på disk, eller en notis om att den saknas."""
    row = manifest.get(str(file_id)) or {}
    name = row.get("fil")
    # Manifestet vet content-typen även när anroparen inte gör det, vilket är
    # fallet för attachment-sektioner -- de bär bara ett id.
    content_type = content_type or row.get("content_type")
    label = display_name or name or "fil {}".format(file_id)
    if not name:
        return '<span class="file warn" title="finns inte i arkivet">{} (saknas)</span>'.format(esc(label))
    href = "{}/{}".format(row["rel"], name)
    if (content_type or "").startswith("image/"):
        return '<div class="section"><img src="{}" alt="{}"><br><a class="file" href="{}">{}</a></div>'.format(
            esc(href), esc(label), esc(href), esc(label))
    return '<a class="file" href="{}">{}</a>'.format(esc(href), esc(label))


def render_sections(content, ctx):
    if is_placeholder(content):
        return '<p class="empty">Tom sida.</p>'
    if isinstance(content, str):
        return '<div class="body">{}</div>'.format(clean_html(content, ctx))
    parts = []
    for sec in content or []:
        if not isinstance(sec, dict):
            continue
        kind = sec.get("section_type")
        if kind in ("rich_text", "html"):
            body = clean_html(sec.get("content"), ctx)
            if body.strip():
                parts.append('<div class="section body">{}</div>'.format(body))
        elif kind == "attachment":
            fid = sec.get("attachment_id")
            parts.append('<div class="section">{}</div>'.format(
                file_link(ctx["manifest"], fid)))
        elif kind == "submission":
            parts.append(render_submission(ctx["subs_by_id"].get(sec.get("submission_id")),
                                           sec.get("submission_id"), ctx))
        else:
            parts.append('<p class="empty">Sektion av okänd typ: {}</p>'.format(esc(kind)))
    return "\n".join(parts) or '<p class="empty">Tom sida.</p>'


def course_label(sub):
    """Kurskod + namn, men bara en gång -- namnet inleds ofta med koden."""
    code = (sub.get("course_code") or "").strip()
    name = (sub.get("course_name") or "").strip()
    if code and name.lower().startswith(code.lower()):
        return name
    return " ".join(x for x in [code, name] if x)


def render_submission(sub, sub_id, ctx):
    if not sub or sub.get("error"):
        return '<div class="section sub warn">Inlämning {} kunde inte hämtas.</div>'.format(esc(sub_id))
    bits = ['<div class="section sub">']
    bits.append('<div class="k">Inlämning i {} &middot; uppgift: {}</div>'.format(
        esc(course_label(sub) or "okänd kurs"), esc(sub.get("assignment_name") or "-")))
    when = (sub.get("submitted_at") or "")[:10]
    bits.append('<div class="k">{}{}</div>'.format(
        "Inlämnad {}".format(esc(when)) if when else "Aldrig inlämnad",
        " &middot; {}".format(esc(sub.get("submission_type"))) if sub.get("submission_type") else ""))
    if sub.get("body"):
        bits.append('<div class="body">{}</div>'.format(clean_html(sub["body"], ctx)))
    if sub.get("url"):
        bits.append('<p><a href="{0}">{0}</a></p>'.format(esc(sub["url"])))
    files = [f for f in sub.get("files") or [] if not f.get("error")]
    if files:
        bits.append("<p>" + " ".join(
            file_link(ctx["manifest"], f.get("id"), f.get("display_name"), f.get("content-type"))
            for f in files) + "</p>")
    if not (sub.get("body") or sub.get("url") or files):
        bits.append('<p class="empty">Inget innehåll lämnades in.</p>')
    bits.append("</div>")
    return "\n".join(bits)


def page_shell(title, body):
    return ("<!doctype html><html lang=\"sv\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>{}</title><style>{}</style></head><body><div class=\"wrap\">{}</div></body></html>"
            ).format(esc(title), CSS, body)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", required=True)
    ap.add_argument("--submissions", help="NDJSON från resolve_eportfolio_submissions.py")
    ap.add_argument("--owners", help="JSON från resolve_eportfolio_owners.py")
    ap.add_argument("--files", action="append", default=[], metavar="KATALOG",
                    help="katalog med nedladdade filer (upprepas: bilagor, "
                         "inlämningsfiler, inbäddade filer)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--host", required=True,
                    help="din Canvas-instans, t.ex. https://canvas.example.edu -- "
                         "länkar som inte pekar på något vi har på disk görs "
                         "absoluta mot den")
    args = ap.parse_args()

    out = os.path.expanduser(args.out)
    pf_dir = os.path.join(out, "portfolios")
    os.makedirs(pf_dir, exist_ok=True)

    # Länkarna skrivs relativt sidorna i out/portfolios/.
    manifest = load_manifests(args.files, pf_dir)

    owners = {}
    if args.owners:
        with open(os.path.expanduser(args.owners), encoding="utf-8") as fh:
            owners = json.load(fh)

    subs_by_id = {}
    subs_by_pf = defaultdict(list)
    if args.submissions:
        with open(os.path.expanduser(args.submissions), encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                subs_by_id[row.get("submission_id")] = row
                subs_by_pf[row.get("eportfolio_id")].append(row)

    export_path = os.path.expanduser(args.export)
    # Vilka portfolios finns? Behövs för att veta om en /eportfolios/<id>-länk
    # inne i någons HTML går att peka om till en lokal sida.
    ids = set()
    with open(export_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                ids.add(json.loads(line).get("id"))
            except ValueError:
                continue

    ctx = {"manifest": manifest, "subs_by_id": subs_by_id, "ids": ids,
           "host": args.host.rstrip("/")}

    index = []
    rendered = skipped = 0
    with open(export_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                pf = json.loads(line)
            except ValueError:
                continue
            pages = sorted(pf.get("pages") or [], key=lambda p: p.get("position") or 0)
            owner = owners.get(str(pf.get("user_id"))) or {}
            owner_name = owner.get("name") or ""
            # login_id är hela adressen (namn@example.edu); bara delen före @
            # visas, så indexet inte blir en lista med tusentals e-postadresser.
            # Sökning på användarnamnet fungerar ändå, och de fullständiga
            # adresserna finns i huvudexporten för den som behöver dem.
            cid = (owner.get("login_id") or "").split("@")[0]
            owner_label = "{} ({})".format(owner_name, cid) if owner_name and cid else owner_name

            body = ['<h1>{}</h1>'.format(esc(pf.get("name") or "Namnlös portfolio"))]
            body.append('<div class="meta">Portfolio {} &middot; ägare {} &middot; skapad {} &middot; '
                        '<span class="pill">{}</span></div>'.format(
                            esc(pf.get("id")),
                            esc(owner_label or pf.get("user_id")),
                            esc((pf.get("created_at") or "")[:10]),
                            "publik" if pf.get("public") else "privat"))
            body.append('<p><a href="../index.html">&larr; Tillbaka till index</a></p>')
            has_content = False
            for page in pages:
                inner = render_sections(page.get("content"), ctx)
                if 'class="empty">Tom sida' not in inner:
                    has_content = True
                body.append('<div class="page"><h2>{}</h2>{}</div>'.format(
                    esc(page.get("name") or "Namnlös sida"), inner))
            if not pages:
                body.append('<p class="empty">Portfolion har inga sidor.</p>')

            path = os.path.join(pf_dir, "{}.html".format(pf.get("id")))
            with open(path, "w", encoding="utf-8") as out_fh:
                out_fh.write(page_shell(pf.get("name") or "Portfolio {}".format(pf.get("id")),
                                        "\n".join(body)))
            rendered += 1
            if not has_content:
                skipped += 1
            index.append({
                "id": pf.get("id"), "name": pf.get("name") or "",
                "user_id": pf.get("user_id"), "pages": len(pages),
                "created": (pf.get("created_at") or "")[:10],
                "public": bool(pf.get("public")),
                "files": len(pf.get("attachment_ids") or []),
                "subs": len(subs_by_pf.get(pf.get("id")) or []),
                "content": has_content,
                "owner": owner_label,
            })

    index.sort(key=lambda r: (r["name"].lower(), r["id"]))
    rows = "\n".join(
        '<tr data-d="{created}"><td><a href="portfolios/{id}.html">{name}</a></td>'
        '<td>{owner}</td><td>{id}</td><td>{user}</td>'
        '<td>{pages}</td><td>{files}</td><td>{subs}</td><td>{created}</td>'
        '<td>{vis}</td></tr>'.format(
            id=esc(r["id"]), name=esc(r["name"] or "(namnlös)"), user=esc(r["user_id"]),
            owner=esc(r["owner"]), pages=r["pages"], files=r["files"], subs=r["subs"],
            created=esc(r["created"]), vis="publik" if r["public"] else "privat")
        for r in index)

    dates = sorted(r["created"] for r in index if r["created"])
    lo, hi = (dates[0], dates[-1]) if dates else ("", "")
    with_content = sum(1 for r in index if r["content"])
    named = sum(1 for r in index if r["owner"])
    head = ('<h1>E-portfolios &ndash; arkiv</h1>'
            '<div class="meta">{total} portfolios, varav {content} med innehåll. '
            '{named} har ägarnamn upplöst. '
            'Filerna ligger utanför den här katalogen; flytta hela <code>canvas-exports/</code> '
            'som en enhet så håller länkarna.</div>'
            '<input id="q" placeholder="Sök på portfolionamn, ägare, CID, id eller ägar-id…" autofocus>'
            '<div class="filters">'
            '<label>Skapad från <input type="date" id="from" min="{lo}" max="{hi}"></label>'
            '<label>till <input type="date" id="to" min="{lo}" max="{hi}"></label>'
            '<button type="button" id="clear">Rensa</button>'
            '<span id="count" class="meta"></span></div>'
            '<div class="tablewrap"><table id="t"><thead><tr>'
            '<th>Portfolio</th><th>Ägare</th><th>Id</th><th>Ägar-id</th><th>Sidor</th><th>Filer</th>'
            '<th>Inlämningar</th><th>Skapad</th><th>Synlighet</th>'
            '</tr></thead><tbody>{rows}</tbody></table></div>').format(
                total=len(index), content=with_content, named=named,
                lo=esc(lo), hi=esc(hi), rows=rows)

    # Filtret är rent klientsidigt: 7 702 rader är för lite för att motivera
    # något mer, och arkivet ska fungera från filsystemet utan server.
    script = ("<script>"
              "const q=document.getElementById('q'),f=document.getElementById('from'),"
              "t2=document.getElementById('to'),c=document.getElementById('count'),"
              "rows=[...document.querySelectorAll('#t tbody tr')],"
              "hay=rows.map(r=>r.textContent.toLowerCase());"
              "function apply(){const v=q.value.trim().toLowerCase(),a=f.value,b=t2.value;let n=0;"
              "rows.forEach((r,i)=>{const d=r.dataset.d;"
              "const ok=(!v||hay[i].includes(v))&&(!a||(d&&d>=a))&&(!b||(d&&d<=b));"
              "r.style.display=ok?'':'none';if(ok)n++;});"
              "c.textContent=n===rows.length?'':n+' av '+rows.length+' visas';}"
              "[q,f,t2].forEach(e=>e.addEventListener('input',apply));"
              "document.getElementById('clear').addEventListener('click',()=>{"
              "q.value='';f.value='';t2.value='';apply();q.focus();});"
              "</script>")

    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page_shell("E-portfolios – arkiv", head + script))

    print("renderade {} portfolios ({} utan innehåll)".format(rendered, skipped))
    print("filer tillgängliga i arkivet: {} från {} kataloger".format(
        len(manifest), len(args.files)))
    print("-> {}/index.html".format(out))


if __name__ == "__main__":
    main()
