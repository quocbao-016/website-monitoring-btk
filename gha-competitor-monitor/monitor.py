import hashlib, json, os, re, time
from urllib.parse import urlparse, urljoin
from dataclasses import dataclass
import requests
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup
import yaml
from datetime import datetime
import csv
from pathlib import Path

STATE_FILE = "state.json"
CONFIG_FILE = "sites.yml"

# Secrets / env
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")            # Slack Incoming Webhook (bắt buộc)
# Google Sheets (tùy chọn; nếu không set sẽ bỏ qua)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_KEY = os.getenv("SHEET_KEY")

DEFAULT_HEADERS = {
    "User-Agent": "CompetitorWatcher/1.0 (+https://github.com/your-repo)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

@dataclass
class Limits:
    max_urls_per_site: int = 1500
    max_total_urls: int = 3000
    request_timeout_sec: int = 20
    request_retries: int = 2
    polite_sleep_ms: int = 150

def load_config():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    sites = cfg.get("sites", [])
    change_threshold = int(cfg.get("change_threshold", 2000))
    limits_cfg = cfg.get("limits", {})
    limits = Limits(
        max_urls_per_site=int(limits_cfg.get("max_urls_per_site", 1500)),
        max_total_urls=int(limits_cfg.get("max_total_urls", 3000)),
        request_timeout_sec=int(limits_cfg.get("request_timeout_sec", 20)),
        request_retries=int(limits_cfg.get("request_retries", 2)),
        polite_sleep_ms=int(limits_cfg.get("polite_sleep_ms", 150)),
    )
    options = cfg.get("options", {})
    return sites, change_threshold, limits, options

def backoff_sleep(attempt):
    # exponential backoff nhỏ: 0.5s, 1s, 2s, tối đa 4s
    time.sleep(min(2 ** attempt * 0.5, 4.0))

def fetch(url, headers=None, timeout=20, retries=2):
    headers = headers or DEFAULT_HEADERS
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception as e:
            last_err = e
            if attempt < retries:
                backoff_sleep(attempt)
            else:
                raise last_err

def try_urls(urls, timeout, retries):
    for u in urls:
        try:
            r = fetch(u, timeout=timeout, retries=retries)
            return u, r
        except Exception:
            continue
    return None, None

def robots_sitemaps(base):
    # đọc robots.txt để tìm dòng Sitemap:
    try:
        robots = urljoin(base, "/robots.txt")
        r = fetch(robots, timeout=10, retries=1)
        sitemaps = []
        for line in r.text.splitlines():
            if line.lower().startswith("sitemap:"):
                sitemaps.append(line.split(":", 1)[1].strip())
        return sitemaps
    except Exception:
        return []

def parse_sitemap_collect(url, timeout, retries, limits):
    # Đệ quy: sitemap index -> sitemap -> urlset
    collected = set()
    seen = set()
    stack = [url]
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

    while stack and len(collected) < limits.max_urls_per_site:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        try:
            r = fetch(cur, timeout=timeout, retries=retries)
            root = ET.fromstring(r.text)
        except Exception:
            continue

        # sitemapindex
        for loc in root.findall(".//sm:sitemap/sm:loc", ns):
            loc_url = loc.text.strip()
            stack.append(loc_url)

        # urlset
        for loc in root.findall(".//sm:url/sm:loc", ns):
            loc_url = loc.text.strip()
            collected.add(loc_url)
            if len(collected) >= limits.max_urls_per_site:
                break
    return collected

def discover_sitemaps(base_url, timeout, retries):
    base = base_url.rstrip("/")
    candidates = [
        urljoin(base, "/sitemap.xml"),
        urljoin(base, "/sitemap_index.xml"),
    ]
    robots_list = robots_sitemaps(base)
    candidates = robots_list + candidates
    chosen, resp = try_urls(candidates, timeout=timeout, retries=retries)
    return chosen

def discover_rss_feeds(base_url, timeout, retries):
    feeds = []
    base = base_url.rstrip("/")
    # common paths
    common = ["/feed", "/rss", "/rss.xml", "/atom.xml"]
    for path in common:
        u = urljoin(base, path)
        try:
            r = fetch(u, timeout=timeout, retries=retries)
            ct = (r.headers.get("content-type") or "").lower()
            if "xml" in ct or "rss" in ct or "atom" in ct or r.text.strip().startswith("<?xml"):
                feeds.append(u)
        except Exception:
            pass

    # parse homepage <link rel="alternate" type="application/rss+xml">
    try:
        r = fetch(base, timeout=timeout, retries=retries)
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("link", attrs={"rel": lambda x: x and "alternate" in x}):
            t = (link.get("type") or "").lower()
            if "rss" in t or "atom" in t or "xml" in t:
                href = link.get("href")
                if href:
                    feeds.append(urljoin(base, href))
    except Exception:
        pass

    # unique
    dedup = []
    for f in feeds:
        if f not in dedup:
            dedup.append(f)
    return dedup

def parse_rss_items(feed_url, timeout, retries, limits):
    try:
        r = fetch(feed_url, timeout=timeout, retries=retries)
        root = ET.fromstring(r.text)
        urls = set()
        # RSS 2.0
        for item in root.findall(".//item/link"):
            if item.text:
                urls.add(item.text.strip())
        # Atom
        for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry/{http://www.w3.org/2005/Atom}link"):
            href = entry.get("href")
            if href:
                urls.add(href.strip())
        return set(list(urls)[:limits.max_urls_per_site])
    except Exception:
        return set()

def normalize_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text

def content_fingerprint(url, timeout, retries, polite_ms):
    r = fetch(url, timeout=timeout, retries=retries)
    text = normalize_text(r.text)
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    length = len(text)
    # lịch sự một chút (ms -> sec)
    time.sleep(max(0.0, polite_ms / 1000.0))
    return h, length

def should_include(url, include_paths, exclude_paths):
    path = urlparse(url).path or "/"
    if include_paths:
        ok = any(path.startswith(p) for p in include_paths)
        if not ok:
            return False
    if exclude_paths:
        if any(path.startswith(p) for p in exclude_paths):
            return False
    return True

def post_to_slack(webhook, text):
    if not webhook:
        print("⚠️ SLACK_WEBHOOK_URL is not set; skipping Slack notify.")
        return
    payload = {"text": text}
    try:
        requests.post(webhook, json=payload, timeout=10)
    except Exception as e:
        print("Slack error:", e)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"sites": {}}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def clamp_urls(urls, limits, remaining_total_budget):
    # cắt theo limit mỗi site và ngân sách tổng
    take = min(limits.max_urls_per_site, remaining_total_budget)
    return set(list(urls)[:take])

# ---------------- Google Sheets logging (rolling theo tháng) ---------------- #

def append_to_google_sheets(summary_rows):
    """
    Ghi log vào Google Sheets theo dạng rolling theo tháng:
    - Tên worksheet: summary_YYYY_MM (vd: summary_2025_10)
    - Tự tạo sheet nếu chưa có, tự thêm header.
    - Append batch 1 lần để tiết kiệm quota.
    """
    if not GOOGLE_SERVICE_ACCOUNT_JSON or not SHEET_KEY:
        return  # Không bật ghi Google Sheets

    try:
        import json as pyjson
        import gspread
        from google.oauth2.service_account import Credentials
        from datetime import datetime

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        info = pyjson.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(SHEET_KEY)

        ym = datetime.utcnow().strftime("%Y_%m")
        ws_title = f"summary_{ym}"

        try:
            ws = sh.worksheet(ws_title)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=ws_title, rows=1000, cols=5)
            ws.append_row(["run_ts", "site", "new_count", "changed_count", "gone_count"])

        # Phòng ngừa tab mới chưa có header
        try:
            if ws.acell("A1").value is None:
                ws.append_row(["run_ts", "site", "new_count", "changed_count", "gone_count"])
        except Exception:
            # một số trường hợp quota/permission có thể ném lỗi acell, bỏ qua
            pass

        if summary_rows:
            ws.append_rows(summary_rows, value_input_option="USER_ENTERED")

    except Exception as e:
        print("Google Sheets append error:", e)

