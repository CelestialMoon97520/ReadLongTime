from __future__ import annotations
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
READER = ROOT / "国富论阅读器.html"
INDEX = ROOT / "index.html"
DEFAULT_DB = ROOT / "storage" / "personal.sqlite3"
APP_ID = "readlongtime-wealth-reader"
SQLITE_APP_ID = 0x524C5431
HOST = "127.0.0.1"
PREFERRED_PORT = 18765
MAX_BODY = 4096


def init_database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        created = True
    except FileExistsError:
        pass
    if created:
        with sqlite3.connect(str(path), timeout=5) as db:
            db.execute("PRAGMA application_id = %d" % SQLITE_APP_ID)
            db.execute("PRAGMA user_version = 1")
            db.execute("CREATE TABLE progress (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, state_json TEXT NOT NULL)")
            db.execute("CREATE TABLE progress_backups (id INTEGER PRIMARY KEY, saved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, revision INTEGER NOT NULL, state_json TEXT NOT NULL)")
            db.commit()
    else:
        # Inspect an existing database read-only before ever opening it for writes.
        uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as db:
            app = db.execute("PRAGMA application_id").fetchone()[0]
            version = db.execute("PRAGMA user_version").fetchone()[0]
            quick = db.execute("PRAGMA quick_check").fetchone()[0]
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if app != SQLITE_APP_ID or version != 1 or quick != "ok" or not {"progress", "progress_backups"}.issubset(names):
                raise RuntimeError("Existing database is not a supported reader database; unchanged: " + str(path))


def connect(path):
    db = sqlite3.connect(str(path), timeout=5)
    db.execute("PRAGMA busy_timeout = 5000")
    return db


def read_state(db):
    row = db.execute("SELECT revision, state_json FROM progress WHERE id=1").fetchone()
    if row is None:
        return {"revision": 0, "state": None}
    return {"revision": row[0], "state": json.loads(row[1])}


def valid_payload(body, book_length):
    if not isinstance(body, dict) or set(body) != {"revision", "state", "backup"}:
        return False
    revision, state, backup = body["revision"], body["state"], body["backup"]
    if type(revision) is not int or revision < 0 or type(backup) is not bool or not isinstance(state, dict) or set(state) != {"start", "font", "dark"}:
        return False
    return (type(state["start"]) is int and 0 <= state["start"] < book_length
            and type(state["font"]) is int and 16 <= state["font"] <= 30
            and type(state["dark"]) is bool)


class BrowserSessions:
    """Track live page streams under one lock, including the shutdown transition."""
    def __init__(self, close_grace=3.0, startup_timeout=90.0, clock=time.monotonic):
        self.close_grace = close_grace
        self.startup_timeout = startup_timeout
        self.clock = clock
        self.started = clock()
        self.idle_since = None
        self.seen_page = False
        self.connections = set()
        self.connection_pages = {}
        self.recent_pages = {}
        self.page_grace = 10.0
        self.closing = False
        self.lock = threading.Lock()

    def connect(self, page_id=None):
        with self.lock:
            if self.closing:
                raise RuntimeError("service is stopping")
            key = uuid.uuid4().hex
            self.connections.add(key)
            if page_id is not None:
                self.connection_pages[key] = page_id
                self.recent_pages.pop(page_id, None)
            self.seen_page = True
            self.idle_since = None
            return key

    def disconnect(self, key):
        with self.lock:
            if key not in self.connections:
                return
            self.connections.remove(key)
            page_id = self.connection_pages.pop(key, None)
            if page_id is not None and page_id not in self.connection_pages.values():
                self.recent_pages[page_id] = self.clock() + self.page_grace
            if not self.connections and self.idle_since is None:
                self.idle_since = self.clock()

    def active_pages(self):
        with self.lock:
            now = self.clock()
            self.recent_pages = {page: until for page, until in self.recent_pages.items() if until >= now}
            return sorted(set(self.connection_pages.values()) | set(self.recent_pages))

    def begin_shutdown_if_idle(self):
        with self.lock:
            if self.closing or self.connections:
                return False
            expired = self.seen_page and self.idle_since is not None and self.clock() - self.idle_since >= self.close_grace
            never_opened = not self.seen_page and self.clock() - self.started >= self.startup_timeout
            if expired or never_opened:
                self.closing = True
                return True
            return False

    def mark_closing(self):
        with self.lock:
            self.closing = True

    def is_closing(self):
        with self.lock:
            return self.closing


def watch_browser_lifetime(server):
    while not server.exit_event.wait(0.25):
        if server.client_sessions.begin_shutdown_if_idle():
            server.exit_event.set()
            server.shutdown()
            return


class ReaderServer(ThreadingHTTPServer):
    allow_reuse_address = False
    # server_close waits for request threads, including in-flight SQLite commits.
    daemon_threads = False

    def __init__(self, address, database, book_length):
        self.database = database
        self.book_length = book_length
        self.client_sessions = BrowserSessions()
        self.exit_event = threading.Event()
        super().__init__(address, ReaderHandler)


