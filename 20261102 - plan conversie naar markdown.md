---
titel: Plan conversie gemengde bak naar Markdown
datum: 2026-11-02
bijgewerkt: 2026-11-02
project: Pilot Informatie Autonomie
status: concept
---

# Plan: gemengde bak naar Markdown

## Uitgangspunt

De keuze "lokaal script óf lokaal LLM" is geen echte keuze. Een taalmodel is slecht in het lezen van bestandsformaten en redelijk in het opknappen van tekst die er al uit is gehaald. Een script is precies andersom. De opzet wordt dus:

1. **Script doet het zware werk**: betrouwbaar, snel, herhaalbaar, geen verzonnen tekst. In de testbak was dat 19 van de 30 bestanden met kwaliteit "goed" zonder enig model.
2. **Vision-model** voor afbeeldingen en scans. Hier wint een model echt van klassieke OCR.
3. **Tekstmodel** alleen voor opmaakherstel van documenten met een matig of gerepareerd resultaat, met een strenge controle achteraf.
4. **Whisper** voor audio en video, als je die wilt meenemen.
5. **Mens** voor wat in quarantaine belandt.

Alles draait lokaal. Geen bestand verlaat de machine.

## Harde regels

- Originelen worden nooit aangepast. Werk altijd van bron naar een aparte doelmap.
- Type bepalen op inhoud, niet op extensie. In oude archieven klopt de extensie vaak niet of ontbreekt.
- Elke conversie krijgt frontmatter: bronpad, hash, converter, kwaliteit, datum. Wat een model heeft gemaakt staat er apart bij (`beschrijving` of `transcriptie`, met "niet gecontroleerd").
- **Data gaat nooit naar een taalmodel.** CSV, JSON, XML en spreadsheets worden letterlijk overgenomen.
- Een tekstmodel mag opmaak herstellen, nooit inhoud veranderen. Het script controleert dat per stuk tekst en gooit afwijkingen weg.

## Wat er per type gebeurt

| Type | Categorie | Route | Model? |
|---|---|---|---|
| docx, odt | document | Pandoc, terugval MarkItDown, LibreOffice, reparatie uit zip | tekstmodel optioneel |
| doc (oud Word), rtf | document | LibreOffice naar docx, dan Pandoc | tekstmodel optioneel |
| WordPerfect | document | LibreOffice (libwpd) naar docx, dan Pandoc | tekstmodel optioneel |
| ppt (oud PowerPoint) | document | LibreOffice naar pptx, dan MarkItDown | tekstmodel optioneel |
| pptx, msg, eml, html, epub | document | MarkItDown of Pandoc | tekstmodel optioneel |
| PDF met tekstlaag | document | PyMuPDF4LLM | tekstmodel optioneel |
| PDF gescand | document | OCRmyPDF + PyMuPDF4LLM, bij mislukking vision-model | vision optioneel |
| csv | data | kleine tabel wordt Markdown-tabel, groot bestand een `csv`-codeblok | nooit |
| json, xml | data | netjes ingesprongen in een codeblok | nooit |
| xls, xlsx | data | MarkItDown (Markdown-tabellen) | nooit |
| afbeeldingen | beeld | Tesseract, plus vision-model voor tekst en beschrijving | vision optioneel |
| audio, video | media | standaard overgeslagen, met `--whisper` getranscribeerd | Whisper optioneel |
| corrupt / onbekend | document | reparatiepoging, LibreOffice, daarna ruwe tekst | tekstmodel optioneel |

Op alle documenttekst draait verder een kleine schoonmaak. Die zet regeleinden gelijk en vervangt onzichtbare tekens die woorden aan elkaar plakken (vertical tab uit PowerPoint, form feed uit PDF) door een regeleinde.

## Kwaliteit en statussen

Het script geeft elk resultaat een status. Die staat in de frontmatter en in `manifest.csv`.

| Status | Betekenis | Quarantaine? |
|---|---|---|
| `goed` | ruim 60 procent echte woorden, weinig rommel | nee |
| `matig` | leesbaar maar rommelig | nee, wel kandidaat voor `--llm` |
| `slecht` / `leeg` / `mislukt` | nauwelijks of geen bruikbare tekst | **ja** |
| `data` | tabel of codeblok, geen score | nee |
| `geen-tekst` | afbeelding zonder herkenbare tekst (foto) | nee |
| `beschreven` | afbeelding met tekst en beschrijving van het vision-model | nee |
| `overgeslagen` | audio of video zonder `--whisper` | nee |
| `duplicaat` | zelfde inhoud als een ander bestand (zelfde hash) | nee |

