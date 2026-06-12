import json
import os
import time
import requests
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:
    firebase_admin = None
    credentials = None
    messaging = None

app = Flask(__name__)
CORS(app)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/ulsan_fcm_server"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

TOKENS_FILE = DATA_DIR / "tokens.json"
WATCH_FILE = DATA_DIR / "watchlist.json"
EVENT_FILE = DATA_DIR / "event_history.json"
AIS_ALERT_FILE = DATA_DIR / "ais_alert_state.json"
AIS_SHIP_STATE_FILE = DATA_DIR / "ais_ship_state.json"

AIS_ALERT_COOLDOWN_MINUTES = int(os.environ.get("AIS_ALERT_COOLDOWN_MINUTES", "30"))
AUTO_CHECK_ENABLED = os.environ.get("AUTO_CHECK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
AUTO_CHECK_INTERVAL_SECONDS = int(os.environ.get("AUTO_CHECK_INTERVAL_SECONDS", "300"))

# 3.0.3 특수 이벤트 설정
# 일반 이벤트는 AIS_ALERT_COOLDOWN_MINUTES(기본 30분) 쿨다운을 유지합니다.
# 아래 특수 이벤트는 30분 쿨다운을 무시하고,
# 선박 + 구역 + 단계 기준으로 1회씩 발송합니다.
SPECIAL_EVENT_PREFIXES = (
    "berth_approaching",
    "berth_completed",
    "anchorage_approaching",
    "anchorage_completed",
)

SERVER_API_KEY = os.environ.get("SERVER_API_KEY", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()

firebase_ready = False
firebase_error = ""

auto_checker_started = False
auto_checker_pid = 0
auto_checker_last_run = ""
auto_checker_last_result: Dict[str, Any] = {}
auto_checker_run_count = 0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_api_key() -> bool:
    if not SERVER_API_KEY:
        return True
    supplied = request.headers.get("X-API-Key", "").strip() or request.args.get("api_key", "").strip()
    return supplied == SERVER_API_KEY


def read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_iso_time(value: str):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def is_special_event_type(event_type: str) -> bool:
    return any(str(event_type).startswith(prefix) for prefix in SPECIAL_EVENT_PREFIXES)


def should_send_ais_alert(ship_name: str, event_type: str, force: bool = False) -> tuple[bool, str]:
    """
    일반 이벤트: 같은 선박 + 같은 이벤트는 AIS_ALERT_COOLDOWN_MINUTES 동안 차단합니다.
    특수 이벤트: berth_approaching/completed, anchorage_approaching/completed 계열은
    30분 쿨다운을 무시하고 정확히 같은 event_type 기준 1회만 발송합니다.

    특수 이벤트의 event_type은 반드시 구역을 포함해야 합니다.
    예: berth_approaching:ULSAN_PORT_BERTH_AREA
        berth_completed:SK_BUOY
        anchorage_completed:M3
    """
    if force:
        return True, "forced"

    normalized_ship = normalize_ship_name(ship_name)
    event_type = str(event_type).strip()
    key = f"{normalized_ship}::{event_type}"
    state = read_json(AIS_ALERT_FILE, {})
    now = datetime.now(timezone.utc)

    previous_raw = state.get(key, {}).get("sentAt") if isinstance(state.get(key), dict) else None
    previous_dt = parse_iso_time(previous_raw) if previous_raw else None

    # 특수 이벤트는 30분 쿨다운 대상이 아닙니다.
    # 대신 같은 선박 + 같은 구역 + 같은 단계는 반복 발송하지 않습니다.
    if is_special_event_type(event_type):
        if previous_dt is not None:
            return False, "special event already sent"

        state[key] = {
            "shipName": normalized_ship,
            "eventType": event_type,
            "special": True,
            "sentAt": now.isoformat(),
        }
        write_json(AIS_ALERT_FILE, cleanup_alert_state(state, now))
        return True, "ok special"

    if previous_dt is not None:
        diff_minutes = (now - previous_dt).total_seconds() / 60
        if diff_minutes < AIS_ALERT_COOLDOWN_MINUTES:
            remain = max(1, int(AIS_ALERT_COOLDOWN_MINUTES - diff_minutes))
            return False, f"cooldown {remain}min remaining"

    state[key] = {
        "shipName": normalized_ship,
        "eventType": event_type,
        "special": False,
        "sentAt": now.isoformat(),
    }

    write_json(AIS_ALERT_FILE, cleanup_alert_state(state, now))
    return True, "ok"


def cleanup_alert_state(state: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """
    중복방지 기록 정리.
    - 일반 이벤트: 24시간 보관
    - 특수 이벤트: 48시간 보관
      같은 부두/묘지에서 장시간 0.0kn 상태가 유지될 때 반복 알림을 막기 위함입니다.
    """
    cleaned = {}
    for item_key, item in state.items():
        if not isinstance(item, dict):
            continue
        sent_at = parse_iso_time(item.get("sentAt"))
        if sent_at is None:
            continue
        keep_seconds = 48 * 3600 if item.get("special") else 24 * 3600
        if (now - sent_at).total_seconds() <= keep_seconds:
            cleaned[item_key] = item
    return cleaned

def normalize_ship_name(value: Any) -> str:
    return str(value).strip().upper()


def unique_ship_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    result: List[str] = []
    seen = set()
    for value in values:
        name = normalize_ship_name(value)
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def watch_summary(watch: Dict[str, Any], token: str = "", ship_name: str = "") -> Dict[str, Any]:
    total_watch_devices = len(watch)
    total_watch_links = 0
    total_watch_ships_set = set()
    my_watch_count = 0
    ship_watch_device_count = 0
    normalized_ship = normalize_ship_name(ship_name)

    for device_token, item in watch.items():
        ships = unique_ship_list(item.get("ships", [])) if isinstance(item, dict) else []
        total_watch_links += len(ships)
        total_watch_ships_set.update(ships)

        if token and device_token == token:
            my_watch_count = len(ships)

        if normalized_ship and normalized_ship in ships:
            ship_watch_device_count += 1

    return {
        "watchDeviceCount": total_watch_devices,
        "totalWatchShips": len(total_watch_ships_set),
        "totalWatchLinks": total_watch_links,
        "myWatchCount": my_watch_count,
        "shipWatchDeviceCount": ship_watch_device_count,
    }


def init_firebase() -> None:
    global firebase_ready, firebase_error
    if firebase_ready:
        return

    if firebase_admin is None:
        firebase_error = "firebase_admin 패키지를 import하지 못했습니다. requirements.txt를 확인하세요."
        return

    try:
        if firebase_admin._apps:
            firebase_ready = True
            return

        if FIREBASE_SERVICE_ACCOUNT_JSON:
            info = json.loads(FIREBASE_SERVICE_ACCOUNT_JSON)
            cred = credentials.Certificate(info)
        elif FIREBASE_SERVICE_ACCOUNT_FILE:
            cred = credentials.Certificate(FIREBASE_SERVICE_ACCOUNT_FILE)
        else:
            firebase_error = "FIREBASE_SERVICE_ACCOUNT_JSON 환경변수가 없습니다."
            return

        firebase_admin.initialize_app(cred)
        firebase_ready = True
        firebase_error = ""
    except Exception as exc:
        firebase_ready = False
        firebase_error = str(exc)


def send_fcm_to_tokens(target_tokens: List[str], title: str, body: str, data: Dict[str, str] | None = None) -> Dict[str, Any]:
    if not firebase_ready:
        return {
            "ok": False,
            "success": 0,
            "requested": len(target_tokens),
            "errors": [f"firebase not ready: {firebase_error}"],
        }

    success = 0
    errors = []

    for token in target_tokens:
        try:
            msg = messaging.Message(
                token=token,
                notification=messaging.Notification(title=title, body=body),
                data=data or {},
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(sound="default"),
                ),
            )
            message_id = messaging.send(msg)
            print(f"FCM SUCCESS: {message_id}")
            success += 1
        except Exception as exc:
            print(f"FCM ERROR: {exc}")
            errors.append(str(exc))

    return {
        "ok": True,
        "requested": len(target_tokens),
        "success": success,
        "errors": errors[:5],
    }


def tokens_for_ship(ship_name: str) -> List[str]:
    normalized = normalize_ship_name(ship_name)
    watch = read_json(WATCH_FILE, {})
    result = []

    for token, item in watch.items():
        ships = unique_ship_list(item.get("ships", [])) if isinstance(item, dict) else []
        if normalized in ships:
            result.append(token)

    return result


def fetch_upa_ais_list() -> List[Dict[str, Any]]:
    url = "https://www.upa.or.kr/abs/cmm/init/getAisListOnTheMap.do"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.upa.or.kr/abs/main/mainPage.do",
        "Origin": "https://www.upa.or.kr",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    data = {
        "bookmarkYn": "N",
        "north": "35.750000",
        "south": "35.150000",
        "east": "129.750000",
        "west": "129.050000",
    }

    response = requests.post(url, headers=headers, data=data, timeout=15)
    response.raise_for_status()
    decoded = response.json()

    if isinstance(decoded, list):
        return [item for item in decoded if isinstance(item, dict)]

    if isinstance(decoded, dict):
        raw_list = (
            decoded.get("vsslList")
            or decoded.get("ships")
            or decoded.get("data")
            or decoded.get("items")
            or decoded.get("vessels")
            or []
        )
        if isinstance(raw_list, list):
            return [item for item in raw_list if isinstance(item, dict)]

    return []


def pick_ship_name(item: Dict[str, Any]) -> str:
    return normalize_ship_name(
        item.get("aisVsslNm")
        or item.get("vsslNm")
        or item.get("shipName")
        or item.get("name")
        or item.get("vslNm")
        or item.get("vesselName")
        or item.get("vsl_eng_nm")
        or ""
    )


def pick_text(item: Dict[str, Any], keys: List[str], default: str = "-") -> str:
    for key in keys:
        value = item.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def pick_float(item: Dict[str, Any], keys: List[str], default: float = 0.0) -> float:
    for key in keys:
        value = item.get(key)
        try:
            if value is not None and str(value).strip():
                return float(str(value).strip())
        except Exception:
            continue
    return default


@app.before_request
def before_request() -> None:
    init_firebase()
    ensure_auto_checker_running()


@app.get("/")
def index():
    tokens = read_json(TOKENS_FILE, [])
    watch = read_json(WATCH_FILE, {})
    summary = watch_summary(watch)
    return jsonify({
        "service": "ulsan-ais-fcm-server",
        "version": "3.0.4-watch-api-compat",
        "ok": True,
        "firebaseReady": firebase_ready,
        "firebaseError": firebase_error,
        "tokenCount": len(tokens),
        "autoCheckEnabled": AUTO_CHECK_ENABLED,
        "autoCheckIntervalSeconds": AUTO_CHECK_INTERVAL_SECONDS,
        "autoCheckStarted": auto_checker_started,
        "autoCheckPid": auto_checker_pid,
        "currentPid": os.getpid(),
        "autoCheckLastRun": auto_checker_last_run,
        **summary,
        "time": now_iso(),
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "firebaseReady": firebase_ready,
        "firebaseError": firebase_error,
        "time": now_iso(),
    })


@app.post("/register-token")
def register_token():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    if not token:
        return jsonify({"ok": False, "error": "token is required"}), 400

    tokens: List[Dict[str, Any]] = read_json(TOKENS_FILE, [])
    now = now_iso()
    found = False

    for item in tokens:
        if item.get("token") == token:
            item.update({
                "app": payload.get("app", item.get("app", "ulsan_ais_mobile")),
                "platform": payload.get("platform", item.get("platform", "android")),
                "device": payload.get("device", item.get("device", "android")),
                "version": payload.get("version", item.get("version", "")),
                "lastSeenAt": now,
            })
            found = True
            break

    if not found:
        tokens.append({
            "token": token,
            "app": payload.get("app", "ulsan_ais_mobile"),
            "platform": payload.get("platform", "android"),
            "device": payload.get("device", "android"),
            "version": payload.get("version", ""),
            "createdAt": now,
            "lastSeenAt": now,
        })

    write_json(TOKENS_FILE, tokens)
    return jsonify({"ok": True, "registered": True, "tokenCount": len(tokens), "time": now})


@app.get("/tokens/count")
def token_count():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401
    tokens = read_json(TOKENS_FILE, [])
    return jsonify({"ok": True, "tokenCount": len(tokens)})


@app.post("/send-test")
def send_test():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401
    if not firebase_ready:
        return jsonify({"ok": False, "error": "firebase not ready", "firebaseError": firebase_error}), 500

    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "🚢 울산항 AIS 테스트 알림"))
    body = str(payload.get("body", "FCM 서버에서 보낸 테스트 푸시입니다."))
    token = str(payload.get("token", "")).strip()

    if token:
        target_tokens = [token]
    else:
        stored = read_json(TOKENS_FILE, [])
        target_tokens = [
            str(item.get("token", "")).strip()
            for item in stored
            if str(item.get("token", "")).strip()
        ]

    if not target_tokens:
        return jsonify({"ok": False, "error": "no tokens registered"}), 400

    result = send_fcm_to_tokens(
        target_tokens,
        title,
        body,
        {
            "source": "ulsan_ais_fcm_server",
            "eventType": "test",
            "sentAt": now_iso(),
        },
    )

    history = read_json(EVENT_FILE, [])
    history.insert(0, {
        "type": "test",
        "title": title,
        "body": body,
        "success": result["success"],
        "errors": result["errors"],
        "time": now_iso(),
    })
    write_json(EVENT_FILE, history[:200])

    return jsonify(result)


