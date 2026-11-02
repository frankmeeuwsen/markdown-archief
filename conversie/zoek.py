# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "markdown-it-py>=3",
# ]
# ///
"""
Zoeken in de Markdown-uitvoer met SQLite FTS5.

Gebruik:
    uv run zoek.py index <markdownmap> [--db PAD]
    uv run zoek.py start <markdownmap> [--db PAD] [--bron BRONMAP] [--poort 8765] [--niet-openen]

'index' bouwt of ververst de zoekindex. Alleen nieuwe en gewijzigde bestanden worden
opnieuw gelezen; verwijderde bestanden verdwijnen uit de index.
'start' ververst de index en start een lokale zoekpagina op http://127.0.0.1:8765.
De server luistert alleen op je eigen computer, niet op het netwerk.

De database is één bestand (standaard zoekindex.db naast de markdownmap). Je kunt hem
weggooien; 'index' bouwt hem opnieuw op.
"""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HIER = Path(__file__).resolve().parent
MAX_RENDER = 300_000   # grotere documenten worden ingekort weergegeven (index bevat alles)

SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id        INTEGER PRIMARY KEY,
    pad       TEXT UNIQUE NOT NULL,     -- relatief aan de markdownmap
    titel     TEXT,
    type      TEXT,
    kwaliteit TEXT,
    converter TEXT,
    bron      TEXT,                     -- pad van het origineel, relatief aan de bronmap
    mtime     REAL,
    grootte   INTEGER,
    tekens    INTEGER
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
    titel, inhoud, pad,
    tokenize = "unicode61 remove_diacritics 2",   -- 'cafe' vindt ook 'café'
    prefix = '2 3'                                -- snelle zoekopdrachten als 'archi*'
);
CREATE TABLE IF NOT EXISTS info (sleutel TEXT PRIMARY KEY, waarde TEXT);
"""


# ================================================================ indexeren

def parse_md(text: str) -> tuple[dict, str]:
    """Splits frontmatter en inhoud. De frontmatter van convert.py is eenvoudig: 'sleutel: waarde'."""
    meta: dict = {}
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip()
                    if v.startswith('"'):
                        try:
                            v = json.loads(v)
                        except ValueError:
                            v = v.strip('"')
                    meta[k.strip()] = v
            text = text[end + 5:]
    return meta, text


def strip_tags(text: str) -> str:
    """Pandoc zet ingewikkelde tabellen als HTML in de Markdown. Voor de index tellen alleen de woorden."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)          # afbeeldingslinks ![](...)
    return html.unescape(re.sub(r"<[^>]{1,500}>", " ", text))


def title_of(body: str, path: Path) -> str:
    for line in body.splitlines()[:80]:
        m = re.match(r"^#{1,3}\s+(.+)", line)
        if m:
            t = re.sub(r"[*_`]+", "", m.group(1)).strip()
            if t:
                return t[:200]
    return path.stem


def md_files(root: Path):
    for p in root.rglob("*.md"):
        rel = p.relative_to(root)
        # .obsidian en dergelijke overslaan, net als _llm-vergelijking
        if any(part.startswith((".", "_")) for part in rel.parts):
            continue
        yield p, rel