class ReaderHandler(BaseHTTPRequestHandler):
    server_version = "LocalReader/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def host_ok(self):
        return self.headers.get("Host", "") == "%s:%d" % self.server.server_address

    def mutation_ok(self):
        origin = "http://%s:%d" % self.server.server_address
        return (self.host_ok() and self.headers.get("Origin") == origin
                and self.headers.get("X-Reader-Request") == "1"
                and self.headers.get_content_type() == "application/json")

    def reply(self, status, data, content_type="application/json; charset=utf-8", head=False):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def route_get(self, head=False):
        if not self.host_ok():
            return self.reply(403, {"error": "invalid host"}, head=head)
        path = urlsplit(self.path).path
        if path in ("/", "/index.html", "/reader"):
            file = READER if path == "/reader" else INDEX
            try:
                return self.reply(200, file.read_bytes(), "text/html; charset=utf-8", head)
            except OSError:
                return self.reply(503, {"error": "page unavailable"}, head=head)
        if path == "/api/health":
            if self.server.client_sessions.is_closing():
                return self.reply(503, {"error": "service stopping"}, head=head)
            return self.reply(200, {"application": APP_ID, "database": str(self.server.database), "version": 2}, head=head)
        if path in ("/api/presence", "/api/presence/active"):
            origin = "http://%s:%d" % self.server.server_address
            if (self.headers.get("X-Reader-Request") != "1"
                    or self.headers.get("Origin", origin) != origin
                    or self.headers.get("Sec-Fetch-Site", "none") not in ("same-origin", "none")):
                return self.reply(403, {"error": "request denied"}, head=head)
            if path == "/api/presence/active":
                return self.reply(200, {"pages": self.server.client_sessions.active_pages()}, head=head)
            if head:
                return self.reply(405, {"error": "GET required"}, head=True)
            page_id = self.headers.get("X-Reader-Page", "")
            if re.fullmatch(r"[0-9a-f]{32}", page_id) is None:
                return self.reply(400, {"error": "invalid page id"})
            return self.page_presence(page_id)
        if path == "/api/progress":
            try:
                with connect(self.server.database) as db:
                    result = read_state(db)
                return self.reply(200, result, head=head)
            except (sqlite3.Error, ValueError):
                return self.reply(503, {"error": "database unavailable"}, head=head)
        return self.reply(404, {"error": "not found"}, head=head)

    def page_presence(self, page_id):
        try:
            key = self.server.client_sessions.connect(page_id)
        except RuntimeError:
            return self.reply(503, {"error": "service stopping"})
        try:
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while not self.server.exit_event.is_set():
                self.wfile.write(b": page-open\n\n")
                self.wfile.flush()
                if self.server.exit_event.wait(1):
                    break
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, OSError):
            pass
        finally:
            self.server.client_sessions.disconnect(key)

    def do_GET(self):
        self.route_get()

    def do_HEAD(self):
        self.route_get(True)

    def parse_json(self):
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("size")
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError):
            return None

    def do_PUT(self):
        if urlsplit(self.path).path != "/api/progress":
            return self.reply(404, {"error": "not found"})
        if not self.mutation_ok():
            return self.reply(403, {"error": "request denied"})
        body = self.parse_json()
        if not valid_payload(body, self.server.book_length):
            return self.reply(400, {"error": "invalid progress"})
        try:
            with connect(self.server.database) as db:
                db.execute("BEGIN IMMEDIATE")
                current = read_state(db)
                if body["revision"] != current["revision"]:
                    db.rollback()
                    return self.reply(409, current)
                if body["backup"] and current["state"] is not None:
                    db.execute("INSERT INTO progress_backups(revision,state_json) VALUES (?,?)", (current["revision"], json.dumps(current["state"], ensure_ascii=False)))
                revision = current["revision"] + 1
                encoded = json.dumps(body["state"], ensure_ascii=False, separators=(",", ":"))
                db.execute("INSERT INTO progress(id,revision,state_json) VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,state_json=excluded.state_json", (revision, encoded))
                db.commit()
            return self.reply(200, {"revision": revision, "state": body["state"]})
        except (sqlite3.Error, ValueError):
            return self.reply(503, {"error": "database unavailable"})

    def do_POST(self):
        if urlsplit(self.path).path != "/api/shutdown":
            return self.reply(404, {"error": "not found"})
        if not self.mutation_ok():
            return self.reply(403, {"error": "request denied"})
        if self.parse_json() != {}:
            return self.reply(400, {"error": "invalid request"})
        self.server.client_sessions.mark_closing()
        self.server.exit_event.set()
        try:
            self.reply(200, {"stopping": True})
        finally:
            # A client may disconnect before receiving the reply; still stop the service.
            threading.Thread(target=self.server.shutdown, daemon=True).start()


def create_server(database, book_length):
    for port in list(range(PREFERRED_PORT, PREFERRED_PORT + 20)) + [0]:
        try:
            return ReaderServer((HOST, port), database, book_length)
        except OSError:
            if port == 0:
                raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    database = args.database.resolve()
    if not READER.is_file() or not INDEX.is_file():
        print("Reader or index file missing.", flush=True)
        return 1
    try:
        html = READER.read_text(encoding="utf-8")
        raw_book = html.split('<script id="book-data" type="application/json">', 1)[1].split("</script>", 1)[0]
        book = json.loads(raw_book)
        if not isinstance(book, str) or not book:
            raise RuntimeError("Reader book content is invalid")
        book_length = len(book.encode("utf-16-le", errors="surrogatepass")) // 2
        init_database(database)
        server = create_server(database, book_length)
        port = server.server_address[1]
        marker = database.parent / "reader-instances.jsonl"
        with marker.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"application": APP_ID, "port": port, "database": str(database)}, ensure_ascii=False) + "\n")
        print("http://%s:%d/" % (HOST, port), flush=True)
        if not args.background:
            print("本地服务运行中。按 Enter 或 Ctrl+C 关闭。", flush=True)
            def wait_for_enter():
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
                server.client_sessions.mark_closing()
                server.exit_event.set()
                server.shutdown()
            threading.Thread(target=wait_for_enter, daemon=True).start()
        threading.Thread(target=watch_browser_lifetime, args=(server,), daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            pass
        finally:
            server.client_sessions.mark_closing()
            server.exit_event.set()
            server.server_close()
        return 0
    except (OSError, RuntimeError, sqlite3.Error, ValueError, IndexError, UnicodeError) as exc:
        print("Reader service failed: %s" % exc, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