@app.post("/test-ship-alert")
def test_ship_alert():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401
    if not firebase_ready:
        return jsonify({"ok": False, "error": "firebase not ready", "firebaseError": firebase_error}), 500

    payload = request.get_json(silent=True) or {}
    ship_name = normalize_ship_name(payload.get("shipName", ""))
    title = str(payload.get("title", f"🚢 {ship_name} 감시 알림"))
    body = str(payload.get("body", f"{ship_name} 테스트 선박 이벤트가 발생했습니다."))

    if not ship_name:
        return jsonify({"ok": False, "error": "shipName is required"}), 400

    target_tokens = tokens_for_ship(ship_name)

    if not target_tokens:
        return jsonify({
            "ok": False,
            "error": "no watchers for this ship",
            "shipName": ship_name,
            "targetCount": 0,
        }), 404

    result = send_fcm_to_tokens(
        target_tokens,
        title,
        body,
        {
            "source": "ulsan_ais_fcm_server",
            "eventType": "ship_test_alert",
            "shipName": ship_name,
            "sentAt": now_iso(),
        },
    )

    print(f"SHIP ALERT result ship={ship_name} result={result}")

    history = read_json(EVENT_FILE, [])
    history.insert(0, {
        "type": "ship_test_alert",
        "shipName": ship_name,
        "title": title,
        "body": body,
        "targetCount": len(target_tokens),
        "success": result["success"],
        "errors": result["errors"],
        "time": now_iso(),
    })
    write_json(EVENT_FILE, history[:200])

    return jsonify({
        "ok": True,
        "shipName": ship_name,
        "targetCount": len(target_tokens),
        "success": result["success"],
        "errors": result["errors"],
        "time": now_iso(),
    })




