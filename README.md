# markdown-archief

Zet een gemengde bak oude bestanden om naar Markdown en maak ze lokaal doorzoekbaar. Alles draait op je eigen computer; er gaat geen bestand naar een clouddienst.

- **`conversie/convert.py`** zet Word (ook oud .doc), WordPerfect, PDF (ook gescand), PowerPoint, Excel, HTML, e-mail, CSV, JSON, afbeeldingen en kapotte bestanden om naar Markdown. Het type wordt bepaald op inhoud, niet op extensie. Elk resultaat krijgt frontmatter met bron, hash en kwaliteitsscore. Optioneel kan een lokaal model via [Ollama](https://ollama.com) opmaak herstellen, afbeeldingen beschrijven of scans overschrijven, met een strenge controle dat er geen inhoud bij komt of verdwijnt. Audio en video kunnen worden getranscribeerd met Whisper.
- **`conversie/zoek.py`** en **`conversie/zoek.html`** zetten de Markdown in een zoekindex (SQLite FTS5) en bieden een zoekpagina in de browser op `http://127.0.0.1:8765`.

## Snel starten

Nodig: [uv](https://docs.astral.sh/uv/), Pandoc, LibreOffice en Tesseract, en voor gescande PDF's ook OCRmyPDF. De installatie voor Mac en Windows staat in de handleiding.

```bash
cd conversie

# wat zit er in de bak?
uv run convert.py ../bron ../markdown --dry-run

# omzetten
uv run convert.py ../bron ../markdown

# zoekpagina starten
uv run zoek.py start ../markdown --bron ../bron
```

`uv` installeert de benodigde Python-pakketten zelf bij de eerste run.

## Handleiding

Alle uitleg staat in [`20261102 - plan conversie naar markdown.md`](./20261102%20-%20plan%20conversie%20naar%20markdown.md):

- installatie op Mac en Windows;
- welke route elk bestandstype volgt;
- wat de kwaliteitsstatussen betekenen;
- hoe de modellaag werkt en wanneer je hem wel of niet gebruikt;
- zoeken en herindexeren;
- bekende beperkingen.

## Status

Werkend prototype, getest op een testbak van 30 gemengde bestanden. De modellaag is getest met een nagebootste Ollama, nog niet met echte modellen.
