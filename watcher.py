#!/usr/bin/env python3
"""國圖多媒體創意實驗中心（OPEN LAB）空位監控。

每次執行：查詢 SPACES 裡每個空間「今天起 7 天內」的時段，
跟上次結果比對，出現新的可預約時段就推播到 ntfy。

- 新日期進入 7 天預約窗口 → 會收到通知（解決「忘記預約」）
- 有人取消、時段空出來   → 會收到通知（解決「等空位」）

只用 Python 標準函式庫，不需要 pip install。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

# ===== 設定 =====
# 新增空間：在預約頁按 F12 → Network，點該空間後找 timezone_media.jsp，
# 從網址複製 roomSysId 和 location。
SPACES = [
    # name 會顯示在通知標題
    {"name": "星際直播攝影棚", "location": "214",
     "roomSysId": "20241008171426298-8830dc09-cacb-47d6-841d-3e5227040395"},
    {"name": "音樂創作室", "location": "216",
     "roomSysId": "20241008172103797-873676a6-5faf-45e0-a111-50946e8a664f"},
    {"name": "討論室 1", "location": "1",
     "roomSysId": "20241008171720718-967bffe2-3f75-4e66-a3b6-8edae69ec559"},
    {"name": "討論室 2", "location": "1",
     "roomSysId": "20241008171746079-9db1019f-655a-4e0f-84d8-110044d3d0cb"},
    {"name": "討論室 3", "location": "1",
     "roomSysId": "20241008171915527-963dc245-30d3-4bca-a0c3-a59d067ab3b0"},
]

DAYS_AHEAD = 7          # 線上預約開放使用前 7 日
REQUEST_GAP = 1.0       # 每個請求間隔秒數，別對國圖伺服器太兇
SKIP_MONDAY = True      # 每週一休館

BASE_URL = "https://spms.ncl.edu.tw/NLT/co/timezone_media.jsp"
BOOKING_PAGE = "https://spms.ncl.edu.tw/NLT/co/media_discussion.jsp"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
DEBUG = os.environ.get("DEBUG") == "1"
TZ = ZoneInfo("Asia/Taipei")
WEEKDAY = "一二三四五六日"

TAG_RE = re.compile(r'<div\b[^>]*\bid="timebox\d+"[^>]*>', re.I)
CLASS_RE = re.compile(r'\bclass="([^"]*)"', re.I)
TIME_RE = re.compile(r'\bdata-param1="([^"]+)"', re.I)


def parse_slots(html: str) -> list[tuple[str, bool, str]]:
    """回傳 [(時段, 是否可預約, class 原文)]。

    已觀察到：被預約的時段 class 含 "disabled reserving"。
    規則：class 裡沒有 disabled 就視為可預約。
    """
    slots = []
    for tag in TAG_RE.findall(html):
        cls = CLASS_RE.search(tag)
        tm = TIME_RE.search(tag)
        if not tm:
            continue
        classes = cls.group(1).split() if cls else []
        slots.append((tm.group(1).strip(), "disabled" not in classes, " ".join(classes)))
    return slots


def fetch_slots(space: dict, day: date) -> list[tuple[str, bool, str]]:
    query = urllib.parse.urlencode({
        "roomSysId": space["roomSysId"],
        "rev4date": f"{day.month:02d}/{day.day:02d}",
        "location": space["location"],
    }, safe="/")
    req = urllib.request.Request(f"{BASE_URL}?{query}", headers={
        "Accept": "text/html, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BOOKING_PAGE,
        "User-Agent": "Mozilla/5.0 openlab-slot-watcher (personal reminder script)",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    return parse_slots(html)


def load_state() -> tuple[set[str], set[str]]:
    """回傳 (上次可預約的時段, 上次有查過的 空間|日期)。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return set(), set()
    if isinstance(data, list):  # 舊版格式
        return set(data), set()
    return set(data.get("available", [])), set(data.get("checked", []))