def is_underway_like(status: str, speed: float) -> bool:
    value = str(status).upper()
    return (
        speed >= 3.0
        or "항해" in value
        or "UNDER" in value
        or "SAILING" in value
        or "NAVIGATION" in value
    )


def is_stopped_like(status: str, speed: float) -> bool:
    value = str(status).upper()
    return (
        speed <= 1.0
        or "정박" in value
        or "접안" in value
        or "묘박" in value
        or "투묘" in value
        or "MOORED" in value
        or "ANCHOR" in value
        or "BERTH" in value
        or "DOCK" in value
    )


def simple_area_from_lat_lon(lat: float, lon: float) -> str:
    if lat == 0 or lon == 0:
        return "위치 확인중"

    # 울산항 주변의 대략적인 구역명입니다. 정확한 부두/묘박지 판정은 2.9.14에서 세분화합니다.
    if lat >= 35.48 and lon >= 129.42:
        return "외항/동측 해역"
    if lat >= 35.43 and lon >= 129.35:
        return "울산항 접근 해역"
    if lat >= 35.37 and lon >= 129.33:
        return "울산항 항내/부두권"
    return "울산항 인근"



def normalize_zone_id(value: str) -> str:
    zone = str(value or "").strip().upper()
    zone = zone.replace(" ", "_").replace("/", "_").replace("-", "_")
    zone = "".join(ch for ch in zone if ch.isalnum() or ch in "_가-힣")
    return zone or "UNKNOWN_ZONE"