# --------------------------------------------------------------------------- #

def main():
    sites_cfg, change_threshold, limits, options = load_config()
    state = load_state()

    # Phát hiện "cold start": chưa có state nào
    is_cold_start = (not os.path.exists(STATE_FILE)) or (not state.get("sites"))

    remaining_total = limits.max_total_urls
    all_reports = []

    # Thu thập số liệu tổng hợp cho Sheets (mỗi site một dòng)
    summary_rows = []
    run_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    for site in sites_cfg:
        base = site["url"].rstrip("/")
        include_paths = site.get("include_paths") or []
        exclude_paths = site.get("exclude_paths") or []
        domain_key = urlparse(base).netloc

        print(f"==> Processing {base}")
        site_state = state["sites"].get(domain_key, {"urls": {}, "last_run": 0})

        # 1) Thu thập URL từ sitemap
        sitemap_url = discover_sitemaps(base, timeout=limits.request_timeout_sec, retries=limits.request_retries)
        urls = set()
        if sitemap_url:
            urls |= parse_sitemap_collect(sitemap_url, limits.request_timeout_sec, limits.request_retries, limits)
        else:
            print(f"  No sitemap found for {base}")

        # 2) Thêm từ RSS nếu bật discover
        if options.get("discover_rss", True):
            feeds = discover_rss_feeds(base, limits.request_timeout_sec, limits.request_retries)
            for f in feeds:
                urls |= parse_rss_items(f, limits.request_timeout_sec, limits.request_retries, limits)

        # 3) Lọc include/exclude + limit ngân sách
        urls = [u for u in urls if should_include(u, include_paths, exclude_paths)]
        urls = clamp_urls(urls, limits, remaining_total)
        remaining_total -= len(urls)
        print(f"  Collected {len(urls)} URLs after filters/limits")

        new_urls, changed_urls, gone_urls = [], [], []

        # 4) So sánh fingerprint
        for u in urls:
            try:
                h, L = content_fingerprint(u, limits.request_timeout_sec, limits.request_retries, limits.polite_sleep_ms)
            except Exception as e:
                print("  Fetch fail:", u, e)
                continue

            prev = site_state["urls"].get(u)
            if prev is None:
                new_urls.append(u)
            else:
                if prev["hash"] != h:
                    prev_len = int(prev.get("len", 0))
                    if abs(L - prev_len) >= change_threshold:
                        changed_urls.append(u)
            site_state["urls"][u] = {"hash": h, "len": L}

        # 5) URL biến mất (trong state nhưng không còn ở sitemap/RSS)
        current_set = set(urls)
        for u in list(site_state["urls"].keys()):
            # nếu nay cấu hình đã siết include/exclude thì bỏ qua URL ngoài phạm vi
            if include_paths and not should_include(u, include_paths, exclude_paths):
                continue
            if u not in current_set:
                gone_urls.append(u)

        # 6) Tạo thông điệp (chỉ gửi nếu không phải cold start)
        blocks = []
        if new_urls:
            blocks.append("🔔 URL mới:\n" + "\n".join(new_urls[:20]) + (f"\n…(+{len(new_urls)-20})" if len(new_urls) > 20 else ""))
        if changed_urls:
            blocks.append("♻️ Nội dung thay đổi (> threshold):\n" + "\n".join(changed_urls[:20]) + (f"\n…(+{len(changed_urls)-20})" if len(changed_urls) > 20 else ""))
        if gone_urls:
            blocks.append("⚠️ URL biến mất khỏi sitemap/RSS:\n" + "\n".join(gone_urls[:10]) + (f"\n…(+{len(gone_urls)-10})" if len(gone_urls) > 10 else ""))

        if blocks and not is_cold_start:
            msg = f"*[{domain_key}]* cập nhật:\n\n" + "\n\n".join(blocks)
            all_reports.append(msg)

        # 7) Ghi summary cho Sheets (mỗi site một dòng)
        summary_rows.append([
            run_ts,
            domain_key,
            len(new_urls),
            len(changed_urls),
            len(gone_urls),
        ])

        site_state["last_run"] = int(time.time())
        state["sites"][domain_key] = site_state

        if remaining_total <= 0:
            print("Reached total URL budget; stopping early.")
            break

    # 8) Lưu state lên đĩa trước khi thông báo
    save_state(state)

    # 9) Ghi Google Sheets (rolling theo tháng)
    try:
        append_to_google_sheets(summary_rows)
    except Exception as e:
        print("Google Sheets append error:", e)

    # 10) Chính sách gửi Slack
    if is_cold_start and not all_reports:
        # Lần chạy đầu: chỉ gửi 1 dòng khởi tạo
        post_to_slack(SLACK_WEBHOOK_URL, f"🚀 Khởi tạo theo dõi lần đầu ({len(sites_cfg)} site). Không gửi danh sách URL.")
        return

    if all_reports:
        for m in all_reports:
            post_to_slack(SLACK_WEBHOOK_URL, m)
    else:
        post_to_slack(SLACK_WEBHOOK_URL, "✅ Không có thay đổi đáng kể (> threshold) trong lần quét hôm nay.")

if __name__ == "__main__":
    main()
