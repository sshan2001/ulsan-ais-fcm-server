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

AIS_ALERT_COOLDOWN_MINUTES = int(os.environ.get("AIS_ALERT_COOLDOWN_MINUTES", "30"))
AUTO_CHECK_ENABLED = os.environ.get("AUTO_CHECK_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
AUTO_CHECK_INTERVAL_SECONDS = int(os.environ.get("AUTO_CHECK_INTERVAL_SECONDS", "300"))

SERVER_API_KEY = os.environ.get("SERVER_API_KEY", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()

firebase_ready = False
firebase_error = ""

auto_checker_started = False
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


def should_send_ais_alert(ship_name: str, event_type: str, force: bool = False) -> tuple[bool, str]:
    """
    같은 선박의 같은 이벤트가 너무 자주 반복 발송되지 않도록 막습니다.
    force=True이면 중복 방지를 무시하고 테스트 발송합니다.
    """
    if force:
        return True, "forced"

    key = f"{normalize_ship_name(ship_name)}::{event_type}"
    state = read_json(AIS_ALERT_FILE, {})
    now = datetime.now(timezone.utc)

    previous_raw = state.get(key, {}).get("sentAt") if isinstance(state.get(key), dict) else None
    previous_dt = parse_iso_time(previous_raw) if previous_raw else None

    if previous_dt is not None:
        diff_minutes = (now - previous_dt).total_seconds() / 60
        if diff_minutes < AIS_ALERT_COOLDOWN_MINUTES:
            remain = max(1, int(AIS_ALERT_COOLDOWN_MINUTES - diff_minutes))
            return False, f"cooldown {remain}min remaining"

    state[key] = {
        "shipName": normalize_ship_name(ship_name),
        "eventType": event_type,
        "sentAt": now.isoformat(),
    }

    # 오래된 기록 정리: 24시간 지난 중복방지 기록 삭제
    cleaned = {}
    for item_key, item in state.items():
        sent_at = parse_iso_time(item.get("sentAt")) if isinstance(item, dict) else None
        if sent_at is not None and (now - sent_at).total_seconds() <= 24 * 3600:
            cleaned[item_key] = item

    write_json(AIS_ALERT_FILE, cleaned)
    return True, "ok"


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


@app.get("/")
def index():
    tokens = read_json(TOKENS_FILE, [])
    watch = read_json(WATCH_FILE, {})
    summary = watch_summary(watch)
    return jsonify({
        "service": "ulsan-ais-fcm-server",
        "version": "2.9.12",
        "ok": True,
        "firebaseReady": firebase_ready,
        "firebaseError": firebase_error,
        "tokenCount": len(tokens),
        "autoCheckEnabled": AUTO_CHECK_ENABLED,
        "autoCheckIntervalSeconds": AUTO_CHECK_INTERVAL_SECONDS,
        "autoCheckStarted": auto_checker_started,
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



def perform_ais_check_once(force: bool = False, source: str = "manual") -> Dict[str, Any]:
    """
    UPA AIS를 1회 조회하고, watchlist에 등록된 선박이 감지되면 해당 사용자에게만 FCM을 발송합니다.
    force=True이면 중복방지 쿨다운을 무시합니다.
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
        return {
            "ok": False,
            "error": "no watched ships",
            "watchedCount": 0,
            "source": source,
            "time": now_iso(),
        }

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
            detected_ships[name] = {
                "name": name,
                "mmsi": pick_text(item, ["aisMmsi", "mmsi", "MMSI", "mmsiNo"], "-"),
                "status": pick_text(item, ["oprtlSttsKr", "oprtlStts", "status", "navStatus", "nvgtStts", "state"], "-"),
                "speed": pick_float(item, ["sog", "speed", "spd", "knots"], 0.0),
                "lat": pick_float(item, ["lat", "latitude", "la", "vslLat"], 0.0),
                "lon": pick_float(item, ["lot", "lon", "lng", "longitude", "vslLot"], 0.0),
                "destination": pick_text(item, ["destination", "dest", "dstn", "etaDest"], "-"),
                "eta": pick_text(item, ["eta", "etaTime", "arrvTm"], "-"),
            }

    sent = []
    skipped = []
    errors = []

    for ship_name, info in detected_ships.items():
        target_tokens = tokens_for_ship(ship_name)
        if not target_tokens:
            continue

        can_send, reason = should_send_ais_alert(ship_name, "ais_detected", force=force)
        if not can_send:
            skipped.append({"shipName": ship_name, "reason": reason})
            continue

        title = f"🚢 {ship_name} AIS 감지"
        body = f"{ship_name} 선박이 울산항 AIS에서 감지되었습니다."

        result = send_fcm_to_tokens(
            target_tokens,
            title,
            body,
            {
                "source": "ulsan_ais_fcm_server",
                "eventType": "ais_detected_auto" if source == "auto" else "ais_detected_once",
                "shipName": ship_name,
                "status": str(info.get("status", "-")),
                "speed": str(info.get("speed", 0.0)),
                "sentAt": now_iso(),
            },
        )

        if result["success"] > 0:
            sent.append(ship_name)
        if result["errors"]:
            errors.extend(result["errors"])

    result_payload = {
        "ok": True,
        "source": source,
        "watchedCount": len(watched_ships),
        "aisCount": len(ais_list),
        "detectedCount": len(detected_ships),
        "detectedShips": sorted(detected_ships.keys()),
        "sentShips": sent,
        "skippedShips": skipped,
        "force": force,
        "cooldownMinutes": AIS_ALERT_COOLDOWN_MINUTES,
        "errors": errors[:5],
        "time": now_iso(),
    }

    history = read_json(EVENT_FILE, [])
    history.insert(0, {
        "type": "check_ais_auto" if source == "auto" else "check_ais_once",
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


def start_auto_checker() -> None:
    global auto_checker_started

    if auto_checker_started:
        return

    if not AUTO_CHECK_ENABLED:
        print("AUTO CHECKER DISABLED")
        return

    auto_checker_started = True
    thread = threading.Thread(target=auto_checker_loop, daemon=True)
    thread.start()
    print("AUTO CHECKER THREAD STARTED")


start_auto_checker()


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