def is_berth_area(location: str) -> bool:
    value = str(location or "").upper()
    berth_keywords = [
        "부두", "선석", "접안", "항내/부두권", "BERTH", "DOCK", "PIER", "WHARF",
        "SK", "S-OIL", "SOIL", "정일", "UTK", "UTT", "OTK", "본항", "염포", "온산", "용연", "용잠",
    ]
    return any(keyword.upper() in value for keyword in berth_keywords)


def is_anchorage_area(location: str) -> bool:
    value = str(location or "").upper().replace(" ", "")
    if "묘지" in value or "묘박" in value or "정박지" in value or "ANCHOR" in value:
        return True
    # M1~M7, E1~E3 형태. 3.0.3에서 실제 좌표 기반 세부 구역 판정으로 확장 예정.
    import re
    return re.search(r"(^|[^A-Z0-9])(M[1-7]|E[1-3])([^A-Z0-9]|$)", value) is not None


def berth_zone_label(location: str) -> str:
    value = str(location or "").strip()
    if not value or value == "위치 확인중":
        return "부두권"
    return value


def anchorage_zone_label(location: str) -> str:
    value = str(location or "").strip()
    if not value or value == "위치 확인중":
        return "묘박지"
    return value

def build_ship_snapshot(ship_name: str, info: Dict[str, Any]) -> Dict[str, Any]:
    lat = float(info.get("lat", 0.0) or 0.0)
    lon = float(info.get("lon", 0.0) or 0.0)
    speed = float(info.get("speed", 0.0) or 0.0)
    status = str(info.get("status", "-"))
    location = str(info.get("location") or simple_area_from_lat_lon(lat, lon))

    return {
        "shipName": normalize_ship_name(ship_name),
        "mmsi": str(info.get("mmsi", "-")),
        "status": status,
        "speed": speed,
        "lat": lat,
        "lon": lon,
        "location": location,
        "destination": str(info.get("destination", "-")),
        "eta": str(info.get("eta", "-")),
        "seenAt": now_iso(),
    }


