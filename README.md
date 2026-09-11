# Canvas e-portfolio-export

Verktyg för att exportera **alla e-portfolios** ur en Canvas-instans: metadata,
sidinnehåll, bilagor, inlämningar som portfolierna hänvisar till, och de filer
som ligger inbäddade i studenternas HTML. Utdatan är NDJSON plus en filkatalog
för maskinell migrering, och ett sökbart HTML-arkiv för läsning.

Skrivet inför avvecklingen av e-portfolios på ett svenskt lärosäte, och delat
för att andra står inför samma sak. Allt är **read-only** mot Canvas.

Körd skarpt mot en instans med 70 000+ användarkonton: **7 702 portfolios,
14 374 sidor och 1,9 GB filer på under 15 minuter**, noll misslyckade
sidhämtningar.

## Det här är problemet verktyget löser

Canvas har **inget konto-index för e-portfolios**. Varken
`/accounts/:id/eportfolios` eller `/eportfolios` finns (404), så den
dokumenterade vägen är `/users/:user_id/eportfolios` — ett anrop per användare.
Med 70 000 användare varav ~2 % har en portfolio blir det åtta timmar för att
mestadels få tomma svar.

`GET /eportfolios/:id` fungerar däremot direkt för en kontoadmin, och id-rymden
är tät. Att gå `1..max_id` kostar ~8 500 anrop i stället för 70 000: **sju
minuter i stället för åtta timmar**. Verktyget hittar `max_id` själv genom att
dubbla uppåt och binärsöka gränsen.

Kontrollerat mot ett användarsvep av 1 001 användare: id-iterationen fick med
samtliga aktiva portfolios därifrån. Genvägen tappar ingenting som är i bruk.

`docs/eportfolio-export.md` har hela kartläggningen — vad API:t ger, vad det
inte ger, och varje mätning bakom siffrorna ovan. **Läs den innan du kör.**

## Kom igång

Kräver Python 3.9+, ett **kontoadmin-token** och läsrätt på kontot.

```bash
git clone <detta repo> && cd canvas-eportfolio-export
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # fyll i CANVAS_HOST och CANVAS_API_TOKEN
```

Prova mot er betainstans först om ni har en. Den är oftast en exakt kopia av
produktion, och då gäller mätvärdena rakt av.

## Hela kedjan

Stegen körs i ordning. Varje steg läser föregående stegs utdata, och alla utom
det sista kräver token.

```bash
V=.venv/bin/python
OUT=~/canvas-exports          # medvetet utanför repot -- se Personuppgifter

# 1. Portfolios, sidor och bilagemetadata  (~7 min)
$V tools/export_eportfolios.py --env .env --resolve-files \
     --out $OUT/eportfolios.ndjson

# 2. Bilagefilerna till disk  (~2,5 min, 1 GB hos oss)
$V tools/download_eportfolio_files.py --env .env \
     --export $OUT/eportfolios.ndjson --dest $OUT/attachments

# 3. Inlämningar som portfolierna hänvisar till, via GraphQL  (~1 min)
$V tools/resolve_eportfolio_submissions.py --env .env \
     --export $OUT/eportfolios.ndjson --out $OUT/submissions.ndjson
$V tools/download_eportfolio_files.py --env .env \
     --export $OUT/submissions.ndjson --dest $OUT/submission-attachments

# 4. Filer inbäddade i rich text -- syns inte i metadatan  (sekunder)
$V tools/resolve_embedded_files.py --env .env \
     --export $OUT/eportfolios.ndjson --out $OUT/embedded.ndjson
$V tools/download_eportfolio_files.py --env .env \
     --export $OUT/embedded.ndjson --dest $OUT/embedded-attachments

# 5. Ägarnas namn -- exporten bär bara user_id  (~7 min)
$V tools/resolve_eportfolio_owners.py --env .env \
     --export $OUT/eportfolios.ndjson --out $OUT/owners.json

# 6. Läsbart HTML-arkiv  (ingen token, ingen nätverksåtkomst)
$V tools/render_eportfolio_archive.py \
     --export $OUT/eportfolios.ndjson \
     --submissions $OUT/submissions.ndjson \
     --owners $OUT/owners.json \
     --files $OUT/attachments \
     --files $OUT/submission-attachments \
     --files $OUT/embedded-attachments \
     --host https://canvas.example.edu \
     --out $OUT/arkiv
```