def connect(db: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db, check_same_thread=False)
    con.executescript(SCHEMA)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def index(root: Path, db: Path, stil: bool = False) -> None:
    t0 = time.time()
    con = connect(db)
    known = {pad: (mtime, grootte, i) for i, pad, mtime, grootte in
             con.execute("SELECT id, pad, mtime, grootte FROM docs")}
    seen, nieuw, gewijzigd = set(), 0, 0
    with con:
        for path, rel in md_files(root):
            key = rel.as_posix()
            seen.add(key)
            st = path.stat()
            old = known.get(key)
            if old and old[0] == st.st_mtime and old[1] == st.st_size:
                continue
            meta, body = parse_md(path.read_text("utf-8", errors="replace"))
            row = (key, title_of(body, path), meta.get("type", ""), meta.get("kwaliteit", ""),
                   meta.get("converter", ""), meta.get("bron", ""), st.st_mtime, st.st_size, len(body))
            if old:
                con.execute("UPDATE docs SET pad=?, titel=?, type=?, kwaliteit=?, converter=?, bron=?, "
                            "mtime=?, grootte=?, tekens=? WHERE id=?", row + (old[2],))
                con.execute("DELETE FROM docs_fts WHERE rowid=?", (old[2],))
                doc_id = old[2]
                gewijzigd += 1
            else:
                doc_id = con.execute("INSERT INTO docs (pad, titel, type, kwaliteit, converter, bron, "
                                     "mtime, grootte, tekens) VALUES (?,?,?,?,?,?,?,?,?)", row).lastrowid
                nieuw += 1
            con.execute("INSERT INTO docs_fts (rowid, titel, inhoud, pad) VALUES (?,?,?,?)",
                        (doc_id, row[1], strip_tags(body), key))
        weg = [v[2] for k, v in known.items() if k not in seen]
        for doc_id in weg:
            con.execute("DELETE FROM docs WHERE id=?", (doc_id,))
            con.execute("DELETE FROM docs_fts WHERE rowid=?", (doc_id,))
        con.execute("INSERT OR REPLACE INTO info VALUES ('geindexeerd', ?)", (time.strftime("%Y-%m-%d %H:%M"),))
        con.execute("INSERT OR REPLACE INTO info VALUES ('map', ?)", (str(root.resolve()),))
    if nieuw or gewijzigd or weg:
        con.execute("INSERT INTO docs_fts(docs_fts) VALUES('optimize')")
    totaal = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    con.close()
    if not stil:
        print(f"Index bijgewerkt in {time.time() - t0:.1f}s: {nieuw} nieuw, {gewijzigd} gewijzigd, "
              f"{len(weg)} verwijderd, {totaal} documenten in totaal.", file=sys.stderr)


# ================================================================ zoeken

def fts_query(q: str) -> str:
    """Zet gewone invoer om naar een veilige FTS5-zoekopdracht.

    woorden         alle woorden moeten voorkomen
    "vaste zin"     precies deze woorden achter elkaar
    archi*          woorden die zo beginnen
    -woord          mag niet voorkomen
    OF / OR         een van beide
    Het laatste woord krijgt automatisch een * zodat je tijdens het typen al resultaten ziet.
    """
    parts = re.findall(r'-?"[^"]*"?|\S+', q.strip())
    pos: list[str] = []
    neg: list[str] = []
    for i, p in enumerate(parts):
        if p.upper() in ("OR", "OF"):
            if pos and pos[-1] != "OR":
                pos.append("OR")
            continue
        is_neg = p.startswith("-") and len(p) > 1
        if is_neg:
            p = p[1:]
        quoted = p.startswith('"')
        prefix = p.endswith("*") or (i == len(parts) - 1 and not quoted and not is_neg and len(p) >= 2)
        p = p.strip('"').rstrip("*").replace('"', '""')
        if not re.search(r"\w", p):
            continue
        term = f'"{p}"' + ("*" if prefix else "")
        (neg if is_neg else pos).append(term)
    while pos and pos[-1] == "OR":
        pos.pop()
    if not pos:
        return ""
    expr = " ".join(pos)
    if neg:
        expr = f"({expr}) NOT " + " NOT ".join(neg)
    return expr