def detect_ship_events(ship_name: str, current: Dict[str, Any], previous: Dict[str, Any] | None, force: bool = False) -> List[Dict[str, str]]:
    """
    이전 AIS 상태와 현재 AIS 상태를 비교해서 발송할 이벤트 목록을 만듭니다.

    3.0.3 핵심 변경:
    - 일반 이벤트는 기존 30분 쿨다운 유지
    - 특수 이벤트는 30분 쿨다운 예외
    - 특수 이벤트는 선박 + 구역 + 단계 기준 1회만 발송
    - 부두 접안중 / 부두 접안완료 / M/E 묘지 투묘중 / M/E 묘지 투묘완료를 분리

    특수 이벤트 조건:
    - 부두 접안중: 부두권 + 0.5~1.0kn
    - 부두 접안완료: 부두권 + 0.1kn 이하 + 정박/MOORED/접안 계열 상태
    - 묘지 투묘중: M/E 묘지/정박지 + 0.5~1.5kn
    - 묘지 투묘완료: M/E 묘지/정박지 + 0.1kn 이하
    """
    events: List[Dict[str, str]] = []
    name = normalize_ship_name(ship_name)

    current_status = str(current.get("status", "-"))
    current_speed = float(current.get("speed", 0.0) or 0.0)
    current_location = str(current.get("location", "위치 확인중"))

    has_previous = isinstance(previous, dict)

    if not has_previous:
        events.append({
            "eventType": "ais_first_detected",
            "title": f"🚢 {name} AIS 최초 감지",
            "body": f"{name} 선박이 울산항 AIS에서 처음 감지되었습니다. 위치: {current_location}",
        })

    previous_status = str(previous.get("status", "-")) if has_previous else "-"
    previous_speed = float(previous.get("speed", 0.0) or 0.0) if has_previous else 0.0
    previous_location = str(previous.get("location", "위치 확인중")) if has_previous else "위치 확인중"

    prev_stopped = is_stopped_like(previous_status, previous_speed)
    now_stopped = is_stopped_like(current_status, current_speed)
    prev_underway = is_underway_like(previous_status, previous_speed)
    now_underway = is_underway_like(current_status, current_speed)

    berth_area = is_berth_area(current_location)
    anchorage_area = is_anchorage_area(current_location)
    berth_zone = berth_zone_label(current_location)
    anchorage_zone = anchorage_zone_label(current_location)
    berth_zone_id = normalize_zone_id(berth_zone)
    anchorage_zone_id = normalize_zone_id(anchorage_zone)

    current_status_upper = current_status.upper()
    berth_completed_status = (
        "MOORED" in current_status_upper
        or "접안" in current_status
        or "정박" in current_status
        or "BERTH" in current_status_upper
        or "DOCK" in current_status_upper
    )

    special_event_added = False

    # 부두 접안중: 부두/선석권에서 0.5~1.0kn 사이로 천천히 접근하는 단계.
    # 30분 쿨다운과 무관하며, should_send_ais_alert()에서 선박+구역+단계 기준 1회만 발송됩니다.
    if berth_area and 0.5 <= current_speed <= 1.0:
        events.append({
            "eventType": f"berth_approaching:{berth_zone_id}",
            "title": f"🚢 {name} 부두 접안중",
            "body": f"{name} 선박이 {berth_zone} 근처에서 접안 중으로 감지되었습니다. 현재 속도: {current_speed:.1f} kn",
        })
        special_event_added = True

    # 부두 접안완료: 부두/선석권 + 0.1kn 이하 + 정박/MOORED/접안 계열 상태.
    if berth_area and current_speed <= 0.1 and berth_completed_status:
        events.append({
            "eventType": f"berth_completed:{berth_zone_id}",
            "title": f"⚓ {name} 부두 접안완료",
            "body": f"{name} 선박이 {berth_zone}에서 접안완료 상태로 감지되었습니다. 현재 속도: {current_speed:.1f} kn",
        })
        special_event_added = True

    # 묘박지 투묘중: M/E 묘지 또는 정박지에서 0.5~1.5kn 사이로 감속/진입하는 단계.
    if anchorage_area and 0.5 <= current_speed <= 1.5:
        events.append({
            "eventType": f"anchorage_approaching:{anchorage_zone_id}",
            "title": f"⚓ {name} 묘지 투묘중",
            "body": f"{name} 선박이 {anchorage_zone}에서 투묘 중으로 감지되었습니다. 현재 속도: {current_speed:.1f} kn",
        })
        special_event_added = True

    # 묘박지 투묘완료: M/E 묘지 또는 정박지에서 0.1kn 이하.
    if anchorage_area and current_speed <= 0.1:
        events.append({
            "eventType": f"anchorage_completed:{anchorage_zone_id}",
            "title": f"⚓ {name} 묘지 투묘완료",
            "body": f"{name} 선박이 {anchorage_zone}에서 투묘완료 상태로 감지되었습니다. 현재 속도: {current_speed:.1f} kn",
        })
        special_event_added = True

    if has_previous and prev_stopped and now_underway:
        events.append({
            "eventType": "departure_detected",
            "title": f"🔔 {name} 출항 · 이동 시작",
            "body": f"{name} 선박이 {previous_location}에서 이동을 시작했습니다. 현재 속도: {current_speed:.1f} kn",
        })

    # 특수 접안/투묘 이벤트가 이미 잡힌 경우에는 기존의 포괄적 anchored_or_docked 알림은 생략합니다.
    # 이렇게 해야 '접안중 → 접안완료'처럼 세분화된 알림이 일반 정박 알림에 묻히지 않습니다.
    if has_previous and prev_underway and now_stopped and not special_event_added:
        events.append({
            "eventType": "anchored_or_docked",
            "title": f"⚓ {name} 정박 · 접안 감지",
            "body": f"{name} 선박이 정박 또는 접안 상태로 감지되었습니다. 위치: {current_location}",
        })

    if has_previous and previous_status != current_status:
        events.append({
            "eventType": f"status_changed:{current_status}",
            "title": f"📡 {name} AIS 상태 변화",
            "body": f"{name} 상태가 {previous_status} → {current_status} 로 변경되었습니다.",
        })

    if has_previous and previous_location != current_location and current_location != "위치 확인중":
        events.append({
            "eventType": f"location_changed:{current_location}",
            "title": f"📍 {name} 위치 변화",
            "body": f"{name} 위치가 {previous_location} → {current_location} 로 변경되었습니다.",
        })

    # 너무 조용한 선박도 force 테스트에서는 감지 이벤트를 확인할 수 있게 합니다.
    if force and not events:
        events.append({
            "eventType": "ais_force_status_check",
            "title": f"🚢 {name} AIS 상태 확인",
            "body": f"{name} 현재 상태: {current_status}, 속도: {current_speed:.1f} kn, 위치: {current_location}",
        })

    return events