Steg 2–5 går att köra i valfri ordning efter steg 1, och nedladdningarna kan
återupptas: en fil vars storlek på disk redan matchar exportens `size` hoppas
över.

## Verktygen

| Verktyg | Gör | Token |
| --- | --- | --- |
| `export_eportfolios.py` | Portfolios, sidor, bilagemetadata. `--via-users` sveper användare i stället (långsamt, men ser raderade). | ja |
| `download_eportfolio_files.py` | Hämtar bilagefiler. Verifier-URL först, Bearer-token som fallback, återupptagbart. | ja |
| `resolve_eportfolio_submissions.py` | Slår upp `submission_id` via GraphQL — REST kan inte. | ja |
| `resolve_embedded_files.py` | Gräver ut filer som ligger i rich text-HTML och inte syns i metadatan. | ja |
| `resolve_eportfolio_owners.py` | `user_id` → namn och användarnamn. | ja |
| `render_eportfolio_archive.py` | Sökbart HTML-arkiv, en sida per portfolio. | nej |

`canvas_api.py` är en minimal Canvas-klient — sidbrytning via `Link`-huvuden
och avbackning vid strypning. Inga andra beroenden än `requests` och
`python-dotenv`.

## Fyra fällor som kostade oss tid

**Inlämningar går inte att slå upp via REST.** `/api/v1/submissions/:id` ger
404, och kursvägen kräver kurs- och uppgifts-id som portfolion inte känner
till. GraphQL `legacyNode(_id:, type: Submission)` löser det i ett anrop.
Kontrollera att inlämningarna tillhör portfolions egen ägare — vid
grupp-inlämningar vore andras material ett integritetsproblem. Hos oss gjorde
samtliga 159 det.

**Inbäddade filer syns inte i metadatan.** `attachment_ids` täcker bara
`attachment`-sektioner. En bild som studenten lagt *inuti* en text blir en
vanlig `<img src="/users/5/files/484931/preview">`, och det id:t står ingen
annanstans. Hos oss: 25 filer i 10 portfolios, 15 MB, som annars försvunnit
tyst.

**Verifier-token i nedladdnings-URL:er går ut.** Ladda ned filerna i samma veva
som exporten, eller lita på token-fallbacken i nedladdaren.

**Raderade portfolios går inte att komma åt.** `/eportfolios/:id` och
`/eportfolios/:id/pages` ger 403 även med `?include[]=deleted`, och GraphQL har
ingen `Eportfolio`-nodtyp. Kvar finns bara
`PUT /users/:user_id/eportfolios/restore`, som återställer *alla* raderade
portfolios för en användare — en skrivoperation i produktion som väcker
material någon aktivt raderat. Det är ett beslut om lagringsändamål, inte ett
tekniskt val.

## Om HTML-arkivet

Ett sökbart index plus en sida per portfolio, som fungerar direkt från
filsystemet utan server. Indexet söker på portfolionamn, ägarnamn,
användarnamn, portfolio-id och ägar-id samtidigt, och filtrerar på
skapandedatum.

* **Skript stryks.** Innehållet är främmande HTML som öppnas lokalt;
  `<script>`, `on*`-attribut och `javascript:`-URL:er tas bort.
* **Länkar skrivs om.** Rotrelativa Canvas-länkar som pekar på något ni har på
  disk blir lokala sökvägar; övriga görs absoluta mot `--host`.
* **Filerna kopieras inte** — arkivet länkar relativt till filkatalogerna,
  eftersom de är gigabyte. Flytta hela utdatakatalogen som en enhet.

## Personuppgifter

Utdatan är studenters arbeten med namn och e-postadresser, och i vår export
fanns personnummer i fritext. **Den ska inte checkas in** — håll den utanför
git, som i exemplen ovan, och behandla den som personuppgifter hela vägen.
`.gitignore` stoppar `.env` och de vanliga utdatanamnen, men den avgörande
vanan är att lägga utdatan utanför arbetskatalogen.

Verktygen rör aldrig något i Canvas: de läser, och skriver bara till disk.

## Licens

MIT, se `LICENSE`.
