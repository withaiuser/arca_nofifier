"""
아카라이브 채널 새 글 키워드 알림 -> 디스코드 웹후크

설치:  pip install curl_cffi beautifulsoup4
       (curl_cffi가 없으면 requests로 동작하지만 차단될 가능성이 더 높음)
실행:  python arca_notifier.py          # 계속 돌면서 주기적으로 확인
       python arca_notifier.py --once   # 한 번만 확인 (작업 스케줄러/cron용)
"""
import json
import os
import random
import re
import sys
import time
from urllib.parse import quote

try:
    from curl_cffi import requests as http  # 브라우저 TLS 지문 흉내 -> 차단 덜 당함
    HTTP_KW = {"impersonate": "chrome"}
except ImportError:
    import requests as http
    HTTP_KW = {}

from bs4 import BeautifulSoup

# ===================== 설정 =====================
CONFIG = {
    # 채널 주소의 /b/ 뒤 부분. 예: https://arca.live/b/bluearchive -> "bluearchive"
    # (환경변수 ARCA_CHANNEL 로 넣어도 됨)
    "channel": os.environ.get("ARCA_CHANNEL") or "CHANNEL_ID",
    # 이 중 하나라도 포함되면 알림 (대소문자 무시)
    # (환경변수 ARCA_KEYWORDS 에 쉼표로 구분해 넣어도 됨: "키워드1,키워드2")
    "keywords": [k.strip() for k in os.environ["ARCA_KEYWORDS"].split(",")]
    if os.environ.get("ARCA_KEYWORDS") else ["키워드1", "키워드2"],
    # False: 목록 페이지 1번만 읽고 '제목'에서 키워드 검사 (요청 적음, 권장)
    # True : 키워드마다 사이트 검색(제목+내용)을 돌림 (본문까지 잡지만 요청이 키워드 수만큼 늘어남)
    # (환경변수 ARCA_SEARCH_CONTENT=true 로 켜도 됨)
    "search_content": os.environ.get("ARCA_SEARCH_CONTENT", "").lower() == "true",
    # 디스코드 웹후크 URL (환경변수 DISCORD_WEBHOOK_URL 로 넣어도 됨)
    "webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", "여기에_웹후크_URL"),
    # 확인 주기(초). 사이트에 부담 주지 않게 60초 이상 권장
    "interval_sec": 120,
    # 이미 본 글 번호 저장 파일
    "state_file": "arca_seen.json",
}
# =================================================

BASE = "https://arca.live"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
}


class Blocked(Exception):
    pass


def fetch(url):
    r = http.get(url, headers=HEADERS, timeout=20, **HTTP_KW)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"HTTP {r.status_code}")
    r.raise_for_status()
    return r.text


def parse_posts(html, channel):
    """목록/검색 페이지에서 (공지 제외) 글 목록 추출"""
    soup = BeautifulSoup(html, "html.parser")
    pat = re.compile(rf"^/b/{re.escape(channel)}/(\d+)")
    posts, seen = [], set()

    rows = soup.select("a.vrow") or soup.select("a[href]")
    for a in rows:
        m = pat.match(a.get("href", ""))
        if not m:
            continue
        if "notice" in (a.get("class") or []):
            continue
        pid = int(m.group(1))
        if pid in seen:
            continue
        seen.add(pid)

        t = a.select_one(".title")
        title = (t or a).get_text(" ", strip=True)
        title = re.sub(r"\s*\[\d+\]\s*$", "", title)  # 댓글 수 [3] 제거
        author_el = a.select_one(".user-info")
        author = author_el.get_text(" ", strip=True) if author_el else ""

        posts.append({
            "id": pid,
            "title": title,
            "author": author,
            "url": f"{BASE}/b/{channel}/{pid}",
        })
    return posts


def find_matches(cfg):
    ch = cfg["channel"]
    kws = [k for k in cfg["keywords"] if k.strip()]
    found = {}  # id -> (post, keyword)

    if cfg["search_content"]:
        for kw in kws:
            url = f"{BASE}/b/{ch}?target=title_content&keyword={quote(kw)}"
            for p in parse_posts(fetch(url), ch):
                found.setdefault(p["id"], (p, kw))
            time.sleep(random.uniform(2, 4))  # 연속 요청 간격
    else:
        for p in parse_posts(fetch(f"{BASE}/b/{ch}"), ch):
            low = p["title"].lower()
            for kw in kws:
                if kw.lower() in low:
                    found.setdefault(p["id"], (p, kw))
                    break
    return found


def load_state(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return set(json.load(f))


def save_state(path, ids):
    keep = sorted(ids)[-3000:]  # 너무 커지지 않게 최근 것만
    with open(path, "w", encoding="utf-8") as f:
        json.dump(keep, f)


def send_discord(webhook, post, kw, channel):
    payload = {
        "username": "아카라이브 알림",
        "embeds": [{
            "title": post["title"][:256] or "(제목 없음)",
            "url": post["url"],
            "description": f"키워드: **{kw}**" + (f"\n작성자: {post['author']}" if post["author"] else ""),
            "footer": {"text": f"arca.live/b/{channel}"},
            "color": 0x00A495,
        }],
    }
    for _ in range(3):
        r = http.post(webhook, json=payload, timeout=15, **HTTP_KW)
        if r.status_code == 429:  # 디스코드 속도 제한
            try:
                wait = float(r.json().get("retry_after", 2))
            except Exception:
                wait = 2
            time.sleep(wait + 0.5)
            continue
        r.raise_for_status()
        return
    print("디스코드 전송 실패(속도 제한):", post["url"])


def check_once(cfg):
    seen = load_state(cfg["state_file"])
    matches = find_matches(cfg)

    if seen is None:
        # 첫 실행: 지금 있는 글은 '본 것'으로 기록만 하고 알림 안 보냄
        save_state(cfg["state_file"], set(matches))
        print(f"[초기화] 기존 글 {len(matches)}개 기록. 이후 새 글부터 알림합니다.")
        return

    new = [matches[i] for i in sorted(matches) if i not in seen]
    for post, kw in new:
        send_discord(cfg["webhook_url"], post, kw, cfg["channel"])
        print(f"[알림] {post['title']}  {post['url']}")
        seen.add(post["id"])
        time.sleep(1)
    save_state(cfg["state_file"], seen)
    if not new:
        print(time.strftime("%H:%M:%S"), "새 글 없음")


def main():
    cfg = CONFIG
    if "여기에" in cfg["webhook_url"] or cfg["channel"] == "CHANNEL_ID":
        sys.exit("CONFIG의 channel / keywords / webhook_url 을 먼저 채워주세요.")

    if "--once" in sys.argv:
        try:
            check_once(cfg)
        except Blocked as e:
            # 차단은 매번 실패 메일이 오지 않도록 경고만 남기고 정상 종료
            print(f"::warning::아카라이브가 요청을 차단함({e})")
        return

    backoff = cfg["interval_sec"]
    while True:
        try:
            check_once(cfg)
            backoff = cfg["interval_sec"]
        except Blocked as e:
            backoff = min(backoff * 2, 1800)
            print(f"차단/제한 응답({e}). {backoff}초 후 재시도")
        except Exception as e:
            print("오류:", repr(e))
        time.sleep(backoff + random.uniform(0, 15))


if __name__ == "__main__":
    main()
