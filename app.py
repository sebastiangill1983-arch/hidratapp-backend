import json
import os
import threading
import time
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS
from pywebpush import webpush, WebPushException

app = Flask(__name__)
CORS(app)  # permite que la PWA en GitHub Pages llame a este servidor

DATA_FILE = os.path.join(os.path.dirname(__file__), "subscriptions.json")

VAPID_PRIVATE_KEY_FILE = os.path.join(os.path.dirname(__file__), "private_key.pem")
VAPID_PUBLIC_KEY = "BDToAnFgEtg6lCIJEMlu0idHQ8humRyuGN2Pp2XpcCdfhoJThutetY-Q8Vd0QOX0AxeEGMKM-jU2EPnnBWfCHMg"
VAPID_CLAIMS = {"sub": "mailto:sebastian@example.com"}

_lock = threading.Lock()


def load_subscriptions():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_subscriptions(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


@app.route("/vapid-public-key", methods=["GET"])
def vapid_public_key():
    return jsonify({"publicKey": VAPID_PUBLIC_KEY})


@app.route("/subscribe", methods=["POST"])
def subscribe():
    """
    El cliente manda:
    {
      "subscription": {...objeto de PushManager...},
      "settings": {
        "name": "sebastian",
        "goalMl": 2600,
        "startHour": "09:00",
        "endHour": "18:00",
        "intervalMinutes": 60
      }
    }
    Se guarda usando el endpoint de la suscripción como clave única.
    """
    body = request.get_json(force=True)
    subscription = body.get("subscription")
    settings = body.get("settings")

    if not subscription or not settings:
        return jsonify({"error": "faltan datos"}), 400

    with _lock:
        subs = load_subscriptions()
        key = subscription["endpoint"]
        subs[key] = {
            "subscription": subscription,
            "settings": settings,
            "last_sent_minute": None,  # evita mandar 2 veces en el mismo minuto
        }
        save_subscriptions(subs)

    return jsonify({"ok": True})


@app.route("/unsubscribe", methods=["POST"])
def unsubscribe():
    body = request.get_json(force=True)
    endpoint = body.get("endpoint")
    with _lock:
        subs = load_subscriptions()
        subs.pop(endpoint, None)
        save_subscriptions(subs)
    return jsonify({"ok": True})


def is_within_work_hours(now, start_hour, end_hour):
    sh, sm = map(int, start_hour.split(":"))
    eh, em = map(int, end_hour.split(":"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def scheduler_loop():
    """Corre en un thread aparte, revisa cada 60s si hay que mandar un recordatorio."""
    while True:
        try:
            now = datetime.now()
            with _lock:
                subs = load_subscriptions()
                changed = False

                for key, entry in list(subs.items()):
                    settings = entry["settings"]
                    if not is_within_work_hours(now, settings["startHour"], settings["endHour"]):
                        continue

                    interval = int(settings.get("intervalMinutes", 60))
                    minutes_since_midnight = now.hour * 60 + now.minute
                    should_send = minutes_since_midnight % interval == 0

                    already_sent_this_minute = entry.get("last_sent_minute") == minutes_since_midnight

                    if should_send and not already_sent_this_minute:
                        ok = send_push(entry["subscription"], settings)
                        if not ok:
                            del subs[key]  # suscripción rota/vencida: la sacamos
                        else:
                            entry["last_sent_minute"] = minutes_since_midnight
                        changed = True

                if changed:
                    save_subscriptions(subs)
        except Exception as e:
            print("Error en scheduler:", e)

        time.sleep(30)


def send_push(subscription, settings):
    name = settings.get("name", "")
    title = "💧 Hora de hidratarte"
    body = f"{name}, no te olvides de tomar agua." if name else "No te olvides de tomar agua."

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body}),
            vapid_private_key=VAPID_PRIVATE_KEY_FILE,
            vapid_claims=dict(VAPID_CLAIMS),
        )
        return True
    except WebPushException as e:
        print("Push falló (probablemente suscripción vencida):", repr(e))
        return False


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# Arranca el scheduler en un thread de background al iniciar la app
threading.Thread(target=scheduler_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
