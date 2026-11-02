# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "markitdown[docx,pptx,xlsx,xls,outlook,pdf]",
#   "pymupdf>=2",          # bevat sinds 2.0 ook pymupdf4llm
#   "charset-normalizer",
# ]
# ///
"""
Bak-naar-Markdown converter, versie 2.

Gebruik (zie de handleiding voor uitleg):
    uv run convert.py <bron> <doel> [--dry-run]
                      [--llm | --llm-vergelijk] [--model qwen3:8b]
                      [--vision qwen2.5vl:7b]
                      [--whisper small]   (start dan met: uv run --with faster-whisper convert.py ...)

Werkwijze per bestand:
  1. Type bepalen op inhoud (magic bytes), niet op extensie.
  2. Converters proberen in volgorde tot er een bruikbaar resultaat is.
  3. Kwaliteit meten, frontmatter toevoegen, wegschrijven.
  4. Optioneel: lokaal model voor opmaakherstel (tekst) of beschrijving/transcriptie (beeld).
  5. Alles loggen in manifest.csv. Mislukte bestanden komen in _quarantaine.txt.
Originelen worden nooit aangepast.
"""
from __future__ import annotations

import argparse
import base64
import csv
import difflib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
OLLAMA = "http://localhost:11434"

# Categorieën bepalen wat er met een bestand mag gebeuren.
DOCUMENT = {"docx", "odf", "rtf", "doc", "wordperfect", "pdf", "html", "epub",
            "pptx", "ppt", "msg", "eml", "tekst", "zip-corrupt", "onbekend"}
DATA = {"csv", "json", "xml", "xlsx", "xls"}      # nooit naar een taalmodel
MEDIA = {"audio", "video"}                         # overslaan, of Whisper
BEELD = {"afbeelding"}

# Statussen die in quarantaine komen. "data", "geen-tekst", "overgeslagen"
# en "duplicaat" zijn geen fouten.
QUARANTAINE = {"mislukt", "slecht", "leeg"}


class Cfg:  # instellingen uit de commandoregel, globaal bereikbaar voor converters
    vision: str | None = None
    whisper: str | None = None


# ================================================================ detectie