def zoek(con: sqlite3.Connection, q: str, type_: str, kwaliteit: str, limit: int, offset: int) -> dict:
    t0 = time.perf_counter()
    expr = fts_query(q)
    if not expr:
        return {"totaal": 0, "resultaten": [], "ms": 0, "fout": "Typ minstens één woord om op te zoeken." if q.strip() else ""}
    where, args = ["docs_fts MATCH ?"], [expr]
    if type_:
        where.append("d.type = ?")
        args.append(type_)
    if kwaliteit:
        where.append("d.kwaliteit = ?")
        args.append(kwaliteit)
    w = " AND ".join(where)
    try:
        totaal = con.execute(f"SELECT count(*) FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid WHERE {w}", args).fetchone()[0]
        rows = con.execute(
            f"""SELECT d.id, d.pad, d.titel, d.type, d.kwaliteit, d.tekens,
                       snippet(docs_fts, 1, char(2), char(3), ' … ', 28)
                FROM docs_fts JOIN docs d ON d.id = docs_fts.rowid
                WHERE {w}
                ORDER BY bm25(docs_fts, 10.0, 1.0, 3.0)
                LIMIT ? OFFSET ?""", args + [limit, offset]).fetchall()
    except sqlite3.OperationalError as e:
        return {"totaal": 0, "resultaten": [], "ms": 0, "fout": f"Zoekopdracht niet begrepen ({e})."}
    res = []
    for i, pad, titel, typ, kw, tekens, snip in rows:
        snip = re.sub(r"[*_#`|>]+", " ", snip or "")           # opmaaktekens uit het fragment
        snip = re.sub(r"\s+", " ", snip).strip()
        snip = html.escape(snip).replace("\x02", "<mark>").replace("\x03", "</mark>")
        res.append({"id": i, "pad": pad, "titel": titel, "type": typ, "kwaliteit": kw,
                    "tekens": tekens, "fragment": snip})
    return {"totaal": totaal, "resultaten": res, "query": expr,
            "ms": round((time.perf_counter() - t0) * 1000, 1)}


# ================================================================ server

class App:
    root: Path
    bron: Path | None
    con: sqlite3.Connection
    lock = threading.Lock()


TOEGESTAAN = {"p", "br", "hr", "b", "i", "em", "strong", "u", "s", "del", "sup", "sub", "span", "div",
              "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "dl", "dt", "dd", "blockquote", "pre", "code",
              "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col", "a"}
LEEG = {"br", "hr", "col"}
WEG_MET_INHOUD = {"script", "style", "iframe", "object", "embed", "svg", "math", "template"}


def sanitize(fragment: str) -> str:
    """Laat alleen onschuldige opmaak- en tabeltags door, zonder attributen (behalve colspan,
    rowspan en http(s)-links). Scripts en dergelijke verdwijnen met inhoud en al."""
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.out, self.skip = [], 0

        def handle_starttag(self, tag, attrs):
            if tag in WEG_MET_INHOUD:
                self.skip += 1
            if self.skip or tag not in TOEGESTAAN:
                return
            keep = []
            for k, v in attrs:
                if k in ("colspan", "rowspan") and v and v.isdigit():
                    keep.append(f' {k}="{v}"')
                if tag == "a" and k == "href" and v and re.match(r"https?://", v):
                    keep.append(f' href="{html.escape(v)}" target="_blank" rel="noopener noreferrer"')
            self.out.append(f"<{tag}{''.join(keep)}>")

        def handle_endtag(self, tag):
            if tag in WEG_MET_INHOUD:
                self.skip = max(0, self.skip - 1)
                return
            if not self.skip and tag in TOEGESTAAN and tag not in LEEG:
                self.out.append(f"</{tag}>")

        def handle_data(self, data):
            if not self.skip:
                self.out.append(html.escape(data, quote=False))

        def handle_entityref(self, name):
            if not self.skip:
                self.out.append(f"&{name};")

        def handle_charref(self, name):
            if not self.skip:
                self.out.append(f"&#{name};")

    p = P()
    p.feed(fragment)
    p.close()
    return "".join(p.out)