def perform_ais_check_once(force: bool = False, source: str = "manual") -> Dict[str, Any]:
    """
    UPA AIS를 1회 조회하고, watchlist에 등록된 선박의 상태 변화를 감지해서 해당 사용자에게만 FCM을 발송합니다.
    2.9.13부터는 단순 감지가 아니라 이전 상태와 비교해 입항/출항/정박/상태/위치 변화를 분리합니다.
    """
    if not firebase_ready:
        return {
            "ok": False,
            "error": "firebase not ready",
            "firebaseError": firebase_error,
            "source": source,
            "time": now_iso(),
        }

    watch = read_json(WATCH_FILE, {})
    watched_ships = set()

    for _, item in watch.items():
        ships = unique_ship_list(item.get("ships", [])) if isinstance(item, dict) else []
        watched_ships.update(ships)

    if not watched_ships:
        result_payload = {
            "ok": False,
            "error": "no watched ships",
            "watchedCount": 0,
            "source": source,
            "time": now_iso(),
        }
        # 앱/서버 연동 문제를 알림탭에서 바로 확인할 수 있도록 기록합니다.
        history = read_json(EVENT_FILE, [])
        history.insert(0, {
            "type": "check_ais_no_watched_ships",
            **result_payload,
        })
        write_json(EVENT_FILE, history[:200])
        return result_payload

    try:
        ais_list = fetch_upa_ais_list()
    except Exception as exc:
        return {
            "ok": False,
            "error": "ais fetch failed",
            "detail": str(exc),
            "source": source,
            "time": now_iso(),
        }

    detected_ships = {}
    for item in ais_list:
        name = pick_ship_name(item)
        if name and name in watched_ships:
            lat = pick_float(item, ["lat", "latitude", "la", "vslLat"], 0.0)
            lon = pick_float(item, ["lot", "lon", "lng", "longitude", "vslLot"], 0.0)
            detected_ships[name] = {
                "name": name,
                "mmsi": pick_text(item, ["aisMmsi", "mmsi", "MMSI", "mmsiNo"], "-"),
                "status": pick_text(item, ["oprtlSttsKr", "oprtlStts", "status", "navStatus", "nvgtStts", "state"], "-"),
                "speed": pick_float(item, ["sog", "speed", "spd", "knots"], 0.0),
                "lat": lat,
                "lon": lon,
                "location": simple_area_from_lat_lon(lat, lon),
                "destination": pick_text(item, ["destination", "dest", "dstn", "etaDest"], "-"),
                "eta": pick_text(item, ["eta", "etaTime", "arrvTm"], "-"),
            }

    previous_state = read_json(AIS_SHIP_STATE_FILE, {})
    new_state = dict(previous_state) if isinstance(previous_state, dict) else {}

    sent = []
    skipped = []
    errors = []
    event_results = []

    for ship_name, info in detected_ships.items():
        target_tokens = tokens_for_ship(ship_name)
        if not target_tokens:
            continue

        current_snapshot = build_ship_snapshot(ship_name, info)
        previous_snapshot = previous_state.get(ship_name) if isinstance(previous_state, dict) else None
        if not isinstance(previous_snapshot, dict):
            previous_snapshot = None

        ship_events = detect_ship_events(ship_name, current_snapshot, previous_snapshot, force=force)

        for event in ship_events:
            event_type = event["eventType"]
            can_send, reason = should_send_ais_alert(ship_name, event_type, force=force)
            if not can_send:
                skipped.append({"shipName": ship_name, "eventType": event_type, "reason": reason})
                continue

            result = send_fcm_to_tokens(
                target_tokens,
                event["title"],
                event["body"],
                {
                    "source": "ulsan_ais_fcm_server",
                    "eventType": event_type,
                    "shipName": ship_name,
                    "status": str(current_snapshot.get("status", "-")),
                    "speed": str(current_snapshot.get("speed", 0.0)),
                    "location": str(current_snapshot.get("location", "위치 확인중")),
                    "sentAt": now_iso(),
                },
            )

            event_results.append({
                "shipName": ship_name,
                "eventType": event_type,
                "title": event["title"],
                "success": result["success"],
            })

            if result["success"] > 0:
                sent.append({"shipName": ship_name, "eventType": event_type})
            if result["errors"]:
                errors.extend(result["errors"])

        # 감지된 선박은 현재 상태를 저장합니다.
        new_state[ship_name] = current_snapshot

    # 이번 AIS에서 사라진 감시선박도 상태에는 남겨두되, 24시간 이상 오래된 상태는 정리합니다.
    now_dt = datetime.now(timezone.utc)
    cleaned_state = {}
    for ship_name, snapshot in new_state.items():
        if not isinstance(snapshot, dict):
            continue
        seen_at = parse_iso_time(str(snapshot.get("seenAt", "")))
        if seen_at is None or (now_dt - seen_at).total_seconds() <= 24 * 3600:
            cleaned_state[ship_name] = snapshot

    write_json(AIS_SHIP_STATE_FILE, cleaned_state)

    result_payload = {
        "ok": True,
        "source": source,
        "watchedCount": len(watched_ships),
        "aisCount": len(ais_list),
        "detectedCount": len(detected_ships),
        "detectedShips": sorted(detected_ships.keys()),
        "eventCount": len(event_results),
        "events": event_results,
        "sentShips": sent,
        "skippedShips": skipped,
        "force": force,
        "cooldownMinutes": AIS_ALERT_COOLDOWN_MINUTES,
        "errors": errors[:5],
        "time": now_iso(),
    }

    history = read_json(EVENT_FILE, [])
    history.insert(0, {
        "type": "check_ais_state_change" if source == "auto" else "check_ais_once_state_change",
        **result_payload,
    })
    write_json(EVENT_FILE, history[:200])

    return result_payload

