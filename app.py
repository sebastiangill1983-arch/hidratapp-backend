import json
import os
import threading
import time
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from pywebpush import webpush, WebPushException

app = Flask(__name__)
CORS(app)  # permite que la PWA en GitHub Pages llame a este servidor

VAPID_PRIVATE_KEY_FILE = os.path.join(os.path.dirname(__file__), "private_key.pem")
VAPID_PUBLIC_KEY = "BDToAnFgEtg6lCIJEMlu0idHQ8humRyuGN2Pp2XpcCdfhoJThutetY-Q8Vd0QOX0AxeEGMKM-jU2EPnnBWfCHMg"
VAPID_CLAIMS = {"sub": "mailto:sebastian@example.com"}

# Zona horaria de referencia para calcular "ahora" y comparar contra startHour/endHour.
# Render corre los servidores en UTC, así que sin esto el scheduler comparaba mal.
ARG_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

# ---------- Supabase (guarda las suscripciones; sobrevive a que el server se reinicie) ----------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

SB_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def sb_load_all():
    """Devuelve un dict {endpoint: {subscription, settings, last_sent_minute}}."""
    res = requests.get(f"{SUPABASE_URL}/rest/v1/subscriptions?select=*", headers=SB_HEADERS, timeout=15)
    res.raise_for_status()
    rows = res.json()
    return {row["endpoint"]: row for row in rows}


def sb_upsert(endpoint, subscription, settings):
    body = [{
        "endpoint": endpoint,
        "subscription": subscription,
        "settings": settings,
        "last_sent_minute": None,
    }]
    headers = dict(SB_HEADERS)
    headers["Prefer"] = "resolution=merge-duplicates"
    res = requests.post(f"{SUPABASE_URL}/rest/v1/subscriptions", headers=headers, json=body, timeout=15)
    res.raise_for_status()


def sb_update_last_sent(endpoint, minute):
    res = requests.patch(
        f"{SUPABASE_URL}/rest/v1/subscriptions?endpoint=eq.{quote(endpoint, safe='')}",
        headers=SB_HEADERS,
        json={"last_sent_minute": minute},
        timeout=15,
    )
    res.raise_for_status()


def sb_delete(endpoint):
    res = requests.delete(
        f"{SUPABASE_URL}/rest/v1/subscriptions?endpoint=eq.{quote(endpoint, safe='')}",
        headers=SB_HEADERS,
        timeout=15,
    )
    res.raise_for_status()


_lock = threading.Lock()


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
    """
    body = request.get_json(force=True)
    subscription = body.get("subscription")
    settings = body.get("settings")

    if not subscription or not settings:
        return jsonify({"error": "faltan datos"}), 400

    try:
        sb_upsert(subscription["endpoint"], subscription, settings)
    except requests.RequestException as e:
        print("Error guardando en Supabase:", repr(e))
        return jsonify({"error": "no se pudo guardar"}), 500

    return jsonify({"ok": True})


@app.route("/unsubscribe", methods=["POST"])
def unsubscribe():
    body = request.get_json(force=True)
    endpoint = body.get("endpoint")
    try:
        sb_delete(endpoint)
    except requests.RequestException as e:
        print("Error borrando en Supabase:", repr(e))
    return jsonify({"ok": True})


def is_within_work_hours(now, start_hour, end_hour):
    sh, sm = map(int, start_hour.split(":"))
    eh, em = map(int, end_hour.split(":"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def scheduler_loop():
    """Corre en un thread aparte, revisa cada 30s si hay que mandar un recordatorio."""
    while True:
        try:
            # OJO: Render corre en UTC. Usamos la zona horaria de Argentina para que
            # startHour/endHour se comparen contra la hora local real del usuario.
            now = datetime.now(ARG_TZ)
            print(f"[scheduler] tick {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")

            with _lock:
                subs = sb_load_all()
                print(f"[scheduler] {len(subs)} suscripcion(es) cargada(s)")

                for endpoint, entry in subs.items():
                    settings = entry["settings"]
                    short_id = endpoint[-12:]  # solo para identificar en el log sin ensuciarlo

                    # El usuario puede apagar los recordatorios desde la app (botón on/off).
                    # Por compatibilidad con suscripciones guardadas antes de este cambio,
                    # si el campo no está presente asumimos que está activado.
                    if settings.get("remindersEnabled") is False:
                        print(f"[scheduler] {short_id}: recordatorios pausados por el usuario")
                        continue

                    within_hours = is_within_work_hours(now, settings["startHour"], settings["endHour"])
                    if not within_hours:
                        print(
                            f"[scheduler] {short_id}: fuera de horario "
                            f"({settings['startHour']}-{settings['endHour']}, ahora {now.strftime('%H:%M')})"
                        )
                        continue

                    interval = int(settings.get("intervalMinutes", 60))
                    minutes_since_midnight = now.hour * 60 + now.minute
                    should_send = minutes_since_midnight % interval == 0
                    already_sent_this_minute = entry.get("last_sent_minute") == minutes_since_midnight

                    print(
                        f"[scheduler] {short_id}: dentro de horario, "
                        f"interval={interval}min, should_send={should_send}, "
                        f"already_sent={already_sent_this_minute}"
                    )

                    if should_send and not already_sent_this_minute:
                        print(f"[scheduler] {short_id}: enviando push...")
                        ok = send_push(entry["subscription"], settings)
                        if not ok:
                            print(f"[scheduler] {short_id}: suscripción rota/vencida, se elimina")
                            sb_delete(endpoint)
                        else:
                            print(f"[scheduler] {short_id}: push enviado OK")
                            sb_update_last_sent(endpoint, minutes_since_midnight)
        except Exception as e:
            print("Error en scheduler:", repr(e))

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
