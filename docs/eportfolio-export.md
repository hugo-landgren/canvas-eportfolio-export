# Export av e-portfolios

Underlag inför avvecklingen av e-portfolios. Allt nedan är uppmätt mot
vårt eget **beta** 2026-08-26 med ett kontoadmin-token.
Verktyget är `tools/export_eportfolios.py`.

## Vad API:t ger

`GET /api/v1/users/:user_id/eportfolios` fungerar, och en kontoadmin får läsa
andras portfolios utan särskild moderatorrätt. Svaret är rena metadata:

```json
{
  "id": 6, "user_id": 5, "name": "My Portfolio", "public": false,
  "created_at": "2018-10-16T08:27:18Z", "updated_at": "2018-10-16T08:28:53Z",
  "workflow_state": "active", "deleted_at": null, "spam_status": null
}
```

Själva innehållet ligger i `GET /api/v1/eportfolios/:id/pages`, en post per sida
med `name`, `position` och `content`. `content` är antingen

* en lista av sektioner (det normala),
* `null` för en sida som aldrig fyllts i, eller
* en platshållarsträng -- `"Inget har angetts än"` eller `["No Content Added Yet"]`.
  Att den ena är en bar sträng och den andra en lista med en sträng är
  inkonsekvent men stabilt; behandla båda som tomma.

Sektionstyperna:

| `section_type` | Innehåll | Går att exportera |
| --- | --- | --- |
| `rich_text` | HTML i `content` | Ja, fullt ut |
| `html` | rå HTML i `content` | Ja, fullt ut |
| `attachment` | bara `attachment_id` | Ja, via `/files/:id` |
| `submission` | bara `submission_id` | Ja, via GraphQL (se nedan) |

## Volym på beta

Hela beståndet hos oss, hämtat på 8 minuter:

* **7 702 portfolios** fördelade på **6 754 ägare** (mest 14 st på en person)
* **14 374 sidor**, varav 94 % av portfolierna har reellt innehåll
* 6 985 publika, 717 privata; 3 markerade `marked_as_safe`, ingen som spam
* 603 bilageferenser, 159 inlämningsreferenser
* 16,5 MB JSON utan bilagefilerna

Skapade per år: 2018: 93, 2019: 217, 2020: 1 938, 2021: 2 209, 2022: 1 147,
2023: 786, 2024: 528, 2025: 466, 2026: 318. Tyngdpunkten ligger alltså på
2020--2022, men det tillkommer fortfarande ett par hundra om året.

## Produktion

Körd skarpt mot vår produktionsinstans 2026-08-27 med `--resolve-files`, klar på
**413 sekunder** (~20 id/s, ingen strypning vid åtta trådar). Siffrorna är
**identiska med beta** post för post -- samma högsta id (8 454), samma 7 702
portfolios, samma 14 374 sidor, samma årsfördelning. Beta är alltså en exakt
kopia, och mätningarna ovan gäller rakt av för produktion.

Utfallet:

* 7 702 portfolios, 7 702 unika id, 6 754 unika ägare
* 14 374 sidor -- **noll sidhämtningar misslyckades**
* 512 id svarade 403 (raderade), 240 svarade 404 (aldrig utdelade)
* 602 av 603 bilagor löstes upp; en är borta ur Canvas (404)
* 436 portfolios har bilagor, 65 har inlämningsreferenser
* 17,2 MB NDJSON exklusive bilagefilerna

Bilagorna är **1 066 MB** totalt, mestadels PDF (377) och Word (116), plus 20
filer med `application/fcw` (FeatureCAM) som kan behöva särskild hantering hos
mottagaren.

Vår utdata låg i `~/canvas-exports/eportfolios-prod-2026-08-27.ndjson`
-- medvetet utanför repot, eftersom den innehåller personuppgifter (namn,
e-post, och i minst ett fall personnummer i fritext) och inte ska checkas in.

## Hur exporten går till

Det finns **inget konto-index**: varken `/accounts/:id/eportfolios` eller
`/eportfolios` existerar (404). Den dokumenterade vägen är ett anrop per
användare, och hos oss är det 70 000+ användare varav ~2 % har någon portfolio
-- ungefär åtta timmar för att mestadels få tomma svar.

`GET /eportfolios/:id` fungerar däremot direkt, och id-rymden är tät och slutar
strax efter högsta befintliga portfolio (8 454 på beta). Att gå 1..max_id
kostar ~8 500 anrop i stället för 70 000: **8 minuter med åtta trådar** mot
åtta timmar. Ingen strypning märktes vid åtta trådar (~18 id/s).

Statuskoder vid id-iteration:

* `200` -- finns och är läsbar
* `403` -- raderad (`workflow_state: "deleted"`)
* `404` -- id:t har aldrig använts

Kontrollerat: ett användarsvep av 1 001 användare gav 40 aktiva och 10 raderade
portfolios. Id-iterationen fick med **samtliga 40 aktiva** och **inga** av de
raderade. Genvägen tappar alltså ingenting som är i bruk.

## Vad som inte kommer med