@app.post("/check-ais-once")
def check_ais_once():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force", False))

    result = perform_ais_check_once(force=force, source="manual")
    status = 200 if result.get("ok") else 500
    if result.get("error") == "no watched ships":
        status = 404
    return jsonify(result), status


@app.get("/auto-check-status")
def auto_check_status():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    return jsonify({
        "ok": True,
        "autoCheckEnabled": AUTO_CHECK_ENABLED,
        "autoCheckStarted": auto_checker_started,
        "autoCheckPid": auto_checker_pid,
        "currentPid": os.getpid(),
        "autoCheckIntervalSeconds": AUTO_CHECK_INTERVAL_SECONDS,
        "autoCheckLastRun": auto_checker_last_run,
        "autoCheckRunCount": auto_checker_run_count,
        "autoCheckLastResult": auto_checker_last_result,
        "cooldownMinutes": AIS_ALERT_COOLDOWN_MINUTES,
        "time": now_iso(),
    })


def auto_checker_loop() -> None:
    global auto_checker_last_run, auto_checker_last_result, auto_checker_run_count

    print(f"AUTO CHECKER LOOP START enabled={AUTO_CHECK_ENABLED} interval={AUTO_CHECK_INTERVAL_SECONDS}s")

    # 서버 부팅 직후 앱 토큰/감시목록 등록 시간을 조금 기다립니다.
    time.sleep(20)

    while True:
        try:
            init_firebase()
            auto_checker_last_run = now_iso()
            auto_checker_run_count += 1
            result = perform_ais_check_once(force=False, source="auto")
            auto_checker_last_result = result
            print(f"AUTO CHECK RESULT: {result}")
        except Exception as exc:
            auto_checker_last_run = now_iso()
            auto_checker_last_result = {
                "ok": False,
                "error": str(exc),
                "source": "auto",
                "time": now_iso(),
            }
            print(f"AUTO CHECK ERROR: {exc}")

        time.sleep(max(60, AUTO_CHECK_INTERVAL_SECONDS))


def ensure_auto_checker_running() -> None:
    """
    Gunicorn/Render 환경에서는 앱 import 시점과 실제 worker 실행 시점이 달라질 수 있습니다.
    그래서 요청이 들어올 때마다 현재 프로세스(pid) 안에서 자동감시 스레드가 살아있도록 보장합니다.
    """
    global auto_checker_started, auto_checker_pid

    if not AUTO_CHECK_ENABLED:
        return

    current_pid = os.getpid()

    if auto_checker_started and auto_checker_pid == current_pid:
        return

    auto_checker_started = True
    auto_checker_pid = current_pid

    thread = threading.Thread(target=auto_checker_loop, daemon=True)
    thread.start()
    print(f"AUTO CHECKER THREAD STARTED pid={current_pid}")


def start_auto_checker() -> None:
    ensure_auto_checker_running()


@app.post("/register-watch")
def register_watch():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    ships = payload.get("ships", [])

    if not token:
        return jsonify({"ok": False, "error": "token is required"}), 400
    if not isinstance(ships, list):
        return jsonify({"ok": False, "error": "ships must be a list"}), 400

    normalized_ships = unique_ship_list(ships)
    watch = read_json(WATCH_FILE, {})
    watch[token] = {
        "ships": normalized_ships,
        "updatedAt": now_iso(),
    }
    write_json(WATCH_FILE, watch)

    ship_name = normalized_ships[-1] if normalized_ships else ""
    summary = watch_summary(watch, token=token, ship_name=ship_name)

    print(f"REGISTER WATCH token={token[:12]}... ships={normalized_ships} summary={summary}")

    return jsonify({
        "ok": True,
        "registered": True,
        "registeredShips": len(normalized_ships),
        "ships": normalized_ships,
        **summary,
        "time": now_iso(),
    })


