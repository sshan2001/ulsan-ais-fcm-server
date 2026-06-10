import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from flask import Flask, jsonify, request
from flask_cors import CORS

try:
    import firebase_admin
    from firebase_admin import credentials, messaging
except Exception:  # Render build 전에 로컬 문법 확인용
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

SERVER_API_KEY = os.environ.get("SERVER_API_KEY", "").strip()
FIREBASE_SERVICE_ACCOUNT_JSON = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
FIREBASE_SERVICE_ACCOUNT_FILE = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE", "").strip()

firebase_ready = False
firebase_error = ""


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
    """
    watch 구조:
    {
      "FCM_TOKEN_A": {
        "ships": ["BOW EXCELLENCE", "KEOYOUNG SUN3"],
        "updatedAt": "..."
      }
    }
    """
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
        "watchDeviceCount": total_watch_devices,              # 감시 등록된 기기 수
        "totalWatchShips": len(total_watch_ships_set),        # 전체 서버에서 감시 중인 고유 선박 수
        "totalWatchLinks": total_watch_links,                 # 기기-선박 연결 총합
        "myWatchCount": my_watch_count,                       # 현재 휴대폰이 감시 중인 선박 수
        "shipWatchDeviceCount": ship_watch_device_count,      # 해당 선박을 감시 중인 기기 수
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
        "version": "2.9.8",
        "ok": True,
        "firebaseReady": firebase_ready,
        "firebaseError": firebase_error,
        "tokenCount": len(tokens),
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
        target_tokens = [str(item.get("token", "")).strip() for item in stored if str(item.get("token", "")).strip()]

    if not target_tokens:
        return jsonify({"ok": False, "error": "no tokens registered"}), 400

    success = 0
    errors = []
    for t in target_tokens:
        try:
            msg = messaging.Message(
                token=t,
                notification=messaging.Notification(title=title, body=body),
                data={
                    "source": "ulsan_ais_fcm_server",
                    "eventType": "test",
                    "sentAt": now_iso(),
                },
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

    history = read_json(EVENT_FILE, [])
    history.insert(0, {
        "type": "test",
        "title": title,
        "body": body,
        "success": success,
        "errors": errors[:5],
        "time": now_iso(),
    })
    write_json(EVENT_FILE, history[:200])
    return jsonify({"ok": True, "requested": len(target_tokens), "success": success, "errors": errors[:5]})
    
@app.post("/test-ship-alert")
def test_ship_alert():
    if not require_api_key():
        return jsonify({"ok": False, "error": "invalid api key"}), 401
    if not firebase_ready:
        return jsonify({"ok": False, "error": "firebase not ready", "firebaseError": firebase_error}), 500

    payload = request.get_json(silent=True) or {}
    ship_name = str(payload.get("shipName", "")).strip().upper()
    title = str(payload.get("title", f"🚢 {ship_name} 감시 알림"))
    body = str(payload.get("body", f"{ship_name} 테스트 선박 이벤트가 발생했습니다."))

    if not ship_name:
        return jsonify({"ok": False, "error": "shipName is required"}), 400

    watch = read_json(WATCH_FILE, {})
    target_tokens = []

    for token, item in watch.items():
        ships = unique_ship_list(item.get("ships", [])) if isinstance(item, dict) else []
        if ship_name in ships:
            target_tokens.append(token)

    if not target_tokens:
        return jsonify({
            "ok": False,
            "error": "no watchers for this ship",
            "shipName": ship_name,
            "targetCount": 0,
        }), 404

    success = 0
    errors = []

    for token in target_tokens:
        try:
            msg = messaging.Message(
                token=token,
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                data={
                    "source": "ulsan_ais_fcm_server",
                    "eventType": "ship_test_alert",
                    "shipName": ship_name,
                    "sentAt": now_iso(),
                },
                android=messaging.AndroidConfig(
                    priority="high",
                    notification=messaging.AndroidNotification(
                        sound="default",
                    ),
                ),
            )
            message_id = messaging.send(msg)
            print(f"SHIP ALERT FCM SUCCESS ship={ship_name} message={message_id}")
            success += 1
        except Exception as exc:
            print(f"SHIP ALERT FCM ERROR ship={ship_name} error={exc}")
            errors.append(str(exc))

    history = read_json(EVENT_FILE, [])
    history.insert(0, {
        "type": "ship_test_alert",
        "shipName": ship_name,
        "title": title,
        "body": body,
        "targetCount": len(target_tokens),
        "success": success,
        "errors": errors[:5],
        "time": now_iso(),
    })
    write_json(EVENT_FILE, history[:200])

    return jsonify({
        "ok": True,
        "shipName": ship_name,
        "targetCount": len(target_tokens),
        "success": success,
        "errors": errors[:5],
        "time": now_iso(),
    })

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


@app.get("/alerts/demo")
def alerts_demo():
    # 앱의 기존 서버 알림 즉시 확인 기능 테스트용입니다.
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