def save_state(available: set[str], checked: set[str]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"available": sorted(available), "checked": sorted(checked)},
                  f, ensure_ascii=False, indent=1)


def notify(title: str, message: str, tags: list[str]) -> None:
    if not NTFY_TOPIC:
        print(f"[未設定 NTFY_TOPIC，只印出不推播]\n== {title} ==\n{message}\n")
        return
    body = json.dumps({
        "topic": NTFY_TOPIC,
        "title": title,
        "message": message,
        "click": BOOKING_PAGE,
        "tags": tags,
    }).encode("utf-8")
    req = urllib.request.Request(NTFY_SERVER, data=body,
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=20).read()


def format_by_date(keys: list[str]) -> list[str]:
    """把同一天的時段合併成一行：11/27（五）09:00-12:00、13:00-16:00"""
    by_day: dict[str, list[str]] = {}
    for key in keys:
        _, iso, tm = key.split("|")
        by_day.setdefault(iso, []).append(tm)
    lines = []
    for iso in sorted(by_day):
        d = date.fromisoformat(iso)
        lines.append(f"{d.month}/{d.day}（{WEEKDAY[d.weekday()]}）" + "、".join(sorted(by_day[iso])))
    return lines


def main() -> int:
    today = datetime.now(TZ).date()
    prev, prev_checked = load_state()
    current: set[str] = set()
    checked: set[str] = set()
    ok = fail = boxes_seen = 0

    for space in SPACES:
        for offset in range(DAYS_AHEAD + 1):
            day = today + timedelta(days=offset)
            if SKIP_MONDAY and day.weekday() == 0:
                continue
            prefix = f"{space['name']}|{day.isoformat()}|"
            try:
                slots = fetch_slots(space, day)
                ok += 1
            except Exception as e:  # 網路錯誤：沿用上次結果，避免下次重複通知
                fail += 1
                print(f"[錯誤] {prefix} {e}", file=sys.stderr)
                current |= {k for k in prev if k.startswith(prefix)}
                if prefix[:-1] in prev_checked:
                    checked.add(prefix[:-1])
                time.sleep(REQUEST_GAP)
                continue
            checked.add(prefix[:-1])
            boxes_seen += len(slots)
            for tm, available, cls in slots:
                if DEBUG:
                    print(f"{prefix}{tm}  {'可預約' if available else '不可'}  [{cls}]")
                if available:
                    current.add(prefix + tm)
            time.sleep(REQUEST_GAP)

    new = current - prev
    first_run = not prev and not prev_checked
    sent = 0
    for space in SPACES:  # 每個空間一則通知，依 SPACES 的順序
        mine = sorted(k for k in new if k.startswith(space["name"] + "|"))
        if not mine:
            continue
        # 這個日期上次沒查過 → 新開放預約；上次查過但不可預約 → 有人取消或空出
        opened = [k for k in mine if first_run or k.rsplit("|", 1)[0] not in prev_checked]
        freed = [k for k in mine if k not in opened]
        parts = []
        if opened:
            parts.append("🆕 新開放預約\n" + "\n".join(format_by_date(opened)))
        if freed:
            parts.append("🔔 有人取消，空出時段\n" + "\n".join(format_by_date(freed)))
        tags = ["rotating_light"] if freed else ["calendar"]
        notify(f"{space['name']}：{len(mine)} 個時段可預約", "\n\n".join(parts), tags)
        sent += 1

    print(f"查詢成功 {ok}、失敗 {fail}，目前可預約 {len(current)}，"
          f"新出現 {len(new)}，送出 {sent} 則通知")
    save_state(current, checked)

    if ok == 0:
        print("全部查詢失敗", file=sys.stderr)
        return 1
    if boxes_seen == 0:
        # 全部查不到任何時段，很可能網站改版或開始要求登入
        print("完全沒解析到時段，網站可能改版了", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
