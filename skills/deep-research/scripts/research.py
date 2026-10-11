#!/usr/bin/env python3
"""
Evidence store for deep research: questions, fetched sources split into numbered passages,
claims linked to the passages that support or contradict them, and a renderer that turns
claim markers in a draft into numbered citations with a source list.

State lives in /workspace/research (override with RESEARCH_DIR): evidence.db and sources/sN.txt.
Run `research.py <command> --help` for the arguments of each command.
"""
import argparse
import concurrent.futures
import datetime
import hashlib
import html
import json
import os
import secrets
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

RESEARCH_DIR = os.environ.get("RESEARCH_DIR", "/workspace/research")
DB_PATH = os.path.join(RESEARCH_DIR, "evidence.db")
SOURCES_DIR = os.path.join(RESEARCH_DIR, "sources")
USER_AGENT = "Mozilla/5.0 (compatible; SecondStackResearch/1.0; research agent)"
MAX_BYTES = 15 * 1024 * 1024
FETCH_TIMEOUT = 30
FETCH_WORKERS = 6
THIN_TEXT_CHARS = 600
MAX_PASSAGE_CHARS = 1200
SHORT_BLOCK_CHARS = 80
MERGED_BLOCK_CHARS = 400
MIN_QUOTE_CHARS = 12
PREVIEW_CHARS = 500

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "templates", "report.html")
EXPORT_OFFER = "Want this as a shareable Site, a Markdown file or a PDF?"
EVIDENCE_LINE_PREFIX = "The quoted passage behind every citation is in evidence.md"
SITE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
QUESTION_STATUSES = ("open", "answered", "partial", "blocked", "dropped")
UNSOURCED_KINDS = ("inference", "opinion")
MAX_LINKS_PER_SOURCE = 4
NEARBY_PASSAGES = 12
MAX_MARKERS_PER_SENTENCE = 4
COMPOUND_FIGURES = 3
VERDICTS = ("pending", "supported", "partial", "contradicted", "unsupported", "insufficient")
STOP_REASONS = ("sufficient", "diminishing", "access", "deadline")
# Each depth raises the evidence bar the store enforces; search counts are only a guide.
DEPTHS = {
    "quick": {"questions": 1, "publishers": 1, "counter": False, "rounds": 1, "sources": 1, "primary": False, "searches": 3},
    "standard": {"questions": 3, "publishers": 2, "counter": True, "rounds": 2, "sources": 4, "primary": False, "searches": 10},
    "deep": {"questions": 5, "publishers": 3, "counter": True, "rounds": 3, "sources": 10, "primary": True, "searches": 25},
}
CHALLENGE_PATTERN = re.compile(
    r"just a moment|verify you are (a )?human|enable javascript and cookies|attention required|"
    r"access denied|are you a robot|captcha|unusual traffic",
    re.I,
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY, text TEXT NOT NULL, needs TEXT, priority INTEGER NOT NULL DEFAULT 2,
  status TEXT NOT NULL DEFAULT 'open', note TEXT, counter INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS searches (
  id INTEGER PRIMARY KEY, query TEXT NOT NULL, question_id INTEGER, results INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY, url TEXT NOT NULL UNIQUE, final_url TEXT, title TEXT, publisher TEXT,
  published TEXT, modified TEXT, content_kind TEXT, fetch_status TEXT NOT NULL, http_status INTEGER, chars INTEGER,
  passages INTEGER, question_id INTEGER, why TEXT, note TEXT, fetched_at TEXT NOT NULL,
  round INTEGER, "primary" INTEGER NOT NULL DEFAULT 0, content_sha TEXT);
CREATE TABLE IF NOT EXISTS claims (
  id INTEGER PRIMARY KEY, question_id INTEGER, statement TEXT NOT NULL, kind TEXT NOT NULL,
  as_of TEXT, verdict TEXT NOT NULL DEFAULT 'pending', note TEXT, quote TEXT, shown INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS claim_links (
  claim_id INTEGER NOT NULL, source_id INTEGER NOT NULL, passage INTEGER, role TEXT NOT NULL);
"""


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)


def connect() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        fail(f"no research store at {RESEARCH_DIR}; run `init` first")
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    for table, column, ddl in (("questions", "counter", "counter INTEGER NOT NULL DEFAULT 0"), ("sources", "round", "round INTEGER"), ("sources", "primary", '"primary" INTEGER NOT NULL DEFAULT 0'), ("sources", "content_sha", "content_sha TEXT")):
        if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(claims)")}
    if "quote" not in columns:
        db.execute("ALTER TABLE claims ADD COLUMN quote TEXT")
    if "shown" not in columns:
        db.execute("ALTER TABLE claims ADD COLUMN shown INTEGER NOT NULL DEFAULT 0")
    return db


def meta(db: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def depth(db: sqlite3.Connection) -> tuple[str, dict]:
    name = meta(db, "depth", "standard")
    return name, DEPTHS[name]


def rounds_done(db: sqlite3.Connection) -> int:
    return db.execute("SELECT COUNT(DISTINCT round) FROM sources WHERE round IS NOT NULL").fetchone()[0]


def parse_id(value: str, prefix: str) -> int:
    match = re.fullmatch(rf"{prefix}?(\d+)", value.strip(), re.I)
    if not match:
        fail(f"expected an id like {prefix}3, got {value!r}")
    return int(match.group(1))


def parse_ref(value: str) -> tuple[int, int, int]:
    """Parses s3:p12 or s3:p10-14 into (source, first passage, last passage)."""
    match = re.fullmatch(r"s(\d+):p(\d+)(?:-p?(\d+))?", value.strip(), re.I)
    if not match:
        fail(f"expected a passage reference like s3:p12 or s3:p10-14, got {value!r}")
    first = int(match.group(2))
    last = int(match.group(3) or first)
    if last < first:
        fail(f"passage range {value!r} runs backwards")
    return int(match.group(1)), first, last


def snapshot_path(source_id: int) -> str:
    return os.path.join(SOURCES_DIR, f"s{source_id}.txt")


def load_passages(source_id: int) -> dict[int, str]:
    path = snapshot_path(source_id)
    if not os.path.exists(path):
        return {}
    passages = {}
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"\[p(\d+)\] (.*)", line.rstrip("\n"))
            if match:
                passages[int(match.group(1))] = match.group(2)
    return passages


# --- extraction -------------------------------------------------------------------------------


def split_long(text: str) -> list[str]:
    if len(text) <= MAX_PASSAGE_CHARS:
        return [text]
    parts, current = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if current and len(current) + len(sentence) + 1 > MAX_PASSAGE_CHARS:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        parts.append(current)
    return [chunk for part in parts for chunk in (part[i : i + MAX_PASSAGE_CHARS * 2] for i in range(0, len(part), MAX_PASSAGE_CHARS * 2))]


def clean_passages(blocks: list[str]) -> list[str]:
    """Short blocks (labels, prices, table cells) merge with their neighbours, and a heading opens
    a new passage, so one passage holds one product card or table row rather than one label."""
    passages, previous, merged = [], None, []

    def flush() -> None:
        if merged:
            passages.append(" · ".join(merged))
            merged.clear()

    for block in blocks:
        text = re.sub(r"\s+", " ", block).strip(" |")
        if re.fullmatch(r"#+", text):
            flush()
            continue
        if len(text) < 2 or text == previous:
            continue
        previous = text
        if text.startswith("## "):
            flush()
            merged.append(text)
        elif len(text) < SHORT_BLOCK_CHARS:
            merged.append(text)
            if sum(len(part) + 3 for part in merged) >= MERGED_BLOCK_CHARS:
                flush()
        else:
            if merged and merged[0].startswith("## ") and len(merged) == 1:
                text = f"{merged.pop()} · {text}"
            flush()
            passages.extend(split_long(text))
    flush()
    return passages


def html_metadata(document) -> dict[str, str]:
    def first_meta(*names: str) -> str | None:
        for name in names:
            for attribute in ("property", "name", "itemprop"):
                values = document.xpath(f'//meta[translate(@{attribute}, "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")="{name}"]/@content')
                if values and values[0].strip():
                    return values[0].strip()
        return None

    title = first_meta("og:title", "citation_title", "dc.title", "twitter:title")
    if not title:
        titles = document.xpath("//title/text()")
        title = titles[0].strip() if titles else None
    published = first_meta(
        "article:published_time", "citation_publication_date", "citation_date", "dc.date",
        "dcterms.date", "date", "pubdate", "publish-date", "datepublished",
    )
    if not published:
        for script in document.xpath('//script[@type="application/ld+json"]/text()'):
            match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', script)
            if match:
                published = match.group(1)
                break
    if not published:
        times = document.xpath("//time/@datetime")
        published = times[0].strip() if times else None
    modified = first_meta("article:modified_time", "datemodified", "last-modified")
    if not modified:
        for script in document.xpath('//script[@type="application/ld+json"]/text()'):
            match = re.search(r'"dateModified"\s*:\s*"([^"]+)"', script)
            if match:
                modified = match.group(1)
                break
    return {
        "title": title or "",
        "publisher": first_meta("og:site_name", "citation_publisher", "dc.publisher", "citation_journal_title") or "",
        "published": published or "",
        "modified": modified or "",
    }


def decode_html(body: bytes, charset: str | None) -> str:
    """lxml guesses Latin-1 for bytes without a declared charset, which garbles UTF-8 pages."""
    declared = charset or (lambda m: m.group(1).decode("ascii") if m else None)(re.search(rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I))
    for encoding in filter(None, (declared, "utf-8")):
        try:
            text = body.decode(encoding)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    else:
        text = body.decode("cp1252", errors="replace")
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text)


def extract_html(body: bytes, charset: str | None) -> tuple[dict[str, str], list[str]]:
    import lxml.html

    document = lxml.html.document_fromstring(decode_html(body, charset))
    metadata = html_metadata(document)
    for element in document.xpath("//script|//style|//noscript|//nav|//footer|//header|//aside|//form|//svg|//iframe|//button|//template"):
        element.drop_tree()
    candidates = document.xpath("//article") or document.xpath("//main") or document.xpath('//*[@role="main"]') or document.xpath("//body") or [document]
    root = max(candidates, key=lambda element: len(element.text_content()))
    blocks = {"p", "div", "li", "pre", "blockquote", "tr", "section", "article", "dd", "dt", "figcaption", "caption", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"}
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if element.tag in ("td", "th"):
            element.tail = " | " + (element.tail or "")
        elif element.tag == "br":
            element.tail = " " + (element.tail or "")
        if element.tag in blocks:
            prefix = "\n\n## " if element.tag in ("h1", "h2", "h3", "h4", "h5", "h6") else "\n\n"
            element.text = prefix + (element.text or "")
            element.tail = "\n\n" + (element.tail or "")
    text = "".join(root.itertext())
    return metadata, clean_passages(re.split(r"\n\s*\n", text))


def extract_pdf(body: bytes) -> tuple[dict[str, str], list[str]]:
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "source.pdf")
        with open(path, "wb") as handle:
            handle.write(body)
        info = subprocess.run(["pdfinfo", "-isodates", path], capture_output=True, text=True).stdout
        text = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"], capture_output=True, text=True, check=True).stdout
    fields = {key: value.strip() for key, value in re.findall(r"^(\w[\w ]*?):[ \t]*(.*)$", info, re.M)}
    passages = []
    for page_number, page in enumerate(text.split("\f"), start=1):
        for passage in clean_passages(re.split(r"\n\s*\n", page)):
            passages.append(f"(page {page_number}) {passage}")
    return {"title": fields.get("Title", ""), "publisher": "", "published": fields.get("CreationDate", ""), "modified": fields.get("ModDate", "")}, passages


def extract_text(body: bytes, charset: str | None) -> tuple[dict[str, str], list[str]]:
    text = body.decode(charset or "utf-8", errors="replace")
    return {"title": "", "publisher": "", "published": "", "modified": ""}, clean_passages(re.split(r"\n\s*\n", text))


def classify(url: str, content_type: str, body: bytes) -> str:
    if "pdf" in content_type or body[:5] == b"%PDF-" or urllib.parse.urlparse(url).path.lower().endswith(".pdf"):
        return "pdf"
    if "html" in content_type or "xml" in content_type and b"<html" in body[:2000].lower():
        return "html"
    if content_type.startswith("text/") or "json" in content_type or content_type == "":
        return "html" if b"<html" in body[:2000].lower() else "text"
    return "binary"


def fetch_http(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/pdf,text/plain;q=0.9,*/*;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
            return {
                "final_url": response.geturl(),
                "http_status": response.status,
                "content_type": response.headers.get_content_type() or "",
                "charset": response.headers.get_content_charset(),
                "body": body[:MAX_BYTES],
                "truncated": len(body) > MAX_BYTES,
            }
    except urllib.error.HTTPError as error:
        body = error.read(200_000) if error.fp else b""
        return {"final_url": url, "http_status": error.code, "content_type": error.headers.get_content_type() if error.headers else "", "charset": None, "body": body, "truncated": False, "error": f"HTTP {error.code}"}
    except Exception as error:  # noqa: BLE001 - reported per URL in the fetch result
        return {"final_url": url, "http_status": None, "content_type": "", "charset": None, "body": b"", "truncated": False, "error": f"{type(error).__name__}: {error}"}


def fetch_browser(urls: list[str]) -> dict[str, dict]:
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for url in urls:
            try:
                response = page.goto(url, wait_until="load", timeout=45_000)
                page.wait_for_timeout(1500)
                results[url] = {
                    "final_url": page.url,
                    "http_status": response.status if response else None,
                    "content_type": "text/html",
                    "charset": "utf-8",
                    "body": page.content().encode("utf-8"),
                    "truncated": False,
                }
            except Exception as error:  # noqa: BLE001 - reported per URL in the fetch result
                results[url] = {"final_url": url, "http_status": None, "content_type": "", "charset": None, "body": b"", "truncated": False, "error": f"{type(error).__name__}: {error}"}
        browser.close()
    return results


def process(url: str, raw: dict) -> dict:
    result = {"url": url, "final_url": raw["final_url"], "http_status": raw["http_status"], "metadata": {}, "passages": []}
    body = raw["body"]
    kind = classify(raw["final_url"], raw["content_type"], body) if body else "none"
    result["content_kind"] = kind
    if kind == "pdf":
        result["metadata"], result["passages"] = extract_pdf(body)
    elif kind == "html":
        result["metadata"], result["passages"] = extract_html(body, raw["charset"])
    elif kind == "text":
        result["metadata"], result["passages"] = extract_text(body, raw["charset"])
    chars = sum(len(passage) for passage in result["passages"])
    result["chars"] = chars
    sample = " ".join(result["passages"][:40])
    status = raw["http_status"]
    if kind == "binary":
        result["fetch_status"] = "unsupported"
    elif raw.get("error") and status is None:
        result["fetch_status"] = "error"
    elif status in (401, 402, 403, 429, 503) and (CHALLENGE_PATTERN.search(sample) or chars < 3000):
        result["fetch_status"] = "blocked"
    elif status is not None and status >= 400:
        result["fetch_status"] = f"http_{status}"
    elif chars < 3000 and CHALLENGE_PATTERN.search(sample):
        result["fetch_status"] = "blocked"
    elif chars < THIN_TEXT_CHARS:
        result["fetch_status"] = "thin"
    else:
        result["fetch_status"] = "ok"
    if raw.get("error"):
        result["error"] = raw["error"]
    if raw.get("truncated"):
        result["note"] = f"download truncated at {MAX_BYTES} bytes"
    return result


# --- commands -------------------------------------------------------------------------------


def cmd_init(args) -> None:
    if os.path.exists(DB_PATH):
        if not args.fresh:
            db = connect()
            print(f"research store already exists for: {meta(db, 'question')}")
            if args.depth and args.depth != depth(db)[0]:
                set_meta(db, "depth", args.depth)
                db.execute("DELETE FROM meta WHERE key IN ('stop_reason', 'stop_note', 'stopped')")
                db.commit()
                print(f"depth changed to {args.depth}; the store keeps every source and claim, and the new bar applies to the next stop")
                print_depth(args.depth)
            else:
                print("use it for follow-ups; pass --fresh to archive it and start a new investigation")
            return
        archive = f"{RESEARCH_DIR}-archive/{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
        os.makedirs(os.path.dirname(archive), exist_ok=True)
        shutil.move(RESEARCH_DIR, archive)
        print(f"archived previous investigation to {archive}")
    os.makedirs(SOURCES_DIR, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    set_meta(db, "question", args.question)
    set_meta(db, "depth", args.depth or "standard")
    set_meta(db, "created", now())
    db.commit()
    print(f"initialized {RESEARCH_DIR}; write the brief to {RESEARCH_DIR}/brief.md")
    print_depth(args.depth or "standard")


def print_depth(name: str) -> None:
    bar = DEPTHS[name]
    print(
        f"depth {name}: at least {bar['questions']} question(s); {bar['publishers']} independent publisher(s) per priority-1 question"
        + ("; a counter-evidence question" if bar["counter"] else "")
        + ("; a primary source per priority-1 question" if bar["primary"] else "")
        + f"; {bar['rounds']} fetch round(s); {bar['sources']} readable cited source(s); about {bar['searches']} searches as a guide"
    )


def cmd_q(args) -> None:
    db = connect()
    if args.action == "add":
        cursor = db.execute("INSERT INTO questions (text, needs, priority, counter) VALUES (?, ?, ?, ?)", (args.text, args.needs, args.priority, int(args.counter)))
        db.commit()
        print(f"q{cursor.lastrowid}")
    elif args.action == "set":
        question_id = parse_id(args.id, "q")
        if args.status in ("dropped", "blocked", "partial") and not args.note:
            fail(f"setting a question {args.status} needs --note with the reason; the report must state it")
        if args.status == "answered" and not db.execute("SELECT 1 FROM claims WHERE question_id = ? AND verdict IN ('supported', 'partial')", (question_id,)).fetchone():
            fail(f"q{question_id} has no verified claim; record and verify what answers it, or set it partial with a note")
        if db.execute("UPDATE questions SET status = ?, note = COALESCE(?, note) WHERE id = ?", (args.status, args.note, question_id)).rowcount == 0:
            fail(f"no question q{question_id}")
        db.commit()
        print(f"q{question_id} {args.status}")
    else:
        for row in db.execute("SELECT * FROM questions ORDER BY priority, id"):
            claims = db.execute("SELECT COUNT(*) FROM claims WHERE question_id = ?", (row["id"],)).fetchone()[0]
            print(f"q{row['id']} [{row['status']}] p{row['priority']}{' counter' if row['counter'] else ''} claims={claims}: {row['text']}")
            if row["needs"]:
                print(f"    needs: {row['needs']}")
            if row["note"]:
                print(f"    note: {row['note']}")


def cmd_searched(args) -> None:
    db = connect()
    question_id = parse_id(args.q, "q") if args.q else None
    db.executemany("INSERT INTO searches (query, question_id, results, at) VALUES (?, ?, ?, ?)", [(query, question_id, args.results, now()) for query in args.queries])
    db.commit()
    used = db.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    name, bar = depth(db)
    print(f"searches {used} (depth {name} guide: about {bar['searches']})")


def cmd_fetch(args) -> None:
    db = connect()
    question_id = parse_id(args.q, "q") if args.q else None
    urls, outputs = [], []
    for url in dict.fromkeys(args.urls):
        if not re.match(r"https://", url):
            outputs.append({"url": url, "fetch_status": "rejected", "error": "only https:// URLs are reachable from the workspace"})
            continue
        existing = db.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()
        if existing and existing["fetch_status"] == "ok" and not args.refresh and not args.browser:
            outputs.append({"source": f"s{existing['id']}", "url": url, "fetch_status": "ok", "already_fetched": True, "title": existing["title"], "passages": existing["passages"]})
            continue
        urls.append(url)
    new_urls = [url for url in urls if not db.execute("SELECT 1 FROM sources WHERE url = ?", (url,)).fetchone()]
    fetch_round = (db.execute("SELECT COALESCE(MAX(round), 0) FROM sources").fetchone()[0] + 1) if new_urls else None
    if args.browser:
        raw_results = fetch_browser(urls)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            raw_results = dict(zip(urls, pool.map(fetch_http, urls)))
    for url in urls:
        result = process(url, raw_results[url])
        metadata = result["metadata"]
        source_id, output = store_source(db, url, result, question_id, args.why, fetch_round)
        if output.get("kept_cited_snapshot"):
            outputs.append(output)
            continue
        if result.get("error"):
            output["error"] = result["error"]
        if result.get("note"):
            output["note"] = result["note"]
        if result["fetch_status"] in ("thin", "blocked") and not args.browser:
            output["hint"] = "retry with --browser; if still blocked, find another source (do not evade the challenge)"
        elif result["fetch_status"] == "ok" and not metadata.get("title"):
            output["hint"] = f"no title found; set it with `source s{source_id} --title ... --publisher ...`"
        if args.preview and result["fetch_status"] == "ok":
            output["preview"] = " ".join(result["passages"])[:PREVIEW_CHARS]
        outputs.append(output)
    db.commit()
    for output in outputs:
        print(json.dumps(output, ensure_ascii=False))


def content_hash(passages: list[str]) -> str:
    return hashlib.sha256("\n".join(passages).encode("utf-8")).hexdigest()


def citing_claims(db: sqlite3.Connection, source_id: int) -> list[int]:
    return [row["claim_id"] for row in db.execute("SELECT DISTINCT claim_id FROM claim_links WHERE source_id = ? ORDER BY claim_id", (source_id,))]


def store_source(db: sqlite3.Connection, url: str, result: dict, question_id: int | None, why: str | None, fetch_round: int | None, publisher: str | None = None) -> tuple[int, dict]:
    """Stores a fetched or added source. A source that claims already cite keeps its snapshot: changed or
    failed new text goes to a side file, so passage numbers behind recorded claims never shift."""
    metadata = result["metadata"]
    new_hash = content_hash(result["passages"])
    existing = db.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()
    if existing:
        cited = citing_claims(db, existing["id"])
        old_hash = existing["content_sha"] or content_hash(list(load_passages(existing["id"]).values()))
        if cited and (result["fetch_status"] != "ok" or new_hash != old_hash):
            side = os.path.join(SOURCES_DIR, f"s{existing['id']}.refetch-{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}.txt")
            write_snapshot(side, existing["id"], url, result)
            return existing["id"], {
                "source": f"s{existing['id']}", "url": url, "fetch_status": result["fetch_status"], "kept_cited_snapshot": True,
                "warning": f"the page changed or failed since claims {' '.join(f'c{c}' for c in cited)} cited it; the cited snapshot is kept and the new text is in {side}. Compare them; if the change matters, say so in the report",
            }
    values = (
        result["final_url"], metadata.get("title") or None, publisher or metadata.get("publisher") or urllib.parse.urlparse(result["final_url"]).hostname,
        metadata.get("published") or None, metadata.get("modified") or None, result["content_kind"], result["fetch_status"], result["http_status"], result["chars"],
        len(result["passages"]), question_id, why, result.get("note") or result.get("error"), now(), new_hash,
    )
    if existing:
        source_id = existing["id"]
        db.execute(
            "UPDATE sources SET final_url=?, title=?, publisher=?, published=?, modified=?, content_kind=?, fetch_status=?, http_status=?, chars=?, passages=?, question_id=COALESCE(question_id, ?), why=COALESCE(?, why), note=?, fetched_at=?, content_sha=? WHERE id=?",
            (*values, source_id),
        )
    else:
        source_id = db.execute(
            "INSERT INTO sources (final_url, title, publisher, published, modified, content_kind, fetch_status, http_status, chars, passages, question_id, why, note, fetched_at, content_sha, url, round) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*values, url, fetch_round),
        ).lastrowid
    write_snapshot(snapshot_path(source_id), source_id, result["final_url"], result)
    return source_id, {
        "source": f"s{source_id}", "url": url, "fetch_status": result["fetch_status"], "http_status": result.get("http_status"),
        "content_kind": result["content_kind"], "title": metadata.get("title") or None, "published": metadata.get("published") or None,
        "modified": metadata.get("modified") or None,
        "passages": len(result["passages"]), "chars": result["chars"], "snapshot": snapshot_path(source_id),
    }


def write_snapshot(path: str, source_id: int, url: str, result: dict) -> None:
    metadata = result["metadata"]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# s{source_id} {metadata.get('title') or ''}\n# url: {url}\n# published: {metadata.get('published') or 'unknown'}\n")
        if metadata.get("modified"):
            handle.write(f"# modified: {metadata['modified']}\n")
        handle.write(f"# fetched: {now()} status: {result['fetch_status']}\n\n")
        for number, passage in enumerate(result["passages"], start=1):
            handle.write(f"[p{number}] {passage}\n")


OFFICE_EXTENSIONS = (".doc", ".xls", ".xlsx", ".ods", ".ppt", ".pptx")
PANDOC_EXTENSIONS = (".docx", ".odt", ".rtf", ".epub")


def extract_file(path: str) -> tuple[str, dict[str, str], list[str]]:
    extension = os.path.splitext(path)[1].lower()
    with open(path, "rb") as handle:
        body = handle.read()
    if extension == ".pdf" or body[:5] == b"%PDF-":
        return ("pdf", *extract_pdf(body))
    if extension in (".html", ".htm"):
        return ("html", *extract_html(body, None))
    if extension in PANDOC_EXTENSIONS:
        text = subprocess.run(["pandoc", path, "-t", "plain", "--wrap=none"], capture_output=True, text=True, check=True).stdout
        return ("document", {"title": "", "publisher": "", "published": "", "modified": ""}, clean_passages(re.split(r"\n\s*\n", text)))
    if extension in OFFICE_EXTENSIONS:
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", directory, path], capture_output=True, check=True)
            converted = os.path.join(directory, os.path.splitext(os.path.basename(path))[0] + ".pdf")
            with open(converted, "rb") as handle:
                _, passages = extract_pdf(handle.read())
            # The converted PDF's dates are the conversion time, not the document's.
            return ("document", {"title": "", "publisher": "", "published": "", "modified": ""}, passages)
    return ("text", *extract_text(body, None))


def cmd_add_file(args) -> None:
    db = connect()
    question_id = parse_id(args.q, "q") if args.q else None
    new_paths = []
    for raw_path in args.paths:
        path = os.path.realpath(raw_path)
        if not os.path.isfile(path):
            fail(f"{raw_path} is not a file in the workspace; import a Chat file with import_chat_file or Knowledge with download_collection_file first")
        new_paths.append(path)
    fresh = [path for path in new_paths if not db.execute("SELECT 1 FROM sources WHERE url = ?", (f"file://{path}",)).fetchone()]
    file_round = (db.execute("SELECT COALESCE(MAX(round), 0) FROM sources").fetchone()[0] + 1) if fresh else None
    for path in new_paths:
        kind, metadata, passages = extract_file(path)
        metadata = {**metadata, "title": args.title or metadata.get("title") or os.path.basename(path), "published": args.published or metadata.get("published") or ""}
        chars = sum(len(passage) for passage in passages)
        result = {
            "final_url": f"file://{path}", "http_status": None, "metadata": metadata, "passages": passages, "content_kind": kind,
            "chars": chars, "fetch_status": "ok" if passages else "error",
        }
        source_id, output = store_source(db, f"file://{path}", result, question_id, args.why, file_round, publisher=args.publisher)
        if args.primary and not output.get("kept_cited_snapshot"):
            db.execute('UPDATE sources SET "primary" = 1 WHERE id = ?', (source_id,))
        print(json.dumps(output, ensure_ascii=False))
    db.commit()


def cmd_snippet(args) -> None:
    db = connect()
    question_id = parse_id(args.q, "q") if args.q else None
    existing = db.execute("SELECT id, fetch_status FROM sources WHERE url = ?", (args.url,)).fetchone()
    if existing and existing["fetch_status"] == "ok":
        fail(f"s{existing['id']} was fetched; cite its passages instead of a snippet")
    if existing:
        source_id = existing["id"]
        db.execute("UPDATE sources SET fetch_status='snippet', title=COALESCE(?, title), publisher=COALESCE(?, publisher), published=COALESCE(?, published), passages=1, note=? WHERE id=?", (args.title, args.publisher, args.published, args.reason, source_id))
    else:
        source_id = db.execute(
            "INSERT INTO sources (url, final_url, title, publisher, published, content_kind, fetch_status, passages, question_id, note, fetched_at) VALUES (?, ?, ?, ?, ?, 'snippet', 'snippet', 1, ?, ?, ?)",
            (args.url, args.url, args.title, args.publisher or urllib.parse.urlparse(args.url).hostname, args.published, question_id, args.reason, now()),
        ).lastrowid
    with open(snapshot_path(source_id), "w", encoding="utf-8") as handle:
        handle.write(f"# s{source_id} {args.title or ''}\n# url: {args.url}\n# search snippet only, page not read: {args.reason}\n\n[p1] {re.sub(chr(10), ' ', args.excerpt)}\n")
    db.commit()
    print(f"s{source_id}:p1 (snippet only)")


def cmd_source(args) -> None:
    db = connect()
    source_id = parse_id(args.id, "s")
    updates = {key: value for key, value in (("title", args.title), ("publisher", args.publisher), ("published", args.published), ("note", args.note)) if value is not None}
    if args.primary:
        updates['"primary"'] = 1
    if updates:
        db.execute(f"UPDATE sources SET {', '.join(f'{key} = ?' for key in updates)} WHERE id = ?", (*updates.values(), source_id))
        db.commit()
    row = db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        fail(f"no source s{source_id}")
    print(json.dumps({key: row[key] for key in row.keys()}, ensure_ascii=False))


def cmd_sources(args) -> None:
    db = connect()
    query = "SELECT * FROM sources" + (" WHERE fetch_status = ?" if args.status else "") + " ORDER BY id"
    for row in db.execute(query, (args.status,) if args.status else ()):
        print(f"s{row['id']} [{row['fetch_status']}] {row['passages'] or 0}p {row['published'] or 'undated'} | {row['title'] or ''} | {row['url']}")


def cmd_find(args) -> None:
    db = connect()
    if args.source == "all":
        source_ids = [row["id"] for row in db.execute("SELECT id FROM sources WHERE fetch_status IN ('ok', 'thin', 'snippet') ORDER BY id")]
    else:
        source_ids = [parse_id(args.source, "s")]
    pattern = re.compile(args.pattern, re.I)
    found = 0
    for source_id in source_ids:
        for number, passage in load_passages(source_id).items():
            match = pattern.search(passage)
            if not match:
                continue
            start = max(0, match.start() - 200)
            excerpt = ("…" if start else "") + passage[start : start + 400] + ("…" if start + 400 < len(passage) else "")
            print(f"s{source_id}:p{number} {excerpt}")
            found += 1
            if found >= args.max:
                print(f"[stopped at {args.max} matches; narrow the pattern or raise --max]")
                return
    if not found:
        print("no matches")


def cmd_read(args) -> None:
    connect()
    for ref in args.refs:
        source_id, first, last = parse_ref(ref)
        passages = load_passages(source_id)
        if not passages:
            fail(f"no snapshot for s{source_id}")
        first, last = max(1, first - args.context), min(max(passages), last + args.context)
        for number in range(first, last + 1):
            if number in passages:
                print(f"s{source_id}:p{number} {passages[number]}")
        print()


def validated_links(db: sqlite3.Connection, refs: list[str] | None) -> list[tuple[int, int]]:
    links = []
    for ref in refs or []:
        source_id, first, last = parse_ref(ref)
        source = db.execute("SELECT fetch_status FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not source:
            fail(f"no source s{source_id}")
        passages = load_passages(source_id)
        for number in range(first, last + 1):
            if number not in passages:
                fail(f"s{source_id} has no passage p{number}; read it before citing")
            links.append((source_id, number))
    return links


def cmd_claim(args) -> None:
    db = connect()
    if args.action == "add":
        support = validated_links(db, args.support)
        counter = validated_links(db, args.counter)
        if not support and args.kind not in UNSOURCED_KINDS:
            fail("a factual claim needs --support passages; record an unsupported idea as --kind inference")
        question_id = parse_id(args.q, "q") if args.q else None
        claim_id = db.execute("INSERT INTO claims (question_id, statement, kind, as_of) VALUES (?, ?, ?, ?)", (question_id, args.statement, args.kind, args.as_of)).lastrowid
        db.executemany("INSERT INTO claim_links (claim_id, source_id, passage, role) VALUES (?, ?, ?, ?)", [(claim_id, s, p, "support") for s, p in support] + [(claim_id, s, p, "counter") for s, p in counter])
        db.commit()
        print(f"c{claim_id}")
        figures = re.findall(r"(?<![\w-])[$€£]?\d[\d,.]*|\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b", args.statement, re.I)
        if len(figures) >= COMPOUND_FIGURES or ";" in args.statement:
            print(f"warning: c{claim_id} bundles several facts; each one is verified on its own only if it is its own claim")
        per_source: dict[int, int] = {}
        for source_id, _ in support:
            per_source[source_id] = per_source.get(source_id, 0) + 1
        if any(count > MAX_LINKS_PER_SOURCE for count in per_source.values()):
            print(f"warning: c{claim_id} links more than {MAX_LINKS_PER_SOURCE} passages of one source; link only the passages that state the claim")
        for hint in conflicting_values(args.statement, support):
            print(hint)
    elif args.action == "link":
        claim_id = parse_id(args.id, "c")
        links = validated_links(db, args.support)
        counter = validated_links(db, args.counter)
        db.executemany("INSERT INTO claim_links (claim_id, source_id, passage, role) VALUES (?, ?, ?, ?)", [(claim_id, s, p, "support") for s, p in links] + [(claim_id, s, p, "counter") for s, p in counter])
        db.execute("UPDATE claims SET shown = 0 WHERE id = ?", (claim_id,))
        db.commit()
        print(f"c{claim_id} linked")
    elif args.action == "verdict":
        claim_id = parse_id(args.id, "c")
        claim = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if not claim:
            fail(f"no claim c{claim_id}")
        if args.verdict != "pending" and not claim["shown"]:
            fail(f"read the evidence first: run `claim show c{claim_id}`, then set the verdict")
        quote = None
        if args.verdict in ("supported", "partial", "contradicted"):
            if not args.quote:
                fail(f"a {args.verdict} verdict needs --quote with the decisive words copied from a passage of c{claim_id}; run `claim show c{claim_id}` and read them")
            links = db.execute("SELECT source_id, passage, role FROM claim_links WHERE claim_id = ?", (claim_id,)).fetchall()
            matched: list[dict] = []
            for raw_quote in args.quote:
                text = normalized(raw_quote)
                if len(text) < MIN_QUOTE_CHARS:
                    fail(f"--quote {raw_quote!r} must be at least {MIN_QUOTE_CHARS} characters of the passage")
                hit, heading_only = None, False
                for link in links:
                    segments = normalized(load_passages(link["source_id"]).get(link["passage"], "")).split(" · ")
                    containing = [segment for segment in segments if text.lower() in segment.lower()]
                    if not containing and text.lower() in " · ".join(segments).lower():
                        containing = [" · ".join(segments)]
                    if containing and all(segment.startswith("## ") for segment in containing):
                        heading_only = True
                    elif containing:
                        hit = link
                        break
                if hit is None and heading_only:
                    fail(f"--quote {raw_quote!r} is only a heading; quote the sentence in which the source states the claim")
                if hit is None:
                    fail(f"--quote {raw_quote!r} is not in any passage linked to c{claim_id}; copy it exactly, or link the passage that says it with `claim link`")
                matched.append({"source": hit["source_id"], "passage": hit["passage"], "quote": text})
            if args.verdict != "contradicted":
                supporting = {link["source_id"] for link in links if link["role"] == "support"}
                missing = supporting - {item["source"] for item in matched}
                if missing:
                    fail(f"c{claim_id} cites {' '.join(f's{n}' for n in sorted(missing))} without a quote; give one --quote per supporting source, or remove that source's links if it does not carry the claim")
            quote = json.dumps(matched, ensure_ascii=False)
        elif not args.note:
            fail(f"a {args.verdict} verdict needs --note saying what the passages lack")
        db.execute(
            "UPDATE claims SET verdict = ?, note = COALESCE(?, note), statement = COALESCE(?, statement), quote = COALESCE(?, quote) WHERE id = ?",
            (args.verdict, args.note, args.restate, quote, claim_id),
        )
        db.commit()
        print(f"c{claim_id} {args.verdict}")
    elif args.action == "show":
        claim_id = parse_id(args.id, "c")
        row = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if not row:
            fail(f"no claim c{claim_id}")
        print(f"c{claim_id} [{row['verdict']}] {row['kind']} q{row['question_id'] or '-'} as_of={row['as_of'] or '-'}: {row['statement']}")
        if row["note"]:
            print(f"  note: {row['note']}")
        for link in db.execute("SELECT * FROM claim_links WHERE claim_id = ? ORDER BY role DESC, source_id, passage", (claim_id,)):
            passage = load_passages(link["source_id"]).get(link["passage"], "<missing>")
            print(f"  {link['role']} s{link['source_id']}:p{link['passage']} {passage[:1500]}")
        db.execute("UPDATE claims SET shown = 1 WHERE id = ?", (claim_id,))
        db.commit()


def conflicting_values(statement: str, support: list[tuple[int, int]]) -> list[str]:
    """Same-source passages near the claim, or naming its model, that give a different price."""
    amounts = set(re.findall(r"[$€£]\s?\d[\d,]*(?:\.\d+)?", statement))
    if not amounts:
        return []
    normalize_amount = lambda value: re.sub(r"[\s,]|\.0+$", "", value)
    claimed = {normalize_amount(value) for value in amounts}
    tokens = set(re.findall(r"\b[A-Z][A-Za-z]*-?\d{2,4}[A-Z]*\b", statement))
    hints = []
    for source_id in dict.fromkeys(source for source, _ in support):
        linked = {passage for source, passage in support if source == source_id}
        for number, passage in load_passages(source_id).items():
            if number in linked:
                continue
            passage_tokens = set(re.findall(r"\b[A-Z][A-Za-z]*-?\d{2,4}[A-Z]*\b", passage))
            near = any(abs(number - linked_number) <= NEARBY_PASSAGES for linked_number in linked) and not (passage_tokens - tokens)
            if not (tokens & passage_tokens or near):
                continue
            other = {normalize_amount(value) for value in re.findall(r"[$€£]\s?\d[\d,]*(?:\.\d+)?", passage)} - claimed
            if other:
                hints.append(f"check: s{source_id}:p{number} gives {', '.join(sorted(other))}; if it is the same item, record both values (--counter)")
    return hints[:3]


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')).strip(" .…")


def cmd_claims(args) -> None:
    db = connect()
    conditions, values = [], []
    if args.q:
        conditions.append("question_id = ?")
        values.append(parse_id(args.q, "q"))
    if args.verdict:
        conditions.append("verdict = ?")
        values.append(args.verdict)
    query = "SELECT * FROM claims" + (f" WHERE {' AND '.join(conditions)}" if conditions else "") + " ORDER BY question_id, id"
    for row in db.execute(query, values):
        links = db.execute("SELECT source_id, passage, role FROM claim_links WHERE claim_id = ?", (row["id"],)).fetchall()
        refs = " ".join(("!" if link["role"] == "counter" else "") + f"s{link['source_id']}:p{link['passage']}" for link in links)
        print(f"c{row['id']} [{row['verdict']}] {row['kind']} q{row['question_id'] or '-'}: {row['statement']}  <{refs}>")


# Hosts shared by many unrelated publishers; the full host name identifies the publisher there.
SHARED_HOSTING = ("blob.core.windows.net", "amazonaws.com", "storage.googleapis.com", "cloudfront.net", "hubspotusercontent-na1.net", "github.io", "pages.dev", "netlify.app", "vercel.app", "substack.com", "medium.com")


def publisher_key(row: sqlite3.Row) -> str:
    """Subdomains of one organization (ai-act-service-desk.ec.europa.eu, digital-strategy.ec.europa.eu) are one publisher."""
    if (row["final_url"] or row["url"]).startswith("file://"):
        return "file:" + (row["publisher"] if "publisher" in row.keys() and row["publisher"] else "workspace").lower()
    host = re.sub(r"^www\.", "", (urllib.parse.urlparse(row["final_url"] or row["url"]).hostname or row["url"]).lower())
    if host.endswith(SHARED_HOSTING):
        return host
    labels = host.split(".")
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "com", "org", "gov", "ac", "net", "edu", "ne", "or", "go") else 2
    return ".".join(labels[-keep:])


def verified_sources(db: sqlite3.Connection, question_id: int | None = None) -> list[sqlite3.Row]:
    condition = "" if question_id is None else " AND c.question_id = ?"
    return db.execute(
        "SELECT DISTINCT s.id, s.url, s.final_url, s.publisher, s.\"primary\" FROM claims c JOIN claim_links l ON l.claim_id = c.id AND l.role = 'support' JOIN sources s ON s.id = l.source_id WHERE c.verdict IN ('supported', 'partial')" + condition,
        () if question_id is None else (question_id,),
    ).fetchall()


def sufficiency_gaps(db: sqlite3.Connection) -> list[str]:
    name, bar = depth(db)
    gaps = []
    open_questions = db.execute("SELECT COUNT(*) FROM questions WHERE status != 'dropped'").fetchone()[0]
    if open_questions < bar["questions"]:
        gaps.append(f"depth {name} needs at least {bar['questions']} research question(s); the brief has {open_questions}")
    if bar["counter"]:
        counter = db.execute("SELECT * FROM questions WHERE counter = 1 AND status != 'dropped'").fetchall()
        if not counter:
            gaps.append(f"depth {name} needs a counter-evidence question (`q add ... --counter --priority 1`)")
        for question in counter:
            if not db.execute("SELECT 1 FROM searches WHERE question_id = ?", (question["id"],)).fetchone():
                gaps.append(f"counter-evidence question q{question['id']} has no search of its own")
    rounds = rounds_done(db)
    if rounds < bar["rounds"]:
        gaps.append(f"depth {name} needs {bar['rounds']} fetch rounds (a round is a fetch of new sources after reading earlier ones); {rounds} so far")
    cited = len({row["id"] for row in verified_sources(db)})
    if cited < bar["sources"]:
        gaps.append(f"depth {name} needs {bar['sources']} sources behind verified claims; {cited} so far")
    for question in db.execute("SELECT * FROM questions WHERE priority = 1 AND status != 'dropped' ORDER BY id"):
        label = f"q{question['id']}"
        if question["status"] != "answered":
            gaps.append(f"{label} is {question['status']}")
        if not db.execute("SELECT 1 FROM searches WHERE question_id = ?", (question["id"],)).fetchone():
            gaps.append(f"{label} has no search of its own (log one with `searched ... --q {label}`)")
        pending = [f"c{row['id']}" for row in db.execute("SELECT id FROM claims WHERE question_id = ? AND verdict = 'pending' AND kind NOT IN ('inference', 'opinion')", (question["id"],))]
        if pending:
            gaps.append(f"{label} has unverified claims: {' '.join(pending)}")
        supporting = verified_sources(db, question["id"])
        publishers = {publisher_key(row) for row in supporting}
        if len(publishers) < bar["publishers"]:
            gaps.append(f"{label} rests on {len(publishers)} publisher(s); depth {name} needs {bar['publishers']} independent ones")
        if bar["primary"] and not any(row["primary"] for row in supporting):
            gaps.append(f"{label} has no primary source behind a verified claim (mark one with `source sN --primary`)")
    return gaps


DEADLINE_MINUTES = 30
DIMINISHING_SEARCHES = 2


def stop_refusal(db: sqlite3.Connection, reason: str) -> str | None:
    """Each stop reason other than `sufficient` must be visible in the store, not only asserted."""
    if reason == "diminishing":
        name, bar = depth(db)
        if rounds_done(db) < bar["rounds"]:
            return f"depth {name} needs {bar['rounds']} fetch rounds before returns can diminish; {rounds_done(db)} so far"
        last_fetch = db.execute("SELECT MAX(fetched_at) FROM sources").fetchone()[0] or ""
        later = db.execute("SELECT COUNT(*) FROM searches WHERE at > ?", (last_fetch,)).fetchone()[0]
        if later < DIMINISHING_SEARCHES:
            return f"diminishing returns means a further search round found nothing worth reading; log at least {DIMINISHING_SEARCHES} searches for the open questions after the last fetch ({later} so far)"
        return None
    if reason == "access":
        if not db.execute("SELECT 1 FROM sources WHERE fetch_status NOT IN ('ok', 'snippet')").fetchone():
            return "no source was blocked or unreadable"
        return None
    if reason == "deadline":
        started = datetime.datetime.strptime(meta(db, "created"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        minutes = (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() / 60
        return None if minutes >= DEADLINE_MINUTES else f"the run is {minutes:.0f} minutes old; deadline applies after {DEADLINE_MINUTES}"
    return None


def cmd_stop(args) -> None:
    db = connect()
    if args.reason == "sufficient":
        gaps = sufficiency_gaps(db)
        if gaps:
            for gap in gaps:
                print(f"gap: {gap}")
            print("not sufficient: close these gaps, or stop with access, diminishing or deadline and report the answer as partial")
            sys.exit(1)
    else:
        refusal = stop_refusal(db, args.reason)
        if refusal:
            fail(f"cannot stop with {args.reason}: {refusal}; keep researching the open questions")
    set_meta(db, "stop_reason", args.reason)
    set_meta(db, "stop_note", args.note or "")
    set_meta(db, "stopped", now())
    db.commit()
    print(f"stopped: {args.reason}")


def cmd_status(args) -> None:
    db = connect()
    used = db.execute("SELECT COUNT(*) FROM searches").fetchone()[0]
    print(f"question: {meta(db, 'question')}")
    print(f"store: {RESEARCH_DIR}  brief: {'yes' if os.path.exists(os.path.join(RESEARCH_DIR, 'brief.md')) else 'MISSING'}")
    name, bar = depth(db)
    print(f"depth: {name}  fetch rounds: {rounds_done(db)}/{bar['rounds']}  searches: {used} (guide about {bar['searches']})")
    counts = dict(db.execute("SELECT fetch_status, COUNT(*) FROM sources GROUP BY fetch_status").fetchall())
    print("sources: " + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"))
    verdicts = dict(db.execute("SELECT verdict, COUNT(*) FROM claims GROUP BY verdict").fetchall())
    print("claims: " + (", ".join(f"{key}={value}" for key, value in sorted(verdicts.items())) or "none"))
    print("questions:")
    for row in db.execute("SELECT * FROM questions ORDER BY priority, id"):
        claims = db.execute("SELECT COUNT(*) FROM claims WHERE question_id = ?", (row["id"],)).fetchone()[0]
        print(f"  q{row['id']} [{row['status']}] p{row['priority']} claims={claims}: {row['text']}" + (f" ({row['note']})" if row["note"] else ""))
    blocked = db.execute("SELECT id, url, fetch_status FROM sources WHERE fetch_status NOT IN ('ok', 'snippet') ORDER BY id").fetchall()
    if blocked:
        print("unread sources: " + "; ".join(f"s{row['id']} {row['fetch_status']} {row['url']}" for row in blocked[:10]))
    gaps = sufficiency_gaps(db)
    print("sufficiency: " + ("met" if not gaps else "; ".join(gaps)))
    if meta(db, "stop_reason"):
        print(f"stopped: {meta(db, 'stop_reason')} {meta(db, 'stop_note') or ''}")


def today_strings() -> list[str]:
    """The run's own date is not a sourced fact, so it never needs a citation."""
    today = datetime.date.today()
    return [today.isoformat(), f"{today:%B} {today.day}, {today.year}", f"{today.day} {today:%B} {today.year}", f"{today:%B} {today.year}"]


def resolve_markers(db: sqlite3.Connection, draft: str) -> tuple[str, list[int], list[str], list[str]]:
    errors, warnings, order, cited_claims = [], [], [], []
    numbers: dict[int, int] = {}

    def number_for(source_id: int) -> int:
        if source_id not in numbers:
            numbers[source_id] = len(numbers) + 1
            order.append(source_id)
        return numbers[source_id]

    def replace(match: re.Match) -> str:
        source_ids: list[int] = []
        for token in re.split(r"\s*,\s*", match.group(2)):
            kind, value = token[0].lower(), int(token[1:])
            if kind == "c":
                claim = db.execute("SELECT verdict, kind FROM claims WHERE id = ?", (value,)).fetchone()
                if not claim:
                    errors.append(f"[c{value}] does not exist")
                    continue
                if claim["verdict"] == "pending" and claim["kind"] not in UNSOURCED_KINDS:
                    errors.append(f"[c{value}] is unverified; verify it with `claim verdict` before citing")
                elif claim["verdict"] in ("contradicted", "unsupported"):
                    errors.append(f"[c{value}] is {claim['verdict']}; correct or remove the statement")
                elif claim["verdict"] in ("partial", "insufficient"):
                    warnings.append(f"[c{value}] is {claim['verdict']}; make sure the wording carries that qualification")
                links = [row["source_id"] for row in db.execute("SELECT DISTINCT source_id FROM claim_links WHERE claim_id = ? AND role = 'support' ORDER BY source_id", (value,))]
                if not links and claim["kind"] not in UNSOURCED_KINDS:
                    errors.append(f"[c{value}] has no supporting passage")
                source_ids.extend(links)
                cited_claims.append(value)
            else:
                if not db.execute("SELECT 1 FROM sources WHERE id = ?", (value,)).fetchone():
                    errors.append(f"[s{value}] does not exist")
                    continue
                source_ids.append(value)
        unique = list(dict.fromkeys(source_ids))
        citations = "".join(f"[{number_for(source_id)}]" for source_id in unique)
        return match.group(1) + citations if citations else ""

    rendered = re.sub(r"([ \t]*)\[((?:[cs]\d+)(?:\s*,\s*[cs]\d+)*)\]", replace, draft, flags=re.I)
    for paragraph in re.split(r"\n\s*\n", draft):
        if paragraph.strip().startswith(("#", "```")):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+(?!\[)|(?<=\])\s+(?=[A-Z*_])|\n", paragraph):
            stripped = sentence.strip()
            markers = sum(len(group.split(",")) for group in re.findall(r"\[((?:[cs]\d+)(?:\s*,\s*[cs]\d+)*)\]", stripped, re.I))
            if markers > MAX_MARKERS_PER_SENTENCE:
                warnings.append(f"{markers} claim markers on one sentence; put each marker on the sentence that states its claim: {stripped[:100]}")
            for today in today_strings():
                stripped = stripped.replace(today, "")
            if re.search(r"\b(19|20)\d{2}\b|\d+(\.\d+)?\s?(%|percent|million|billion|USD|EUR|\$)", stripped) and not re.search(r"\[[cs]\d+", stripped, re.I):
                warnings.append(f"uncited figure or date; add the claim marker to this sentence, do not delete the fact: {stripped[:120]}")
    return rendered, order, errors, warnings, list(dict.fromkeys(cited_claims)), numbers


def stored_quotes(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


def evidence_lines(db: sqlite3.Connection, claim_ids: list[int], numbers: dict[int, int]) -> list[str]:
    lines = []
    for claim_id in claim_ids:
        claim = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        links = db.execute("SELECT DISTINCT source_id, passage FROM claim_links WHERE claim_id = ? AND role = 'support' ORDER BY source_id, passage", (claim_id,)).fetchall()
        if not links:
            continue
        quotes = stored_quotes(claim["quote"])
        if not quotes:
            quotes = [{"source": links[0]["source_id"], "quote": normalized(load_passages(links[0]["source_id"]).get(links[0]["passage"], ""))[:240]}]
        verdict = "" if claim["verdict"] == "supported" else f" ({claim['verdict']})"
        lines.append(f"- {claim['statement']}{verdict}")
        for item in quotes:
            source = db.execute("SELECT url, final_url FROM sources WHERE id = ?", (item["source"],)).fetchone()
            # Text fragments match page text, so skip the store's " · " joins; "-", "&" and "," are fragment syntax.
            segment = max(item["quote"].split(" · "), key=len)
            fragment = urllib.parse.quote(" ".join(segment.split()[:8]), safe="").replace("-", "%2D")
            number = f"[{numbers[item['source']]}] " if item["source"] in numbers else ""
            target = source["final_url"] or source["url"]
            quoted = f"\u201c{item['quote']}\u201d" if target.startswith("file://") else f"[\u201c{item['quote']}\u201d]({target}#:~:text={fragment})"
            lines.append(f"  > {number}{quoted}")
    return lines


def integrity_warnings(db: sqlite3.Connection, source_ids: list[int]) -> list[str]:
    warnings = []
    for source_id in source_ids:
        row = db.execute("SELECT content_sha FROM sources WHERE id = ?", (source_id,)).fetchone()
        if row and row["content_sha"] and content_hash(list(load_passages(source_id).values())) != row["content_sha"]:
            warnings.append(f"s{source_id} snapshot changed after it was stored; its passages may no longer match the claims that cite it")
    return warnings


def source_entry(row: sqlite3.Row) -> str:
    title = (row["title"] or row["url"]).replace("[", "(").replace("]", ")")
    local = (row["final_url"] or row["url"]).startswith("file://")
    filename = os.path.basename(row["url"])
    parts = [(f"{title} (provided document)" if title == filename else f"{title} (provided document: {filename})") if local else f"[{title}]({row['final_url'] or row['url']})"]
    if row["publisher"]:
        parts.append(row["publisher"])
    parts.append((row["published"] or "undated")[:10])
    if row["modified"] and row["modified"][:10] != (row["published"] or "")[:10]:
        parts.append(f"updated {row['modified'][:10]}")
    parts.append(f"accessed {row['fetched_at'][:10]}")
    entry = ", ".join(parts)
    if row["fetch_status"] == "snippet":
        entry += " (snippet only, page not read)"
    return entry


def cmd_render(args) -> None:
    db = connect()
    with open(args.draft, encoding="utf-8") as handle:
        draft = handle.read()
    rendered, order, errors, warnings, cited_claims, numbers = resolve_markers(db, draft)
    warnings += integrity_warnings(db, order)
    for warning in warnings:
        print(f"warning: {warning}")
    if errors:
        for error in errors:
            print(f"error: {error}")
        print("not rendered; fix the errors above")
        sys.exit(1)
    if args.check:
        print(f"ok: {len(order)} sources cited, {len(warnings)} warnings")
        return
    lines = [rendered.rstrip(), "", f"## {args.heading}", ""]
    for number, source_id in enumerate(order, start=1):
        lines.append(f"{number}. {source_entry(db.execute('SELECT * FROM sources WHERE id = ?', (source_id,)).fetchone())}")
    if cited_claims and "evidence.md" not in rendered:
        lines += ["", f"_{args.evidence_line}_"]
    if depth(db)[0] != "quick":
        lines += ["", f"_{EXPORT_OFFER}_"]
    with open(args.output, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    set_meta(db, "cited_claims", json.dumps(cited_claims))
    set_meta(db, "cited_sources", json.dumps(order))
    db.commit()
    evidence_path = os.path.join(os.path.dirname(args.output), "evidence.md")
    with open(evidence_path, "w", encoding="utf-8") as handle:
        handle.write("# Evidence\n\nThe passage behind each cited claim; a quote link opens the source at that text.\n\n" + "\n".join(evidence_lines(db, cited_claims, numbers)) + "\n")
    print(f"wrote {args.output}: {len(order)} sources, {len(warnings)} warnings; wrote {evidence_path}")
    print("reply with the content of report.md exactly as written; to change wording, edit the draft and render again")


STALE_DAYS = 365


def cmd_audit(args) -> None:
    """Scores the evidence behind the rendered report; it checks structure and quotes, not meaning."""
    db = connect()
    name, bar = depth(db)
    cited = json.loads(meta(db, "cited_claims", "[]") or "[]")
    if not cited:
        fail("no rendered report to audit; run `render` first")
    cited_sources = json.loads(meta(db, "cited_sources", "[]") or "[]")
    checks: list[tuple[str, bool, str]] = []

    fully_quoted, unverified = 0, []
    for claim_id in cited:
        claim = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if claim["kind"] in UNSOURCED_KINDS:
            fully_quoted += 1
            continue
        supporting = {row["source_id"] for row in db.execute("SELECT DISTINCT source_id FROM claim_links WHERE claim_id = ? AND role = 'support'", (claim_id,))}
        quoted = {item["source"] for item in stored_quotes(claim["quote"])}
        if claim["verdict"] in ("supported", "partial") and supporting <= quoted:
            fully_quoted += 1
        else:
            unverified.append(f"c{claim_id}")
    checks.append(("support", not unverified, f"{fully_quoted}/{len(cited)} cited claims verified with a quote from every supporting source" + (f"; not: {' '.join(unverified)}" if unverified else "")))

    draft_path = os.path.join(RESEARCH_DIR, "draft.md")
    uncited = []
    if os.path.exists(draft_path):
        with open(draft_path, encoding="utf-8") as handle:
            uncited = [warning for warning in resolve_markers(db, handle.read())[3] if warning.startswith("uncited")]
    checks.append(("citation completeness", not uncited, f"{len(uncited)} sentence(s) with figures or dates and no citation"))

    gaps = sufficiency_gaps(db)
    stop_reason = meta(db, "stop_reason")
    if gaps and stop_reason in ("diminishing", "access", "deadline"):
        # A checked partial stop is honest; the report must name these gaps instead.
        checks.append((f"depth {name} bar", True, f"partial (stopped: {stop_reason}); the report must name these gaps: " + "; ".join(gaps)))
    else:
        checks.append((f"depth {name} bar", not gaps, "met" if not gaps else "; ".join(gaps)))

    snippets = [f"s{row['id']}" for row in db.execute(f"SELECT id FROM sources WHERE fetch_status = 'snippet' AND id IN ({','.join('?' * len(cited_sources)) or 'NULL'})", cited_sources)]
    checks.append(("read sources", not snippets, "every cited source was read" if not snippets else f"cited from search snippets only: {' '.join(snippets)}"))

    stale = []
    for row in db.execute(f"SELECT id, published, modified FROM sources WHERE id IN ({','.join('?' * len(cited_sources)) or 'NULL'})", cited_sources):
        dated = (row["modified"] or row["published"] or "")[:10]
        try:
            age = (datetime.date.today() - datetime.date.fromisoformat(dated)).days
        except ValueError:
            continue
        if age > STALE_DAYS:
            stale.append(f"s{row['id']} ({dated})")
    checks.append(("freshness", True, f"{len(stale)} cited source(s) older than 12 months" + (f": {', '.join(stale)}; the report must say so where it matters" if stale else "")))

    conflicts = db.execute(f"SELECT COUNT(DISTINCT claim_id) FROM claim_links WHERE role = 'counter' AND claim_id IN ({','.join('?' * len(cited)) or 'NULL'})", cited).fetchone()[0]
    checks.append(("conflicts", True, f"{conflicts} cited claim(s) carry counter-evidence; the report must state each disagreement"))

    drift = integrity_warnings(db, cited_sources)
    checks.append(("snapshot integrity", not drift, "cited snapshots unchanged" if not drift else "; ".join(drift)))

    for label, passed, detail in checks:
        print(f"{'pass' if passed else 'FAIL'}  {label}: {detail}")
    failed = [label for label, passed, _ in checks if not passed]
    with open(os.path.join(RESEARCH_DIR, "audit.json"), "w", encoding="utf-8") as handle:
        json.dump({"depth": name, "checked": now(), "failed": failed, "checks": [{"check": label, "passed": passed, "detail": detail} for label, passed, detail in checks]}, handle, indent=2)
    print(f"audit: {len(failed)} failing check(s); written to {RESEARCH_DIR}/audit.json")


def workspace_root() -> str:
    return os.path.dirname(RESEARCH_DIR)


def markdown_to_html(text: str) -> str:
    return subprocess.run(["pandoc", "-f", "gfm", "-t", "html5", "--wrap=none"], input=text, capture_output=True, text=True, check=True).stdout


def split_report(markdown: str) -> tuple[str, str, str]:
    """Returns the title, the body and the source list of a rendered report."""
    kept = [line for line in markdown.splitlines() if EXPORT_OFFER not in line and EVIDENCE_LINE_PREFIX not in line]
    text = "\n".join(kept).strip() + "\n"
    title = ""
    heading = re.match(r"#\s+(.+)\n", text)
    if heading:
        title, text = heading.group(1).strip(), text[heading.end():]
    lists = list(re.finditer(r"^## [^\n]+\n+(?=1\. )", text, re.M))
    if not lists:
        return title, text, ""
    return title, text[: lists[-1].start()], text[lists[-1].start():]


def build_page(db: sqlite3.Connection, with_downloads: bool) -> str:
    report_path = os.path.join(workspace_root(), "report.md")
    if not os.path.exists(report_path):
        fail("no rendered report; run `render` first")
    with open(report_path, encoding="utf-8") as handle:
        markdown = handle.read()
    title, body, sources = split_report(markdown)
    title = title or meta(db, "question") or "Research report"
    body_html = re.sub(r"\[(\d+)\]", r'<a class="cite" href="#src-\1">[\1]</a>', markdown_to_html(body))
    counter = iter(range(1, 10_000))
    sources_html = re.sub(r"<li>", lambda _: f'<li id="src-{next(counter)}">', markdown_to_html(sources))
    evidence_path = os.path.join(workspace_root(), "evidence.md")
    evidence_html = ""
    if os.path.exists(evidence_path):
        with open(evidence_path, encoding="utf-8") as handle:
            evidence = re.sub(r"\A# Evidence\s*\n(?:[^\n]*\n)?", "", handle.read())
        evidence_html = markdown_to_html(evidence)
    source_count = len(re.findall(r"^\d+\. ", sources, re.M))
    confidence = re.search(r"confidence[^a-zA-Z]{0,6}(high|medium-high|medium|low)", markdown, re.I) or re.search(r"(high|medium-high|medium|low) confidence", markdown, re.I)
    facts = [f"<span><b>{html.escape(datetime.date.today().isoformat())}</b></span>", f"<span>Depth <b>{depth(db)[0]}</b></span>", f"<span><b>{source_count}</b> sources</span>"]
    if confidence:
        facts.append(f"<span>Confidence <b>{html.escape(confidence.group(1).lower())}</b></span>")
    downloads = (
        '<nav class="downloads" aria-label="Downloads"><a href="report.pdf" download>PDF</a><a href="report.md" download>Markdown</a>'
        '<a href="report.html" download>HTML</a><a href="evidence.md" download>Evidence</a></nav>'
        if with_downloads else ""
    )
    with open(TEMPLATE_PATH, encoding="utf-8") as handle:
        page = handle.read()
    values = {
        "title": html.escape(title), "meta": "".join(facts), "downloads": downloads, "body": body_html,
        "sources": sources_html, "evidence": evidence_html, "evidence_open": "" if with_downloads else "open",
        "generated": html.escape(now()),
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda match: values[match.group(1)], page)


def print_pdf(html_path: str, pdf_path: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"file://{html_path}", wait_until="load")
        page.emulate_media(media="print")
        page.pdf(
            path=pdf_path, format="A4", print_background=True, display_header_footer=True,
            margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"},
            header_template="<div></div>",
            footer_template='<div style="font-size:8px;width:100%;text-align:center;color:#777"><span class="pageNumber"></span> / <span class="totalPages"></span></div>',
        )
        browser.close()


def write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def cmd_export(args) -> None:
    db = connect()
    root = workspace_root()
    report_md, evidence_md = os.path.join(root, "report.md"), os.path.join(root, "evidence.md")
    if args.format == "md":
        if not os.path.exists(report_md):
            fail("no rendered report; run `render` first")
        print(f"attach {report_md} (text/markdown)" + (f" and {evidence_md} (text/markdown)" if os.path.exists(evidence_md) else ""))
        return
    standalone = build_page(db, with_downloads=False)
    report_html = os.path.join(root, "report.html")
    write_text(report_html, standalone)
    if args.format == "html":
        print(f"attach {report_html} (text/html)")
        return
    report_pdf = os.path.join(root, "report.pdf")
    print_pdf(report_html, report_pdf)
    if args.format == "pdf":
        print(f"attach {report_pdf} (application/pdf)")
        return
    title = split_report(open(report_md, encoding="utf-8").read())[0] or meta(db, "question") or "research"
    name = args.name or (re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-") + "-" + secrets.token_hex(2))
    if not SITE_NAME.fullmatch(name) or name == "login":
        fail(f"site name {name!r} must be 1-63 lowercase letters, digits and hyphens, starting and ending with a letter or digit")
    site_dir = os.path.join(root, "sites", name)
    if os.path.isdir(site_dir) and os.listdir(site_dir) and not args.replace:
        fail(f"{site_dir} already has content; pass --replace to rebuild it, or choose another --name")
    os.makedirs(site_dir, exist_ok=True)
    write_text(os.path.join(site_dir, "index.html"), build_page(db, with_downloads=True))
    shutil.copyfile(report_html, os.path.join(site_dir, "report.html"))
    shutil.copyfile(report_pdf, os.path.join(site_dir, "report.pdf"))
    shutil.copyfile(report_md, os.path.join(site_dir, "report.md"))
    if os.path.exists(evidence_md):
        shutil.copyfile(evidence_md, os.path.join(site_dir, "evidence.md"))
    write_text(os.path.join(site_dir, "_spot.json"), json.dumps({"title": title, "description": meta(db, "question") or title, "tags": ["research"]}, ensure_ascii=False))
    print(f"site ready in {site_dir}: index.html, report.pdf, report.md, report.html, evidence.md")
    print(f"publish with publish_site site={name!r} path={site_dir!r}; keep it private unless the user asked to share it")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    command = commands.add_parser("init", help="create the research store")
    command.add_argument("question")
    command.add_argument("--depth", choices=tuple(DEPTHS), help="quick, standard (default) or deep; on an existing store, changes its depth")
    command.add_argument("--fresh", action="store_true", help="archive an existing store and start over")
    command.set_defaults(handler=cmd_init)

    command = commands.add_parser("q", help="manage research questions")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("add")
    action.add_argument("text")
    action.add_argument("--needs", help="evidence that would settle this question")
    action.add_argument("--priority", type=int, choices=(1, 2, 3), default=2)
    action.add_argument("--counter", action="store_true", help="the question looks for counter-evidence")
    action = actions.add_parser("set")
    action.add_argument("id")
    action.add_argument("status", choices=QUESTION_STATUSES)
    action.add_argument("--note")
    actions.add_parser("list")
    command.set_defaults(handler=cmd_q)

    command = commands.add_parser("searched", help="log web_search calls, one query per call")
    command.add_argument("queries", nargs="+", help="the exact query text of each call")
    command.add_argument("--q")
    command.add_argument("--results", type=int)
    command.set_defaults(handler=cmd_searched)

    command = commands.add_parser("fetch", help="fetch pages concurrently and store numbered passages")
    command.add_argument("urls", nargs="+")
    command.add_argument("--q", help="question the sources serve")
    command.add_argument("--why", help="why these sources were chosen")
    command.add_argument("--browser", action="store_true", help="render with headless Chromium (slower; for thin or script-built pages)")
    command.add_argument("--refresh", action="store_true", help="fetch again even if already stored")
    command.add_argument("--preview", action="store_true", help=f"include the first {PREVIEW_CHARS} characters")
    command.set_defaults(handler=cmd_fetch)

    command = commands.add_parser("snippet", help="record a search excerpt for a page that could not be read")
    command.add_argument("url")
    command.add_argument("--excerpt", required=True)
    command.add_argument("--reason", required=True, help="why the page itself was not read")
    command.add_argument("--title")
    command.add_argument("--publisher")
    command.add_argument("--published")
    command.add_argument("--q")
    command.set_defaults(handler=cmd_snippet)

    command = commands.add_parser("source", help="show or correct one source's metadata")
    command.add_argument("id")
    command.add_argument("--title")
    command.add_argument("--publisher")
    command.add_argument("--published")
    command.add_argument("--note")
    command.add_argument("--primary", action="store_true", help="the source is original evidence for what it states (law, dataset, filing, official spec, the tester's own test)")
    command.set_defaults(handler=cmd_source)

    command = commands.add_parser("sources", help="list sources")
    command.add_argument("--status")
    command.set_defaults(handler=cmd_sources)

    command = commands.add_parser("find", help="regex search in one source (sN) or all")
    command.add_argument("source")
    command.add_argument("pattern")
    command.add_argument("--max", type=int, default=20)
    command.set_defaults(handler=cmd_find)

    command = commands.add_parser("read", help="print passages, e.g. s3:p12 s4:p10-14")
    command.add_argument("refs", nargs="+")
    command.add_argument("--context", type=int, default=0, help="extra passages before and after")
    command.set_defaults(handler=cmd_read)

    command = commands.add_parser("claim", help="record and verify claims")
    actions = command.add_subparsers(dest="action", required=True)
    action = actions.add_parser("add")
    action.add_argument("statement")
    action.add_argument("--support", nargs="+", help="passages that support the whole claim")
    action.add_argument("--counter", nargs="+", help="passages that contradict or qualify it")
    action.add_argument("--kind", type=str.lower, default="fact", help="fact, number, date, comparison, ...; inference and opinion need no support")
    action.add_argument("--as-of", help="date or version the claim holds for")
    action.add_argument("--q")
    action = actions.add_parser("link")
    action.add_argument("id")
    action.add_argument("--support", nargs="+")
    action.add_argument("--counter", nargs="+")
    action = actions.add_parser("verdict")
    action.add_argument("id")
    action.add_argument("verdict", choices=VERDICTS)
    action.add_argument("--quote", nargs="+", help="decisive words copied exactly from linked passages, one per supporting source (required for supported, partial, contradicted)")
    action.add_argument("--note", help="the mismatch, if any (required for unsupported, insufficient)")
    action.add_argument("--restate", help="corrected statement the evidence does support")
    action = actions.add_parser("show")
    action.add_argument("id")
    command.set_defaults(handler=cmd_claim)

    command = commands.add_parser("claims", help="list claims")
    command.add_argument("--q")
    command.add_argument("--verdict", choices=VERDICTS)
    command.set_defaults(handler=cmd_claims)

    command = commands.add_parser("stop", help="record why research stopped")
    command.add_argument("reason", choices=STOP_REASONS)
    command.add_argument("--note")
    command.set_defaults(handler=cmd_stop)

    command = commands.add_parser("add-file", help="add workspace files (Chat attachments, Knowledge downloads, documents) as sources")
    command.add_argument("paths", nargs="+")
    command.add_argument("--publisher", required=True, help="who authored the document, for example the user's company or team")
    command.add_argument("--title")
    command.add_argument("--published", help="document date, if known")
    command.add_argument("--q")
    command.add_argument("--why")
    command.add_argument("--primary", action="store_true", help="the document is original evidence for what it states")
    command.set_defaults(handler=cmd_add_file)

    command = commands.add_parser("audit", help="score the evidence behind the rendered report")
    command.set_defaults(handler=cmd_audit)

    command = commands.add_parser("export", help="turn the rendered report into a Site directory, a PDF, HTML or Markdown files")
    command.add_argument("format", choices=("site", "pdf", "html", "md"))
    command.add_argument("--name", help="site name (default: from the title plus a short suffix)")
    command.add_argument("--replace", action="store_true", help="rebuild an existing site directory")
    command.set_defaults(handler=cmd_export)

    command = commands.add_parser("status", help="compact state; read it after a long pause or compaction")
    command.set_defaults(handler=cmd_status)

    command = commands.add_parser("render", help="resolve [cN]/[sN] markers into numbered citations and a source list")
    command.add_argument("draft")
    command.add_argument("-o", "--output", default=os.path.join(os.path.dirname(RESEARCH_DIR), "report.md"))
    command.add_argument("--heading", default="Sources")
    command.add_argument("--evidence-line", default=f"{EVIDENCE_LINE_PREFIX}; ask for it if you want to check a claim.")
    command.add_argument("--check", action="store_true", help="validate only")
    command.set_defaults(handler=cmd_render)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