@app.post("/unregister-watch")
def unregister_watch():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    ships = payload.get("ships", [])

    if not token:
        return jsonify({"ok": False, "error": "token is required"}), 400
    if not isinstance(ships, list):
        return jsonify({"ok": False, "error": "ships must be a list"}), 400

    normalized_ships = unique_ship_list(ships)
    watch = read_json(WATCH_FILE, {})

    if normalized_ships:
        watch[token] = {
            "ships": normalized_ships,
            "updatedAt": now_iso(),
        }
    else:
        watch.pop(token, None)

    write_json(WATCH_FILE, watch)

    ship_name = str(payload.get("shipName", "")).strip()
    summary = watch_summary(watch, token=token, ship_name=ship_name)

    print(f"UNREGISTER WATCH token={token[:12]}... ships={normalized_ships} summary={summary}")

    return jsonify({
        "ok": True,
        "unregistered": True,
        "registeredShips": len(normalized_ships),
        "ships": normalized_ships,
        **summary,
        "time": now_iso(),
    })


@app.post("/watch-status")
def watch_status():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()

    watch = read_json(WATCH_FILE, {})
    my_ships = []
    if token and token in watch and isinstance(watch[token], dict):
        my_ships = unique_ship_list(watch[token].get("ships", []))

    summary = watch_summary(watch, token=token)
    return jsonify({
        "ok": True,
        "ships": my_ships,
        **summary,
        "time": now_iso(),
    })


def extract_ship_list_from_payload(payload: Dict[str, Any]) -> List[str]:
    """
    앱 버전에 따라 감시 선박 목록 키 이름이 달라질 수 있어 여러 이름을 허용합니다.
    예: ships, shipNames, watchShips, trackedShips
    """
    for key in ("ships", "shipNames", "watchShips", "trackedShips", "watchList"):
        values = payload.get(key)
        if isinstance(values, list):
            return unique_ship_list(values)
    return []


@app.route("/my-watch", methods=["GET", "POST"])
def my_watch():
    """
    모바일 앱 호환용 감시목록 엔드포인트입니다.

    현재 앱 로그에서 POST /my-watch 호출이 확인되었는데, 기존 서버에는 이 주소가 없어 404가 발생했습니다.
    이 엔드포인트는 다음 두 역할을 동시에 처리합니다.
    1) token만 오면 서버에 저장된 내 감시목록 조회
    2) token + ships 계열 목록이 같이 오면 서버 감시목록을 복구/동기화
    """
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    payload = request.get_json(silent=True) or {}
    token = (
        str(payload.get("token", "")).strip()
        or str(request.args.get("token", "")).strip()
    )

    watch = read_json(WATCH_FILE, {})
    if not isinstance(watch, dict):
        watch = {}

    incoming_ships = extract_ship_list_from_payload(payload)
    synced = False

    # Render 재배포 후 /tmp 데이터가 비어도 앱이 로컬 SharedPreferences의 선박목록을 보내주면 즉시 복구합니다.
    if token and incoming_ships:
        watch[token] = {
            "ships": incoming_ships,
            "updatedAt": now_iso(),
            "source": "my-watch-sync",
        }
        write_json(WATCH_FILE, watch)
        synced = True

    my_ships = []
    if token and token in watch and isinstance(watch[token], dict):
        my_ships = unique_ship_list(watch[token].get("ships", []))

    summary = watch_summary(watch, token=token)

    print(
        f"MY WATCH token={token[:12]}... incoming={incoming_ships} "
        f"synced={synced} myShips={my_ships} summary={summary}"
    )

    return jsonify({
        "ok": True,
        "synced": synced,
        "registered": synced,
        "ships": my_ships,
        "registeredShips": len(my_ships),
        **summary,
        "time": now_iso(),
    })


@app.get("/ais-state")
def ais_state():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    state = read_json(AIS_SHIP_STATE_FILE, {})
    return jsonify({
        "ok": True,
        "stateCount": len(state) if isinstance(state, dict) else 0,
        "state": state if isinstance(state, dict) else {},
        "time": now_iso(),
    })


@app.get("/alert-history")
def alert_history():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    history = read_json(EVENT_FILE, [])
    alert_state = read_json(AIS_ALERT_FILE, {})

    return jsonify({
        "ok": True,
        "historyCount": len(history),
        "recentHistory": history[:30] if isinstance(history, list) else [],
        "cooldownCount": len(alert_state) if isinstance(alert_state, dict) else 0,
        "cooldownState": alert_state if isinstance(alert_state, dict) else {},
        "cooldownMinutes": AIS_ALERT_COOLDOWN_MINUTES,
        "time": now_iso(),
    })


@app.post("/clear-alert-history")
def clear_alert_history():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401

    write_json(EVENT_FILE, [])
    write_json(AIS_ALERT_FILE, {})
    write_json(AIS_SHIP_STATE_FILE, {})

    return jsonify({
        "ok": True,
        "cleared": True,
        "message": "alert history, duplicate state, ais ship state cleared",
        "time": now_iso(),
    })


@app.get("/alerts/demo")
def alerts_demo():
    return jsonify({
        "ok": True,
        "alerts": [
            {
                "id": f"demo-{int(time.time() // 60)}",
                "type": "🌐",
                "title": "FCM 서버 연결 테스트",
                "body": "Render FCM 서버가 정상 응답했습니다.",
            }
        ]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