**Raderade portfolios.** De syns bara via
`/users/:user_id/eportfolios?include[]=deleted`, och även då svarar
`/eportfolios/:id/pages` 403 -- man får metadata men aldrig innehållet. Ska de
med krävs användarsvepet (`--via-users`), och innehållet går ändå inte att nå
via API:t. På beta var 512 av 8 500 id:n raderade.

**Inlämningar** -- *löst 2026-08-27, se eget avsnitt nedan.* Det stämmer att
REST inte kan slå upp dem, men GraphQL kan.

**Ägarnas namn** vid id-iteration -- posterna bär bara `user_id`. Slå upp
`/users/:id` för de 6 754 unika ägarna efteråt (~6 min), eller kör
`--via-users` som har med ägaruppgifterna direkt.

## Bilagor

`--resolve-files` slår upp varje `attachment_id` via `/files/:id` och lägger
filmetadatan i utdatan, inklusive en nedladdnings-URL:

```json
{"id": 1273, "display_name": "gurra.jpg", "content-type": "image/jpeg", "size": 358299,
 "url": "https://larosate.beta.instructure.com/files/1273/download?...&verifier=..."}
```

Två saker att veta: `verifier`-token i URL:en går ut med tiden, och en del
bilagor är helt borta -- de posterna får `{"id": ..., "error": 404}` i stället.

`tools/download_eportfolio_files.py` hämtar filerna till disk utifrån en färdig
export. Den provar verifier-URL:en först och faller tillbaka på
`/files/:id/download` med Bearer-token om verifieraren avvisas, så en gammal
export duger -- man behöver inte köra om exporten bara för att få nya URL:er.
Filer namnges `<attachment_id>-<filnamn>` (samma `display_name` återkommer
flitigt), storleken kontrolleras mot exportens `size`, och en fil som redan
ligger rätt på disk hoppas över så körningen kan återupptas.

### Skarp nedladdning 2026-08-27

**602 av 602 filer, 1 066 MB, på 144 sekunder. Noll misslyckanden**, och
samtliga gick via verifier-URL:en -- token-fallbacken behövde aldrig användas
(exporten var några minuter gammal). Alla filstorlekar matchar exportens `size`,
inga `.part`-rester.

En innehållskontroll av filsignaturer gav 528 av 529 rätt. Avvikaren var **inte** ett nedladdningsfel: filen ligger som
1 047 byte HTML i Canvas -- en Facebook-CDN-redirect som någon laddat upp i
stället för PDF:en 2019, och länken den pekar på gick ut i januari samma år.
Innehållet är alltså förlorat sedan länge och går inte att rädda ur Canvas.

## Inlämningar

`section_type: "submission"` bär bara ett `submission_id`, och REST kan inte
slå upp det: `/api/v1/submissions/:id` ger 404, och
`/courses/:c/assignments/:a/submissions/:u` kräver kurs- och uppgifts-id som
portfolion inte känner till.

**GraphQL löser det i ett anrop.** `legacyNode(_id:, type: Submission)` ger
kurs, uppgift, inlämnare, inlämnad text/URL och bilagor, och en kontoadmin får
läsa andras inlämningar utan särskild rätt:

```graphql
query($id: ID!){ legacyNode(_id: $id, type: Submission){ ... on Submission {
  _id submittedAt attempt submissionType url body
  assignment { _id name htmlUrl course { _id name courseCode } }
  user { _id name }
  attachments { _id displayName contentType size url }
} } }
```

Verktyget är `tools/resolve_eportfolio_submissions.py`. Mätt mot beta
2026-08-27, alla 159 referenser i exporten:

* **159 av 159 upplösta, noll misslyckade**, på 6 s (53 s med filuppslag)
* 65 portfolios berörda, en per ägare
* **samtliga tillhör portfolions egen ägare** -- inga andras inlämningar följer
  med på köpet, vilket vore ett integritetsproblem vid grupp-inlämningar
* 57 kurser, inlämnade 2018-12-04 till 2025-10-06
* typer: 123 `online_upload`, 8 `online_quiz`, 2 `online_text_entry`,
  1 `external_tool`, 25 utan typ (uppgiften finns, men inget lämnades in)
* innehåll: 123 med bilagor, 10 med text, 0 med URL, **26 helt tomma**
* **160 bilagor, 886 MB** -- mestadels PDF (118), plus zip (7), Word (8),
  Excel (4), text (12), bilder (5) och enstaka PowerPoint/Python

De 886 MB kommer **utöver** portfoliernas egna 1 066 MB: hela arkivet blir
alltså ~1,95 GB.

En fallgrop: GraphQL ger `size` som visningssträng (`"853 KB"`) och en URL utan
`verifier`. Verktyget slår därför upp varje bilaga via REST `/files/:id`, som
ger byte som heltal och en verifier-URL. Utdatan får samma filformat som
huvudexportens `attachments`, så `download_eportfolio_files.py` kan köras rakt
på den.

## Raderade portfolios -- fortsatt oåtkomliga

Sonderat igen 2026-08-27, med samma GraphQL-trick som löste inlämningarna. Det
går inte:

* `/eportfolios/:id`, `/eportfolios/:id/pages` och båda med `?include[]=deleted`
  ger alla **403** för en raderad portfolio
* GraphQL har **ingen `Eportfolio`-nodtyp** -- `NodeType` har 37 värden
  (Account, Assignment, Course, File, Submission, User, ...) och ingen av dem
  rör e-portfolios

Kvar finns bara Canvas återställningsväg,
`PUT /api/v1/users/:user_id/eportfolios/restore`, som återställer *alla*
raderade portfolios för en användare. Det är en **skrivoperation i produktion**
som återuppväcker material någon aktivt raderat, och den är trubbig -- den kan
inte riktas mot en enskild portfolio. Ska den vägen tas är det ett beslut om
lagringsändamål och samtycke, inte ett tekniskt val.

## Inbäddade filer -- ett hål i exporten

`attachment_ids` täcker bara `attachment`-sektioner. Lägger en student i
stället in en bild eller PDF *inuti* en rich_text-sektion blir det en vanlig
`<img src="/users/5/files/484931/preview">` i HTML:en, och det id:t syns
ingenstans i metadatan. De filerna laddades alltså aldrig ned.

Uppmätt på prod-exporten: **25 sådana filer i 10 portfolios, 15 MB**. Litet,
men de hade tyst försvunnit med Canvas. `tools/resolve_embedded_files.py`
plockar ut dem ur HTML:en och löser upp dem; utdatan matas in i
`download_eportfolio_files.py` som vanligt. Hämtade 2026-08-27: 25 av 25.

## HTML-arkiv

`tools/render_eportfolio_archive.py` gör ett läsbart arkiv av exporten: ett
sökbart index plus en sida per portfolio, med sidorna i ordning, bilagor och
inlämningar länkade till filerna på disk.

```
.venv/bin/python tools/render_eportfolio_archive.py \
    --export ~/canvas-exports/eportfolios-prod-2026-08-27.ndjson \
    --submissions ~/canvas-exports/submissions-prod-2026-08-27.ndjson \
    --files ~/canvas-exports/attachments \
    --files ~/canvas-exports/submission-attachments \
    --files ~/canvas-exports/embedded-attachments \
    --out ~/canvas-exports/arkiv
```

Kört 2026-08-27: **7 702 sidor, 42 MB HTML**, 781 filer nåbara från arkivet,
467 portfolios utan innehåll (renderas men är tomma).

Tre saker som är värda att veta om renderingen:

* **Skript stryks.** Innehållet är främmande HTML som öppnas lokalt från
  filsystemet; `<script>`, `on*`-attribut och `javascript:`-URL:er tas bort.
  Kontrollerat efteråt: noll träffar kvar i de 7 702 sidorna.
* **Länkar skrivs om.** Studenternas HTML är full av rotrelativa Canvas-länkar.
  De som pekar på något vi har på disk blir lokala sökvägar; övriga görs
  absoluta mot `--host` så de åtminstone fungerar så länge instansen finns.
* **Filerna kopieras inte** -- arkivet länkar relativt till filkatalogerna,
  eftersom de är 1,9 GB. Flytta hela `canvas-exports/` som en enhet.

## Personuppgifter

Utöver namn och e-postadresser: minst två portfolios hade personnummer i
`tel:`-länkar i fritext, och minst en i löpande text. Materialet -- NDJSON, filer och HTML-arkiv -- ska hanteras därefter och
ligger medvetet utanför git.

## Att hitta i arkivet

Indexet söker på portfolionamn, ägarens namn, CID, portfolio-id och ägar-id
samtidigt, och filtrerar på när portfolion skapades (`från`/`till`). Filtren
kombineras, och räknaren visar hur många av de 7 702 raderna som återstår.
Allt sker i webbläsaren -- arkivet ska fungera från filsystemet utan server.

Ägarnamnen kommer inte ur exporten. Id-iterationen frågar aldrig efter
användaren (det är just därför den är snabb), så posterna bär bara `user_id`.
`tools/resolve_eportfolio_owners.py` slår upp dem efteråt och skriver
`{user_id: {name, sortable_name, login_id}}`:

```
.venv/bin/python tools/resolve_eportfolio_owners.py \
    --env .env --export ~/canvas-exports/eportfolios-prod-2026-08-27.ndjson \
    --out ~/canvas-exports/owners-prod-2026-08-27.json
```

Filen matas sedan in i renderaren med `--owners`. Observera att `login_id` i
Canvas är **hela e-postadressen** (`namn@example.edu`), inte bara
användarnamnet. Arkivet visar därför bara delen före `@` -- annars blir indexet
en lista med tusentals e-postadresser i en och samma fil. Sökning på
användarnamnet fungerar ändå, och de fullständiga adresserna finns i
huvudexporten för den som behöver dem.

Uppmätt 2026-08-27: **6 754 av 6 754 ägare upplösta, noll utan träff**, på 426
sekunder (~16 uppslag/s). Ägare som hunnit raderas ur Canvas skulle ge 404 och
visas med sitt `user_id`; inga sådana fanns.