def detect(path: Path) -> str:
    """Bepaal het echte type op basis van de eerste bytes (en waar nodig de extensie)."""
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return "onleesbaar"
    if not head:
        return "leeg"
    ext = path.suffix.lower()

    # Documenten
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\xffWPC") or ext in {".wpd", ".wp", ".wp4", ".wp5", ".wp6", ".wpt"}:
        return "wordperfect"            # WP 5.x+ heeft header; WP 4.2 alleen via extensie
    if head.startswith(b"{\\rtf") or ext == ".rtf":
        return "rtf"
    if head.startswith(b"\xd0\xcf\x11\xe0"):  # OLE2: oud Office of Outlook
        return {".xls": "xls", ".ppt": "ppt", ".pps": "ppt", ".msg": "msg"}.get(ext, "doc")
    if head.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(path).namelist()
        except zipfile.BadZipFile:
            return "zip-corrupt"
        if any(n.startswith("word/") for n in names):
            return "docx"
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        if any(n.startswith("ppt/") for n in names):
            return "pptx"
        if "content.xml" in names:
            return "odf"
        if "META-INF/container.xml" in names:
            return "epub"
        return "zip"

    # Beeld en geluid
    if head[:4] in (b"\x89PNG", b"GIF8") or head[:3] == b"\xff\xd8\xff" \
            or head[:4] in (b"II*\x00", b"MM\x00*") or head[:2] == b"BM":
        return "afbeelding"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "afbeelding"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"mif1", b"avif"):
            return "afbeelding"
        return "audio" if brand in (b"M4A ", b"M4B ") else "video"
    if head.startswith((b"fLaC", b"ID3", b"OggS", b"FORM")) or head[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") \
            or (head[:4] == b"RIFF" and head[8:12] == b"WAVE"):
        return "audio"
    if head.startswith(b"\x1a\x45\xdf\xa3") or (head[:4] == b"RIFF" and head[8:12] == b"AVI "):
        return "video"

    # Tekstachtig
    low = head.lower()
    if b"<html" in low or b"<!doctype html" in low:
        return "html"
    if ext == ".eml" or low.startswith(b"from:") or (b"\nsubject:" in low and b"\nfrom:" in low):
        return "eml"
    ctrl = sum(1 for b in head if b < 9 or 13 < b < 32)
    if ctrl / len(head) >= 0.05:
        return "onbekend"
    if ext in {".csv", ".tsv"}:
        return "csv"
    stripped = head.lstrip()
    if ext == ".json" or stripped[:1] in (b"{", b"["):
        try:
            json.loads(read_text(path))
            return "json"
        except Exception:  # noqa: BLE001
            pass
    if stripped.startswith(b"<?xml") or ext == ".xml":
        return "xml"
    return "tekst"


# ================================================================ hulpfuncties

def run(cmd: list[str], timeout: int = 180) -> str:
    # Altijd UTF-8: Windows gebruikt anders cp1252 en verminkt dan é, ë, € enzovoort.
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        # Fontconfig-waarschuwingen van LibreOffice zijn ruis, de echte fout staat erna.
        err = "\n".join(l for l in r.stderr.splitlines() if not l.startswith("Fontconfig"))
        raise RuntimeError(err.strip()[:300] or f"exit {r.returncode}")
    return r.stdout


def read_text(path: Path) -> str:
    from charset_normalizer import from_path
    best = from_path(path).best()
    if best is None:
        raise RuntimeError("geen tekencodering gevonden")
    return str(best)


def find_tool(name: str, *fallbacks: str) -> str:
    """Zoek een programma in het zoekpad en anders op de standaardplekken (Mac en Windows)."""
    found = shutil.which(name)
    if found:
        return found
    for f in fallbacks:
        if Path(f).is_file():
            return f
    return name   # laat de fout dan zelf duidelijk melden dat het programma ontbreekt


def soffice() -> str:
    return find_tool("soffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice",
                     r"C:\Program Files\LibreOffice\program\soffice.exe",
                     r"C:\Program Files (x86)\LibreOffice\program\soffice.exe")


def tesseract() -> str:
    return find_tool("tesseract", r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                     r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")


_LANGS: str | None = None


def ocr_langs() -> str:
    """Nederlands + Engels als het Nederlandse taalpakket er is, anders alleen Engels.
    Op Windows installeert Tesseract standaard alleen Engels."""
    global _LANGS
    if _LANGS is None:
        try:
            have = set(run([tesseract(), "--list-langs"], timeout=30).split())
        except Exception:  # noqa: BLE001
            have = set()
        _LANGS = "+".join(l for l in ("nld", "eng") if l in have) or "eng"
        if "nld" not in have:
            print("Let op: Nederlands taalpakket voor Tesseract ontbreekt, OCR gebruikt alleen Engels.", file=sys.stderr)
    return _LANGS


def image_png(path: Path, max_side: int = 1600) -> list[bytes]:
    """Afbeelding of PDF naar PNG-bytes per pagina, verkleind voor het model."""
    import pymupdf
    doc = pymupdf.open(path)
    out = []
    for page in list(doc)[:30]:
        zoom = min(2.0, max_side / max(page.rect.width, page.rect.height))
        out.append(page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png"))
    return out


# ================================================================ converters

def c_pandoc(path: Path) -> str:
    return run(["pandoc", str(path), "-t", "gfm", "--wrap=none"])


def c_markitdown(path: Path) -> str:
    from markitdown import MarkItDown
    return MarkItDown().convert(str(path)).text_content


def _lo(path: Path, target: str) -> Path:
    tmp = Path(tempfile.mkdtemp())
    run([soffice(), "--headless", "--convert-to", target, "--outdir", str(tmp), str(path)], timeout=300)
    out = next(tmp.glob(f"*.{target}"), None)
    if not out:
        raise RuntimeError(f"LibreOffice maakte geen {target}")
    return out


def c_lo_docx(path: Path) -> str:
    """Oude tekstformaten (doc, WordPerfect, rtf) via LibreOffice naar docx, dan Pandoc."""
    return c_pandoc(_lo(path, "docx"))


def c_lo_pptx(path: Path) -> str:
    """Oude presentaties: LibreOffice kan ppt niet naar docx, wel naar pptx."""
    return c_markitdown(_lo(path, "pptx"))


def c_lo_xlsx(path: Path) -> str:
    return c_markitdown(_lo(path, "xlsx"))


_PDF_STIL = False


def c_pdf_text(path: Path) -> str:
    """PDF naar Markdown met PyMuPDF4LLM. Pagina's zonder tekstlaag krijgen daarbij al OCR
    via Tesseract, in dezelfde talen als de rest van het script (standaard zou dat alleen
    Engels zijn). De meldingen 'Document parser messages' / 'RapidOCR is missing' zijn
    informatief en verschijnen bij elke PDF; die zetten we uit."""
    global _PDF_STIL
    import pymupdf
    import pymupdf4llm
    if not _PDF_STIL:
        import logging
        pymupdf.set_messages(pylogging=True, pylogging_level=logging.DEBUG)
        _PDF_STIL = True
    return pymupdf4llm.to_markdown(str(path), ocr_language=ocr_langs())


def c_pdf_ocr(path: Path) -> str:
    """Gescande PDF: eerst een tekstlaag toevoegen met OCRmyPDF (Nederlands + Engels)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "ocr.pdf"
        run(["ocrmypdf", "-l", ocr_langs(), "--skip-text", "--quiet", str(path), str(out)], timeout=900)
        return c_pdf_text(out)


def c_image_ocr(path: Path) -> str:
    return run([tesseract(), str(path), "-", "-l", ocr_langs()], timeout=300)


def c_text(path: Path) -> str:
    return read_text(path)


def c_csv(path: Path) -> str:
    """Kleine CSV wordt een Markdown-tabel, grote blijft letterlijk staan in een codeblok."""
    text = read_text(path)
    try:
        dialect = csv.Sniffer().sniff(text[:5000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    width = max((len(r) for r in rows), default=0)
    if 0 < len(rows) <= 500 and width <= 20:
        cell = lambda v: v.replace("|", "\\|").replace("\n", " ").strip()  # noqa: E731
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(cell(v) for v in rows[0]) + " |",
                 "|" + "---|" * width]
        lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows[1:]]
        return "\n".join(lines) + "\n"
    return f"```csv\n{text.rstrip()}\n```\n"


def c_json(path: Path) -> str:
    text = read_text(path)
    try:
        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except ValueError:
        pass
    return f"```json\n{text.rstrip()}\n```\n"


def c_xml(path: Path) -> str:
    return f"```xml\n{read_text(path).rstrip()}\n```\n"


def c_docx_repair(path: Path) -> str:
    """Kapotte docx/zip: zoek de lokale header van word/document.xml en pak die los uit,
    ook als de inhoudsopgave van het zipbestand ontbreekt (afgebroken download e.d.)."""
    import zlib
    data = path.read_bytes()
    pos = data.find(b"PK\x03\x04")
    while pos != -1:
        name_len = int.from_bytes(data[pos + 26:pos + 28], "little")
        extra_len = int.from_bytes(data[pos + 28:pos + 30], "little")
        name = data[pos + 30:pos + 30 + name_len]
        if name in (b"word/document.xml", b"content.xml"):
            start = pos + 30 + name_len + extra_len
            d = zlib.decompressobj(-15)
            try:
                xml = d.decompress(data[start:])
            except zlib.error:
                xml = d.flush()
            xml = re.sub(rb"</w:p>|</text:p>|</text:h>", b"\n\n", xml)
            return re.sub(rb"<[^>]+>", b"", xml).decode("utf-8", "replace")
        pos = data.find(b"PK\x03\x04", pos + 4)
    raise RuntimeError("geen document.xml gevonden")


def c_raw_strings(path: Path) -> str:
    """Laatste redmiddel: alle leesbare tekstfragmenten van 4+ tekens."""
    data = path.read_bytes()
    parts = re.findall(rb"[\x20-\x7e\xa0-\xff\t\r\n]{4,}", data)
    return "\n".join(p.decode("latin-1").strip() for p in parts if p.strip())


def c_whisper(path: Path) -> str:
    if not Cfg.whisper:
        raise RuntimeError("overgeslagen (gebruik --whisper om te transcriberen)")
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("faster-whisper ontbreekt: start met 'uv run --with faster-whisper convert.py ...'") from e
    model = WhisperModel(Cfg.whisper, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(path))
    lines = [f"[{int(s.start // 60):02d}:{int(s.start % 60):02d}] {s.text.strip()}" for s in segments]
    return f"*Transcriptie door Whisper ({Cfg.whisper}), taal: {info.language}*\n\n" + "\n".join(lines) + "\n"


CHAINS: dict[str, list] = {
    "docx":        [c_pandoc, c_markitdown, c_lo_docx, c_docx_repair],
    "odf":         [c_pandoc, c_lo_docx],
    "rtf":         [c_lo_docx, c_raw_strings],
    "doc":         [c_lo_docx, c_raw_strings],
    "wordperfect": [c_lo_docx, c_raw_strings],
    "pdf":         [c_pdf_text, c_pdf_ocr, c_markitdown],
    "xlsx":        [c_markitdown],
    "xls":         [c_markitdown, c_lo_xlsx],
    "pptx":        [c_markitdown],
    "ppt":         [c_lo_pptx],
    "msg":         [c_markitdown],
    "eml":         [c_markitdown, c_text],
    "html":        [c_pandoc, c_markitdown],
    "epub":        [c_pandoc],
    "afbeelding":  [c_image_ocr],
    "csv":         [c_csv],
    "json":        [c_json],
    "xml":         [c_xml],
    "tekst":       [c_text],
    "audio":       [c_whisper],
    "video":       [c_whisper],
    "zip-corrupt": [c_docx_repair, c_raw_strings],
    "onbekend":    [c_lo_docx, c_raw_strings],
}


# ================================================================ kwaliteit

WORD = re.compile(r"^[^\W\d_]{2,25}$")
VOWELS = set("aeiouyàáâäèéêëìíîïòóôöùúûü")


def is_word(w: str) -> bool:
    """Een echt woord bestaat uit letters. Latijnse woorden moeten ook een klinker hebben;
    voor ander schrift (Cyrillisch, Grieks, Arabisch ...) is 'alleen letters' genoeg."""
    if not WORD.match(w):
        return False
    if all("LATIN" in unicodedata.name(ch, "") for ch in w):
        return bool(VOWELS & set(w.lower()))
    return True


def quality(text: str) -> tuple[str, float]:
    """Score = aandeel echte woorden minus een straf voor rommeltekens."""
    t = text.strip()
    if len(t) < 40:
        return "leeg", 0.0
    tokens = re.findall(r"\S+", t)
    real = sum(1 for tok in tokens if is_word(tok.strip(".,;:!?()\"'*#-_[]«»“”„")))
    junk = sum(1 for ch in t if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\r\t"))
    score = real / max(len(tokens), 1) - (junk / len(t)) * 5
    if score > 0.60:
        return "goed", round(score, 2)
    if score > 0.35:
        return "matig", round(score, 2)
    return "slecht", round(score, 2)


# ================================================================ lokaal model (Ollama)

def ollama_check(model: str) -> None:
    try:
        tags = json.loads(urllib.request.urlopen(f"{OLLAMA}/api/tags", timeout=5).read())
    except (urllib.error.URLError, OSError):
        sys.exit("Ollama draait niet. Start de Ollama-app of 'ollama serve' en probeer opnieuw.")
    names = {m["name"] for m in tags.get("models", [])}
    if model not in names and f"{model}:latest" not in names:
        sys.exit(f"Model '{model}' niet gevonden in Ollama. Haal het op met: ollama pull {model}")


def ollama_chat(model: str, system: str, user: str, images: list[bytes] | None = None) -> str:
    msg = {"role": "user", "content": user}
    if images:
        msg["images"] = [base64.b64encode(b).decode() for b in images]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, msg],
        "stream": False,
        "think": False,                       # geen denkstap (qwen3 e.d.)
        "options": {"temperature": 0, "num_ctx": 16384},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA}/api/chat", body, {"Content-Type": "application/json"})
    res = json.loads(urllib.request.urlopen(req, timeout=900).read())
    return clean_model_output(res["message"]["content"])


def clean_model_output(s: str) -> str:
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S)
    s = re.sub(r"^\s*/(no_)?think\s*$", "", s, flags=re.M)
    s = s.strip()
    m = re.match(r"^```(?:markdown|md)?\n(.*)\n```$", s, flags=re.S)  # model zet alles in een codeblok
    return m.group(1).strip() if m else s


CLEANUP_SYSTEM = (
    "Je herstelt de Markdown-opmaak van tekst die uit een oud document is gehaald. "
    "Herstel alleen koppen, alinea's, lijsten, tabellen en afgebroken woorden. "
    "Voeg niets toe, laat niets weg, vat niets samen, vertaal niets en verander geen woorden. "
    "Antwoord uitsluitend met de herstelde tekst, zonder uitleg."
)
# Stukjes van de opdracht: als die in de uitvoer staan, heeft het model de prompt teruggeplakt.
PROMPT_LEAKS = ("Voeg niets toe", "Herstel alleen", "Markdown-opmaak", "zonder uitleg")


def chunks_by_lines(text: str, size: int = 6000) -> list[str]:
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:              # extreem lange regel hard knippen
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:size])
            line = line[size:]
        if len(buf) + len(line) > size and buf:
            out.append(buf)
            buf = ""
        buf += line
    if buf:
        out.append(buf)
    return out


def check_chunk(orig: str, new: str) -> str | None:
    """Geeft een reden terug als het resultaat wordt afgewezen, anders None.

    Bij echt opmaakherstel blijven de letters en cijfers gelijk; alleen spaties, regeleinden en
    tekens als # - * | veranderen. Daarom vergelijken we alleen letters en cijfers, en staan we
    hoogstens 2 toegevoegde of weggelaten letters toe. Getallen moeten exact gelijk blijven."""
    if any(p.lower() in new.lower() for p in PROMPT_LEAKS) and not any(p.lower() in orig.lower() for p in PROMPT_LEAKS):
        return "prompt in uitvoer"
    nfkc = lambda s: unicodedata.normalize("NFKC", s)  # noqa: E731  ligaturen als ﬁ worden fi
    if re.findall(r"\d+", nfkc(orig).replace(" ", "")) != re.findall(r"\d+", nfkc(new).replace(" ", "")):
        return "getallen veranderd"
    norm = lambda s: re.sub(r"[\W_]+", "", nfkc(s).lower())  # noqa: E731
    a, b = norm(orig), norm(new)
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    added = sum(j2 - j1 for tag, i1, i2, j1, j2 in ops if tag in ("insert", "replace"))
    removed = sum(i2 - i1 for tag, i1, i2, j1, j2 in ops if tag in ("delete", "replace"))
    limit = 2
    if added > limit:
        return f"tekst toegevoegd ({added} tekens)"
    if removed > limit:
        return f"tekst weggelaten ({removed} tekens)"
    return None


def llm_cleanup(text: str, model: str) -> tuple[str, dict]:
    stats = Counter()
    out = []
    for ch in chunks_by_lines(text):
        try:
            new = ollama_chat(model, CLEANUP_SYSTEM, ch)
        except Exception as e:  # noqa: BLE001
            stats["fout"] += 1
            stats["laatste fout"] = str(e)[:80]  # type: ignore[assignment]
            out.append(ch)
            continue
        if " ".join(new.split()) == " ".join(ch.split()):
            stats["ongewijzigd"] += 1
            out.append(ch)
            continue
        reason = check_chunk(ch, new)
        if reason:
            stats["afgewezen"] += 1
            stats["reden"] = reason  # type: ignore[assignment]
            out.append(ch)
        else:
            stats["aangepast"] += 1
            out.append(new.rstrip("\n") + "\n")
    return "".join(out), dict(stats)


VISION_SYSTEM = (
    "Je bekijkt een afbeelding uit een archief. Antwoord in het Nederlands, in Markdown, precies zo:\n"
    "## Tekst in de afbeelding\n<alle leesbare tekst letterlijk overgenomen, of 'Geen'>\n\n"
    "## Beschrijving\n<maximaal drie zinnen over wat er te zien is>"
)
VISION_PAGE = (
    "Dit is een gescande pagina. Schrijf alle tekst letterlijk over als Markdown, met koppen, "
    "lijsten en tabellen zoals op de pagina. Voeg niets toe en geef geen uitleg."
)


def vision_image(path: Path, model: str) -> str:
    return ollama_chat(model, VISION_SYSTEM, "Beschrijf deze afbeelding.", image_png(path)[:1])


def vision_pdf(path: Path, model: str) -> str:
    pages = image_png(path)
    parts = [f"<!-- pagina {i} -->\n" + ollama_chat(model, VISION_PAGE, "Schrijf deze pagina over.", [png])
             for i, png in enumerate(pages, 1)]
    return "\n\n".join(parts) + "\n"


# ================================================================ hoofdlus

def frontmatter(fields: dict) -> str:
    lines = ["---"]
    for k, v in fields.items():
        if v not in (None, ""):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False) if isinstance(v, str) else v}")
    return "\n".join(lines) + "\n---\n\n"


def tidy(text: str) -> str:
    """Kleine, veilige schoonmaak die voor elk resultaat geldt: regeleinden gelijktrekken en
    onzichtbare tekens die woorden aan elkaar plakken (vertical tab uit PowerPoint/Word,
    form feed uit PDF, nul-bytes) vervangen door een regeleinde of weghalen."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\x0b", "\n").replace("\x0c", "\n").replace("\x00", "")
    return re.sub(r"\n{4,}", "\n\n\n", text)


def convert_one(src: Path, kind: str) -> tuple[str | None, str, str, float | str, list[str]]:
    """Probeer de keten. Geeft (tekst, converter, status, score, fouten)."""
    errors: list[str] = []
    best = None
    for conv in CHAINS.get(kind, [c_raw_strings]):
        try:
            text = conv(src)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{conv.__name__[2:]}: {e}")
            continue
        if kind not in DATA:
            text = tidy(text)
        if kind in DATA:
            return text, conv.__name__[2:], "data", "", errors
        status, score = quality(text)
        if best is None or score > best[3]:
            best = (text, conv.__name__[2:], status, score)
        if status == "goed":
            break
    if best is None:
        if kind in MEDIA and not Cfg.whisper:
            return None, "", "overgeslagen", "", []
        return None, "", "mislukt", "", errors
    text, name, status, score = best
    if kind in BEELD and status == "leeg":
        status = "geen-tekst"                 # foto zonder tekst is geen fout
    return text, name, status, score, errors


def main() -> None:
    ap = argparse.ArgumentParser(description="Zet een gemengde bak bestanden om naar Markdown.")
    ap.add_argument("bron", type=Path)
    ap.add_argument("doel", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="alleen inventariseren")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--llm", action="store_true",
                   help="matige en gerepareerde documenten laten opknappen door een lokaal model")
    g.add_argument("--llm-vergelijk", action="store_true",
                   help="alle documenten langs het model, uitvoer apart in <doel>/_llm-vergelijking")
    ap.add_argument("--model", default="qwen3:8b", help="tekstmodel in Ollama (standaard qwen3:8b)")
    ap.add_argument("--vision", metavar="MODEL", help="vision-model in Ollama voor afbeeldingen en scans")
    ap.add_argument("--whisper", metavar="GROOTTE", nargs="?", const="small",
                    help="audio/video transcriberen (tiny, base, small, medium, large-v3)")
    a = ap.parse_args()
    Cfg.vision, Cfg.whisper = a.vision, a.whisper

    if not a.bron.is_dir():
        sys.exit(f"Bronmap niet gevonden: {a.bron.resolve()}")
    files = sorted(p for p in a.bron.rglob("*") if p.is_file() and p.name not in SKIP_NAMES)
    if not files:
        sys.exit(f"Geen bestanden gevonden in: {a.bron.resolve()}")
    if not a.dry_run:
        if a.llm or a.llm_vergelijk:
            ollama_check(a.model)
        if a.vision:
            ollama_check(a.vision)
    a.doel.mkdir(parents=True, exist_ok=True)
    vergelijk_dir = a.doel / "_llm-vergelijking"

    seen: dict[str, Path] = {}
    rows = []
    for i, src in enumerate(files, 1):
        sha = hashlib.sha256(src.read_bytes()).hexdigest()
        kind = detect(src)
        rel = src.relative_to(a.bron)
        row = {"bron": rel.as_posix(), "type": kind, "sha256": sha[:16], "converter": "",
               "kwaliteit": "", "score": "", "tekens": 0, "llm": "", "fout": ""}
        print(f"[{i}/{len(files)}] {kind:12} {rel}", file=sys.stderr)

        if sha in seen:
            row.update(kwaliteit="duplicaat", fout=f"zelfde als {seen[sha].relative_to(a.bron)}")
            rows.append(row)
            continue
        seen[sha] = src
        if a.dry_run:
            rows.append(row)
            continue

        text, name, status, score, errors = convert_one(src, kind)
        extra: dict = {}

        # Vision: afbeeldingen altijd, PDF's alleen als tekst en OCR niets bruikbaars gaven.
        if a.vision and (kind in BEELD or (kind == "pdf" and status in QUARANTAINE)):
            try:
                if kind in BEELD:
                    ocr = (text or "").strip()
                    text = vision_image(src, a.vision)
                    if ocr:
                        text += f"\n\n## OCR (Tesseract)\n\n{ocr}\n"
                    name, status, score = (name + "+vision").lstrip("+"), "beschreven", ""
                    extra["beschrijving"] = f"gegenereerd door {a.vision}, niet gecontroleerd"
                else:
                    text = vision_pdf(src, a.vision)
                    status, score = quality(text)
                    name = "vision"
                    extra["transcriptie"] = f"gegenereerd door {a.vision}, niet gecontroleerd"
            except Exception as e:  # noqa: BLE001
                errors.append(f"vision: {e}")

        if text is None:
            row.update(kwaliteit=status, fout=" | ".join(errors)[:500])
            rows.append(row)
            continue

        # Tekstmodel: alleen voor documenten, nooit voor data, beeld of media.
        if kind in DOCUMENT and (a.llm_vergelijk or (a.llm and (status == "matig" or name in {"raw_strings", "docx_repair"}))):
            new, stats = llm_cleanup(text, a.model)
            row["llm"] = ", ".join(f"{k} {v}" for k, v in stats.items())
            if a.llm_vergelijk:
                vout = (vergelijk_dir / rel).with_suffix(".md")
                vout.parent.mkdir(parents=True, exist_ok=True)
                vout.write_text(frontmatter({"bron": rel.as_posix(), "model": a.model, "llm": row["llm"]}) + new, "utf-8")
            elif stats.get("aangepast"):
                text = new
                name += "+llm"
                status, score = quality(text)

        out = (a.doel / rel).with_suffix(".md")
        out.parent.mkdir(parents=True, exist_ok=True)
        fm = {"bron": rel.as_posix(), "sha256": sha, "type": kind, "converter": name,
              "kwaliteit": status, **extra, "geconverteerd": f"{datetime.now():%Y-%m-%d}"}
        out.write_text(frontmatter(fm) + text, "utf-8")
        row.update(converter=name, kwaliteit=status, score=score, tekens=len(text),
                   fout=" | ".join(errors)[:500])
        rows.append(row)

    with open(a.doel / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    bad = [r["bron"] for r in rows if r["kwaliteit"] in QUARANTAINE]
    (a.doel / "_quarantaine.txt").write_text("\n".join(bad) + ("\n" if bad else ""), "utf-8")

    print("\nResultaat:", dict(Counter(r["kwaliteit"] or "geïnventariseerd" for r in rows)), file=sys.stderr)
    llm_rows = [r for r in rows if r["llm"]]
    if llm_rows:
        print(f"Langs het taalmodel: {len(llm_rows)} bestanden. Zie kolom 'llm' in manifest.csv.", file=sys.stderr)
    if a.llm_vergelijk:
        print(f"Vergelijkingsversies staan in: {vergelijk_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