"Echte woorden" geldt voor elk schrift: Cyrillisch, Grieks en Arabisch tellen ook mee.

De score is grof. Een document dat netjes leest maar waar een halve pagina mist, scoort gewoon `goed`. Steekproeven blijven nodig.

## De modellaag

### Tekstmodel: twee standen

- **`--llm`** stuurt alleen documenten met status `matig` naar het model, plus resultaten die uit een reparatie of ruwe tekst komen. Verbeteringen komen direct in de uitvoer.
- **`--llm-vergelijk`** stuurt *alle* documenten langs het model, maar schrijft het resultaat apart naar `<doel>/_llm-vergelijking/`. Je gewone uitvoer blijft zoals het script hem maakte. Zo kun je per bestand zelf beoordelen of het model iets toevoegt. **Begin hiermee.**

Hoe het werkt:

- Tekst gaat in stukken van maximaal 6000 tekens, geknipt op regels, zodat niets buiten het contextvenster valt. Het venster staat op 16.000 tokens.
- De denkstap van modellen als qwen3 staat uit (`think: false`). Overgebleven `<think>`-blokken, `/think`-regels en codeblok-omhulsels worden weggehaald.
- **Elk stuk wordt gecontroleerd.** Het wordt afgewezen als:
  - de opdracht in de uitvoer staat (het model plakte de prompt terug);
  - een getal is veranderd;
  - er meer dan 2 letters zijn bijgekomen of verdwenen. Spaties, regeleinden en tekens als `#`, `-`, `*` en `|` tellen niet mee, want die zijn juist de opmaak.
- In de kolom `llm` van het manifest zie je per bestand hoeveel stukken `aangepast`, `ongewijzigd` of `afgewezen` zijn, en waarom. Het label `+llm` bij de converter komt er alleen als er echt iets is veranderd.

Wat de controle niet vangt: een model dat woorden van plek verwisselt of een alinea verkeerd als kop markeert. Daarvoor blijft de vergelijkingsmap nodig.

### Vision-model

Met `--vision <model>` gaat elke afbeelding naar een vision-model. Dat schrijft de leesbare tekst over en geeft een korte beschrijving. De uitvoer van Tesseract komt eronder te staan, zodat je kunt vergelijken. Gescande PDF's waar tekst-extractie en OCR niets opleverden, worden per pagina overgeschreven (maximaal 30 pagina's).

Let op: dit is door een model gemaakt, dus niet letterlijk betrouwbaar. In de frontmatter staat daarom "niet gecontroleerd".

### Modelkeuze

Dit verandert snel, dus check wat er op dat moment actueel is.

- **Tekst**: een model van 7 tot 14B dat goed Nederlands kan, bijvoorbeeld `qwen3:8b`. Op een Mac met 16 GB is 8B het praktische maximum.
- **Beeld**: een vision-model in Ollama, bijvoorbeeld `qwen2.5vl:7b`. Voor zware scans met tabellen is **Docling** (IBM, open source) het bekijken waard.
- **Audio**: Whisper `small` is een goede balans. `medium` of `large-v3` zijn nauwkeuriger maar veel trager op CPU.

## Installatie

Op beide systemen installeert `uv` de Python-pakketten zelf bij de eerste run; die staan bovenin de scripts. Whisper installeer je alleen als je het nodig hebt (zie Gebruik). De zoekpagina (`zoek.py`) heeft alleen `uv` nodig.

### Mac

```bash
brew install pandoc tesseract tesseract-lang ocrmypdf uv
brew install --cask libreoffice
# voor de modellaag
brew install ollama
ollama pull qwen3:8b          # tekst
ollama pull qwen2.5vl:7b      # beeld
```

### Windows (10 of 11, 64-bit)

Open **PowerShell** en installeer met `winget`, dat standaard op Windows zit:

```powershell
winget install -e --id astral-sh.uv
winget install -e --id JohnMacFarlane.Pandoc
winget install -e --id TheDocumentFoundation.LibreOffice
winget install -e --id tesseract-ocr.tesseract
# voor de modellaag
winget install -e --id Ollama.Ollama
```

Daarna nog drie losse stappen:

1. **Ghostscript** (nodig voor OCRmyPDF) zit niet in winget. Download de 64-bit Windows-versie van [ghostscript.com/releases](https://ghostscript.com/releases/gsdnld.html) en installeer die.
2. **Nederlands voor Tesseract.** Tesseract installeert op Windows standaard alleen Engels. Haal het Nederlandse taalpakket op in een PowerShell die je opent met **Als administrator uitvoeren**:

   ```powershell
   Invoke-WebRequest https://github.com/tesseract-ocr/tessdata/raw/main/nld.traineddata `
     -OutFile "C:\Program Files\Tesseract-OCR\tessdata\nld.traineddata"
   ```

   Zonder dit pakket werkt OCR nog steeds, maar dan alleen in het Engels. Het script meldt dat bij de start.
3. **OCRmyPDF** installeer je via uv. Open daarvoor eerst een **nieuw** PowerShell-venster, zodat de programma's van hierboven gevonden worden:

   ```powershell
   uv tool install ocrmypdf
   ocrmypdf --version
   ```

   Wordt `ocrmypdf` niet gevonden? Draai `uv tool update-shell` en open opnieuw een nieuw venster.

De modellen haal je op zoals op de Mac:

```powershell
ollama pull qwen3:8b
ollama pull qwen2.5vl:7b
```

Goed om te weten:

- **Zoekpad.** Tesseract en LibreOffice komen op Windows meestal niet in het zoekpad. Het script zoekt ze daarom ook op hun standaardplek in `C:\Program Files`. Heb je ze ergens anders geïnstalleerd, voeg die map dan toe aan de omgevingsvariabele `Path`.
- **Tekencodering.** Het script leest de uitvoer van Pandoc en Tesseract altijd als UTF-8. Zonder die instelling verminkt Windows letters als é, ë en €.
- **Modellen.** Ollama draait op Windows het snelst met een NVIDIA-videokaart. Zonder videokaart werkt het ook, maar de modellaag is dan flink trager.

## Gebruik

```bash
cd markdown-archief/conversie      # pas aan naar waar jouw map staat

# 1. inventariseren: wat zit er echt in de bak?
uv run convert.py ../bron ../markdown --dry-run

# 2. converteren zonder model
uv run convert.py ../bron ../markdown

# 3. afbeeldingen en scans laten lezen door een vision-model
uv run convert.py ../bron ../markdown --vision qwen2.5vl:7b

# 4a. testen wat het tekstmodel toevoegt (uitvoer apart in _llm-vergelijking)
uv run convert.py ../bron ../markdown --llm-vergelijk --model qwen3:8b

# 4b. alleen matige documenten laten opknappen, direct in de uitvoer
uv run convert.py ../bron ../markdown --llm --model qwen3:8b

# 5. audio en video transcriberen
uv run --with faster-whisper convert.py ../bron ../markdown --whisper small
```

**Op Windows** werken dezelfde opdrachten in PowerShell. Alleen het pad bij `cd` is anders, bijvoorbeeld:

```powershell
cd "C:\pad\naar\markdown-archief\conversie"
uv run convert.py ..\bron ..\markdown --dry-run
```

Schuine strepen (`../bron`) werken op Windows ook. Wil je in de oude Windows PowerShell 5 twee opdrachten achter elkaar laten lopen? Gebruik dan `;` in plaats van `&&`. Bij `;` loopt de tweede opdracht ook als de eerste is mislukt.

Opties zijn te combineren, bijvoorbeeld `--vision qwen2.5vl:7b --llm`. Alleen `--llm` en `--llm-vergelijk` gaan niet samen. Draait Ollama niet, of ontbreekt het model, dan stopt het script direct met een melding en de opdracht om het model op te halen.

Uitvoer in de doelmap:

- de `.md`-bestanden in dezelfde mappenstructuur als de bron;
- `manifest.csv`: per bestand het type, de converter, de kwaliteit, wat het model deed en eventuele fouten;
- `_quarantaine.txt`: de bestanden met `slecht`, `leeg` of `mislukt`;
- bij `--llm-vergelijk` de map `_llm-vergelijking/`;
- `_fouten.log`, alleen als er onverwachte fouten waren: per bestand de tijd en de volledige foutmelding.

**Het script stopt nooit door één bestand.** Gaat er bij een bestand iets mis wat niet bij een gewone conversiefout hoort, dan:

- meldt het script dat in de terminal (`FOUT, overgeslagen: ...`);
- schrijft het de details naar `_fouten.log`;
- zet het dat bestand als `mislukt` in het manifest en de quarantaine;
- gaat het door met het volgende bestand.

Aan het eind zie je hoeveel onverwachte fouten er waren. Ook bij Ctrl+C schrijft het script het manifest voor wat al klaar is.

Staat `manifest.csv` nog open in Excel, dan kan Windows hem niet overschrijven. Het script slaat hem dan op als `manifest-<datum-tijd>.csv`.

Echt vastlopen kan nog wel, als een onderliggend programma zelf hangt of crasht, zoals LibreOffice of PyMuPDF bij een heel kapot bestand. Voor de externe programma's geldt een tijdslimiet. Die is 3 minuten voor Pandoc, 5 minuten voor Tesseract en LibreOffice, en 15 minuten voor OCRmyPDF. PyMuPDF heeft geen tijdslimiet.

Opnieuw draaien overschrijft de uitvoer, niet de bron. **Let op:** `.md`-bestanden uit een eerdere run die nu niet meer worden gemaakt, blijven staan. Een overgeslagen audiobestand van een vorige run kan dus nog een oude `.md` hebben. Gebruik voor een schone vergelijking een nieuwe doelmap.

## Zoeken in de uitvoer

De Markdown-bestanden gaan in een zoekindex: **SQLite met FTS5**, de full-text-zoekmodule van SQLite.

Waarom deze keuze:

- Open source (public domain) en zit standaard in Python. Er is niets te installeren.
- De hele index is één bestand (`zoekindex.db`). Geen server die moet blijven draaien, makkelijk mee te nemen en te back-uppen.
- Snel genoeg voor een groot archief. In een test met 20.000 documenten (ruim 150 MB tekst) duurde indexeren 11 seconden en elke zoekopdracht minder dan 50 ms.
- Alternatieven als Meilisearch en Typesense zijn ook open source en kunnen meer (typfouten opvangen, synoniemen), maar draaien als aparte server. Voor een lokaal archief is dat meer beheer dan het oplevert.

Er zijn twee bestanden in de map `conversie`: `zoek.py` (index en lokale server) en `zoek.html` (de zoekpagina).

```bash
cd markdown-archief/conversie      # pas aan naar waar jouw map staat

# zoekpagina starten (ververst eerst de index, opent dan de browser)
uv run zoek.py start ../markdown --bron ../bron

# alleen de index bijwerken
uv run zoek.py index ../markdown
```

De pagina draait op `http://127.0.0.1:8765` en is alleen vanaf je eigen computer bereikbaar. Stoppen doe je met Ctrl+C in de terminal. Met `--bron` kun je vanuit een zoekresultaat het originele bestand openen; zonder `--bron` werkt alles behalve die knop.

Hoe het werkt:

- **Bijwerken.** Bij elke start worden alleen nieuwe en gewijzigde `.md`-bestanden opnieuw gelezen; verwijderde verdwijnen uit de index. Verversen zonder wijzigingen kost minder dan een seconde, ook bij 20.000 documenten.
- **Wat er wel en niet in komt.** Mappen die met een punt of liggend streepje beginnen (`.obsidian`, `_llm-vergelijking`) tellen niet mee.
- **De database.** `zoekindex.db` komt naast de markdownmap te staan, of op een eigen plek met `--db`. Reken op ongeveer twee keer de omvang van de Markdown-tekst. Weggooien mag altijd; `index` bouwt hem opnieuw op.
- **Titel.** De eerste kop in het document, anders de bestandsnaam.
- **Filters.** Type en kwaliteit komen uit de frontmatter van `convert.py`.

Zoeken:

| Invoer | Betekenis |
|---|---|
| `klimaat beleid` | beide woorden komen voor (in willekeurige volgorde) |
| `"vaste zin"` | precies deze woorden achter elkaar |
| `archi*` | woorden die zo beginnen |
| `-concept` | dit woord mag niet voorkomen |
| `fema OF nasa` | een van beide (`OR` werkt ook) |

Hoofdletters en accenten maken niet uit: `cafe` vindt ook café. Het laatste woord dat je typt wordt automatisch aangevuld, zodat je tijdens het typen al resultaten ziet. Resultaten staan op relevantie, waarbij een treffer in de titel zwaarder telt.

Op de pagina:

- **Toetsen.** `/` springt naar de zoekbalk, Esc sluit het document.
- **Documentweergave.** Een klik op een resultaat opent het document rechts, met de treffers gemarkeerd. Met de pijltjes spring je van treffer naar treffer.
- **Gedeelde links.** De zoekopdracht staat in de adresbalk. Een link als `http://127.0.0.1:8765/?q=protocol` kun je bewaren.
- **Veilige weergave.** HTML uit documenten wordt gefilterd: tabellen en opmaak blijven staan, scripts en dergelijke worden weggehaald.

## Werkvolgorde

1. Draai `--dry-run` en kijk in `manifest.csv` welke typen en aantallen er zijn.
2. Draai zonder model. Lees `_quarantaine.txt` en neem een steekproef van 10 tot 20 `goed`-bestanden per type. Let op tabellen, voetnoten en speciale tekens (ë, é, €).
3. Zitten er afbeeldingen of scans in: draai met `--vision` en controleer een paar beschrijvingen.
4. Draai `--llm-vergelijk` op de testbak en vergelijk een paar bestanden naast elkaar, bijvoorbeeld in Obsidian of met `diff`. Zie je echte winst, gebruik dan `--llm` voor de hele bak. Zie je die niet, laat de tekstlaag weg.
5. Start de zoekpagina met `uv run zoek.py start ../markdown --bron ../bron` en zoek op termen waarvan je weet in welke documenten ze staan. Zo test je meteen de conversie: wat je niet vindt, is er waarschijnlijk niet goed uitgekomen.

## Bekende beperkingen

- **WordPerfect**: LibreOffice leest WP 4.2 tot en met X via libwpd. WP 5.x en later herkent het script aan de header. WP 4.2-bestanden hebben geen header en worden alleen aan de extensie herkend. Zonder extensie komen ze als `onbekend` binnen en gaan ze alsnog eerst langs LibreOffice. Bij eigen tekensets of macrocodes kan er rommel uitkomen; probeer dan `wpd2text` uit libwpd.
- **LibreOffice moet dicht zijn** tijdens een conversie. Staat LibreOffice open, dan weigert de headless omzetting soms zonder duidelijke fout, en komen doc-, ppt- en WordPerfect-bestanden in de quarantaine. Dit geldt op Mac en Windows.
- **Grote CSV-bestanden** (meer dan 500 rijen of 20 kolommen) worden geen tabel maar een codeblok. Dat is verliesvrij, maar niet mooi.
- **Tabellen in PDF**: PyMuPDF4LLM doet het redelijk, Docling duidelijk beter.
- **Wachtwoordbeveiligde bestanden** worden niet geopend en komen in quarantaine.
- **Snelheid**: het script doet ruwweg seconden per bestand en OCR een halve minuut per document. Het tekstmodel kost minuten per document; met `--llm-vergelijk` op duizenden bestanden ben je dagen bezig. Gebruik die stand dus alleen op een testbak.
- **Zoeken op betekenis** kan niet. De zoekindex vindt woorden, geen begrippen: `auto` vindt geen `voertuig`, en een typfout vindt niets. Daarvoor is semantisch zoeken nodig (embeddings, bijvoorbeeld via Ollama en de SQLite-uitbreiding sqlite-vec). Dat is een logische volgende stap, maar wel een flinke.
- **Zeer grote documenten** (meer dan 300.000 tekens, zoals de grote CSV) worden in de zoekpagina ingekort weergegeven. Doorzoekbaar zijn ze wel volledig.
- **Getest**: de controle van het tekstmodel en de vision-route zijn getest met een nagebootste Ollama, niet met echte modellen. Whisper is getest tot aan het downloaden van het model. De eerste echte run op de Mac is dus ook een test. De Windows-installatie volgt de officiële documentatie van de onderdelen, maar is niet op een Windows-computer uitgeprobeerd.
