from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_ID = "readlongtime-wealth-reader"
DEFAULT_DB = ROOT / "storage" / "personal.sqlite3"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def show_error(message):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "国富论阅读器", 0x10)
    else:
        print(message, file=sys.stderr)


def find_service(database):
    marker = database.parent / "reader-instances.jsonl"
    try:
        entries = marker.read_text(encoding="utf-8").splitlines()[-300:]
    except (OSError, UnicodeError):
        return None
    seen = set()
    for line in reversed(entries):
        try:
            item = json.loads(line)
            port = item["port"]
            if item["application"] != APP_ID or type(port) is not int or not 1 <= port <= 65535 or Path(item["database"]).resolve() != database or port in seen:
                continue
            seen.add(port)
            url = "http://127.0.0.1:%d/" % port
            with OPENER.open(url + "api/health", timeout=0.3) as response:
                data = json.loads(response.read(4096))
            if data.get("application") == APP_ID and data.get("version") == 2 and Path(data.get("database", "")).resolve() == database:
                return url
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None


def launch_server(database):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen([sys.executable, "-u", str(ROOT / "reader_server.py"), "--background", "--database", str(database)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=flags)
    lines = []
    def read_line():
        lines.append(process.stdout.readline())
    thread = threading.Thread(target=read_line, daemon=True)
    thread.start()
    thread.join(10)
    if not lines:
        raise RuntimeError("本地服务启动超时；请检查 Python 和文件权限。")
    url = lines[0].strip()
    if not url.startswith("http://127.0.0.1:") or not url.endswith("/"):
        raise RuntimeError("本地服务启动失败；请检查数据库及文件权限。")
    verified = find_service(database)
    if verified != url:
        raise RuntimeError("无法验证新服务身份，未连接。")
    return url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    args = parser.parse_args()
    database = args.database.resolve()
    try:
        url = find_service(database) or launch_server(database)
        if args.no_browser:
            print(url, flush=True)
        else:
            if not webbrowser.open(url):
                raise RuntimeError("无法打开浏览器，请手动访问 " + url)
    except Exception as exc:
        show_error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