def render_md(text: str) -> str:
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable("table").enable("strikethrough")
    return sanitize(md.render(text))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # stil
        pass

    def send(self, body: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj, status: int = 200):
        self.send(json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                return self.send((HIER / "zoek.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/zoek":
                with App.lock:
                    r = zoek(App.con, q.get("q", ""), q.get("type", ""), q.get("kwaliteit", ""),
                             min(int(q.get("limit", 30)), 200), int(q.get("offset", 0)))
                return self.json(r)
            if u.path == "/api/info":
                with App.lock:
                    c = App.con
                    return self.json({
                        "totaal": c.execute("SELECT count(*) FROM docs").fetchone()[0],
                        "types": c.execute("SELECT type, count(*) FROM docs GROUP BY type ORDER BY 2 DESC").fetchall(),
                        "kwaliteit": c.execute("SELECT kwaliteit, count(*) FROM docs GROUP BY kwaliteit ORDER BY 2 DESC").fetchall(),
                        "geindexeerd": (c.execute("SELECT waarde FROM info WHERE sleutel='geindexeerd'").fetchone() or [""])[0],
                        "bron": App.bron is not None,
                    })
            if u.path == "/api/doc":
                with App.lock:
                    row = App.con.execute("SELECT pad, titel, type, kwaliteit, converter, bron FROM docs WHERE id=?",
                                          (int(q["id"]),)).fetchone()
                if not row:
                    return self.json({"fout": "niet gevonden"}, 404)
                pad, titel, typ, kw, conv, bron = row
                meta, body = parse_md((App.root / pad).read_text("utf-8", errors="replace"))
                ingekort = len(body) > MAX_RENDER
                return self.json({"pad": pad, "titel": titel, "meta": meta, "ingekort": ingekort,
                                  "html": render_md(body[:MAX_RENDER]),
                                  "bron": bool(App.bron and bron and (App.bron / bron).is_file())})
            if u.path == "/origineel" and App.bron:
                with App.lock:
                    row = App.con.execute("SELECT bron FROM docs WHERE id=?", (int(q["id"]),)).fetchone()
                if row and row[0]:
                    f = (App.bron / row[0]).resolve()
                    if f.is_relative_to(App.bron.resolve()) and f.is_file():
                        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
                        return self.send(f.read_bytes(), ctype)
                return self.send(b"Origineel niet gevonden", "text/plain; charset=utf-8", 404)
            if u.path == "/api/herindex":
                with App.lock:
                    index(App.root, Path(App.con.execute("PRAGMA database_list").fetchone()[2]), stil=True)
                return self.json({"ok": True})
            self.send(b"Niet gevonden", "text/plain; charset=utf-8", 404)
        except (KeyError, ValueError) as e:
            self.json({"fout": f"ongeldige vraag: {e}"}, 400)


def start(root: Path, db: Path, bron: Path | None, poort: int, openen: bool) -> None:
    index(root, db)
    App.root, App.bron = root.resolve(), bron.resolve() if bron else None
    App.con = connect(db)
    srv = ThreadingHTTPServer(("127.0.0.1", poort), Handler)
    url = f"http://127.0.0.1:{poort}"
    print(f"Zoekpagina draait op {url}  (stoppen met Ctrl+C)", file=sys.stderr)
    if openen:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nGestopt.", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="Zoekindex en zoekpagina voor de Markdown-uitvoer.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("index", "start"):
        p = sub.add_parser(name)
        p.add_argument("map", type=Path, help="map met de .md-bestanden (uitvoer van convert.py)")
        p.add_argument("--db", type=Path, help="pad naar de database (standaard: zoekindex.db naast de map)")
        if name == "start":
            p.add_argument("--bron", type=Path, help="bronmap, om originelen te kunnen openen")
            p.add_argument("--poort", type=int, default=8765)
            p.add_argument("--niet-openen", action="store_true", help="browser niet automatisch openen")
    a = ap.parse_args()
    if not a.map.is_dir():
        sys.exit(f"Map niet gevonden: {a.map.resolve()}")
    db = a.db or a.map.resolve().parent / "zoekindex.db"
    if a.cmd == "index":
        index(a.map, db)
    else:
        if a.bron and not a.bron.is_dir():
            sys.exit(f"Bronmap niet gevonden: {a.bron.resolve()}")
        start(a.map, db, a.bron, a.poort, not a.niet_openen)


if __name__ == "__main__":
    main()
