import json, os, re, subprocess, sys, threading, time, webbrowser, requests
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs, urlencode
from flask import Flask, jsonify, Response, request

# ============================================================
#  INDSTILLINGER — juster disse
# ============================================================
FTP           = 190   # Opdater efter Half Monty
STREAM_SESSIONS = 10  # Antal cykelsessioner med fuld watt-stream

# ============================================================
#  KONSTANTER
# ============================================================
STRAVA_CONFIG  = "strava_config.json"
STRAVA_TOKENS  = "strava_tokens.json"
DATA_FILE      = "workouts.json"
SERVER_PORT    = 5000

STRAVA_BASE      = "https://www.strava.com/api/v3"
STRAVA_AUTH_URL  = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_REDIRECT  = "https://localhost"
STRAVA_SCOPES    = "activity:read_all"

UGEDAGE = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
TYPE_MAP = {
    "Ride": "Cykel - Outdoor", "VirtualRide": "Cykel - Indoor",
    "Run": "Lob - Outdoor", "TrailRun": "Lob - Trail",
    "Swim": "Svom", "Walk": "Gang", "WeightTraining": "Styrke",
}

# ============================================================
#  HJÆLPEFUNKTIONER
# ============================================================

def format_duration(s):
    if not s: return "?"
    s = float(s)
    return f"{int(s//3600)}t {int((s%3600)//60):02d}m"

# ============================================================
#  STRAVA AUTH
# ============================================================

def load_config():
    if not os.path.exists(STRAVA_CONFIG):
        print("Mangler strava_config.json"); sys.exit(1)
    with open(STRAVA_CONFIG) as f: return json.load(f)

def load_tokens():
    if not os.path.exists(STRAVA_TOKENS): return None
    with open(STRAVA_TOKENS) as f: return json.load(f)

def save_tokens(t):
    with open(STRAVA_TOKENS, "w") as f: json.dump(t, f, indent=2)

def get_token(config):
    t = load_tokens()
    if not t:
        params = {"client_id": config["client_id"], "redirect_uri": STRAVA_REDIRECT,
                  "response_type": "code", "scope": STRAVA_SCOPES, "approval_prompt": "auto"}
        webbrowser.open(f"{STRAVA_AUTH_URL}?{urlencode(params)}")
        print("Log ind i browser -> kopier URL -> paste her:")
        url = input("> ").strip()
        parsed = parse_qs(urlparse(url).query)
        if "code" not in parsed: raise RuntimeError("Ingen code i URL")
        r = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": config["client_id"], "client_secret": config["client_secret"],
            "code": parsed["code"][0], "grant_type": "authorization_code"})
        r.raise_for_status()
        t = r.json(); save_tokens(t)
        return t
    if time.time() > t.get("expires_at", 0) - 300:
        r = requests.post(STRAVA_TOKEN_URL, data={
            "client_id": config["client_id"], "client_secret": config["client_secret"],
            "refresh_token": t["refresh_token"], "grant_type": "refresh_token"})
        r.raise_for_status()
        t = r.json(); save_tokens(t)
    return t

def strava_get(endpoint, tokens, params=None):
    r = requests.get(f"{STRAVA_BASE}{endpoint}",
                     headers={"Authorization": f"Bearer {tokens['access_token']}"},
                     params=params)
    r.raise_for_status()
    return r.json()

# ============================================================
#  WATT ANALYSE
# ============================================================

def analyseer_stream(watts, varighed_sek):
    if not watts or len(watts) < 30: return ""
    sek_per_pkt = varighed_sek / len(watts) if watts else 1
    pkt_per_min = max(1, int(60 / sek_per_pkt))
    blokke = []
    for i in range(0, len(watts), pkt_per_min):
        seg = [w for w in watts[i:i+pkt_per_min] if w]
        if seg: blokke.append(round(sum(seg)/len(seg)))
    if not blokke: return ""

    def zone(w):
        p = w / FTP * 100
        if p < 56: return "Z1"
        if p < 76: return "Z2"
        if p < 90: return "Z3"
        if p < 105: return "Z4"
        if p < 120: return "Z5"
        return "Z6+"

    linjer = [f"  Watt per minut (FTP={FTP}W):"]
    linje = "  "
    for i, w in enumerate(blokke):
        linje += f"min{i+1:02d}:{w}W({zone(w)}) "
        if (i+1) % 6 == 0:
            linjer.append(linje.rstrip()); linje = "  "
    if linje.strip(): linjer.append(linje.rstrip())
    z = {}
    for w in blokke: z[zone(w)] = z.get(zone(w), 0) + 1
    ford = " | ".join(f"{k}: {round(v/len(blokke)*100)}%" for k,v in sorted(z.items()))
    linjer.append(f"  Zonefordeling: {ford}")
    return "\n".join(linjer)

def analyseer_lob_stream(velocity, heartrate, varighed_sek):
    if not velocity or len(velocity) < 30: return ""
    sek_per_pkt = varighed_sek / len(velocity) if velocity else 1
    pkt_per_min = max(1, int(60 / sek_per_pkt))

    blokke = []
    for i in range(0, len(velocity), pkt_per_min):
        v_seg = [v for v in velocity[i:i+pkt_per_min] if v]
        h_seg = [h for h in (heartrate[i:i+pkt_per_min] if heartrate else []) if h]
        if v_seg:
            avg_speed = sum(v_seg)/len(v_seg)
            pace_sek = 1000/avg_speed if avg_speed > 0 else 0
            avg_hr = round(sum(h_seg)/len(h_seg)) if h_seg else None
            blokke.append((pace_sek, avg_hr))

    if not blokke: return ""

    linjer = ["  Pace og puls per minut:"]
    linje = "  "
    for i, (pace, hr) in enumerate(blokke):
        p = f"{int(pace//60)}:{int(pace%60):02d}"
        hr_str = f"/{hr}bpm" if hr else ""
        linje += f"min{i+1:02d}:{p}{hr_str} "
        if (i+1) % 4 == 0:
            linjer.append(linje.rstrip()); linje = "  "
    if linje.strip(): linjer.append(linje.rstrip())
    return "\n".join(linjer)

# ============================================================
#  FORMAT AKTIVITET
# ============================================================

def format_aktivitet(a):
    try:
        dt = datetime.fromisoformat(a["start_date_local"].replace("Z",""))
        dato, ugedag = dt.strftime("%Y-%m-%d"), UGEDAGE[dt.weekday()]
    except:
        dato, ugedag = "?", "?"

    atype    = a.get("type","")
    duration = format_duration(a.get("moving_time",0))
    dist_km  = a.get("distance",0)/1000
    hr       = a.get("average_heartrate",0)
    cadence  = a.get("average_cadence",0)
    calories = a.get("calories",0)
    power    = a.get("average_watts",0)
    np       = a.get("weighted_average_watts",0)
    speed    = a.get("average_speed",0)
    navn     = a.get("name","")

    lines = [f"Dato: {dato} ({ugedag})", f"Type: {TYPE_MAP.get(atype, atype)}"]
    tid = f"Tid: {duration}"
    if dist_km > 0: tid += f" | Distance: {dist_km:.1f} km"
    lines.append(tid)

    if atype in ("Ride","VirtualRide") and power > 0:
        w = f"Avg: {power:.0f}W"
        if np > 0: w += f" | NP: {np:.0f}W"
        w += f" | {round(power/FTP*100)}% af FTP"
        lines.append(w)
    if atype in ("Run","TrailRun") and speed > 0:
        p = 1000/speed
        lines.append(f"Pace: {int(p//60)}:{int(p%60):02d}/km")
    if atype == "Swim" and speed > 0:
        p = 100/speed
        lines.append(f"Pace: {int(p//60)}:{int(p%60):02d}/100m")
    if hr > 0:
        puls = f"Avg HR: {hr:.0f} bpm"
        if cadence > 0:
            cad = cadence*2 if atype in ("Run","TrailRun") else cadence
            puls += f" | Kadence: {cad:.0f} {'spm' if atype in ('Run','TrailRun') else 'rpm'}"
        lines.append(puls)
    if calories > 0: lines.append(f"Kalorier: {calories:.0f} kcal")
    if navn: lines.append(f'Navn: "{navn}"')

    streams = a.get("_streams",{})
    if isinstance(streams, dict):
        watts_data = streams.get("watts",{}).get("data",[])
        vel_data   = streams.get("velocity_smooth",{}).get("data",[])
        hr_data    = streams.get("heartrate",{}).get("data",[])
    else:
        watts_data = vel_data = hr_data = []

    if watts_data and atype in ("Ride","VirtualRide"):
        profil = analyseer_stream(watts_data, a.get("moving_time",0))
        if profil: lines.append(profil)
    if vel_data and atype in ("Run","TrailRun"):
        profil = analyseer_lob_stream(vel_data, hr_data, a.get("moving_time",0))
        if profil: lines.append(profil)

    return "\n".join(lines)

# ============================================================
#  SYNC
# ============================================================

def load_data():
    if not os.path.exists(DATA_FILE): return None
    with open(DATA_FILE, encoding="utf-8") as f: return json.load(f)

def koor_sync():
    config = load_config()
    tokens = get_token(config)

    # Eksisterende data
    existing = load_data()
    alle_gamle = existing.get("aktiviteter", []) if existing else []
    gamle_ids  = {a["id"] for a in alle_gamle}
    nyeste_ts  = 0
    for a in alle_gamle:
        try:
            dt = datetime.fromisoformat(a.get("start_date","").replace("Z","+00:00"))
            ts = int(dt.timestamp())
            if ts > nyeste_ts: nyeste_ts = ts
        except: pass

    # Hent nye aktiviteter
    params = {"per_page": 30, "page": 1}
    if nyeste_ts > 0: params["after"] = nyeste_ts
    nye = []
    while True:
        batch = strava_get("/athlete/activities", tokens, params)
        if not batch: break
        batch = [a for a in batch if a["id"] not in gamle_ids]
        nye.extend(batch)
        if len(batch) < 30: break
        params["page"] += 1

    # Hent streams for nye cykel- og løbesessioner
    stream_keys = "watts,heartrate,cadence,velocity_smooth,time"
    for a in [x for x in nye if x.get("type") in ("Ride","VirtualRide","Run","TrailRun")][:STREAM_SESSIONS]:
        try:
            time.sleep(0.4)
            a["_streams"] = strava_get(f"/activities/{a['id']}/streams", tokens,
                {"keys": stream_keys, "key_by_type": "true"})
        except: a["_streams"] = {}

    # Sammensæt og sortér
    alle = nye + alle_gamle
    alle.sort(key=lambda a: a.get("start_date",""), reverse=True)

    # Sørg for streams på de 10 nyeste cykel- og løbesessioner
    stream_keys = "watts,heartrate,cadence,velocity_smooth,time"
    tael = 0
    for a in alle:
        if a.get("type") in ("Ride","VirtualRide","Run","TrailRun"):
            if tael < STREAM_SESSIONS:
                if not a.get("_streams"):
                    try:
                        time.sleep(0.4)
                        a["_streams"] = strava_get(f"/activities/{a['id']}/streams", tokens,
                            {"keys": stream_keys, "key_by_type": "true"})
                    except: a["_streams"] = {}
                tael += 1
            else:
                a.pop("_streams", None)

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"synced_at": datetime.now().isoformat(), "aktiviteter": alle},
                  f, indent=2, default=str, ensure_ascii=False)

    status = f"✅ Sync faerdig — {len(alle)} aktiviteter totalt"
    if nye: status += f" ({len(nye)} nye)"
    print(status)

# ============================================================
#  FLASK SERVER
# ============================================================

app = Flask(__name__)

@app.after_request
def headers(r):
    r.headers["ngrok-skip-browser-warning"] = "true"
    return r

def aggregate(aktiviteter, start):
    s = {"cykel":{"antal":0,"tid_min":0.0,"distance_km":0.0},
         "lob":  {"antal":0,"tid_min":0.0,"distance_km":0.0},
         "svom": {"antal":0,"tid_min":0.0,"distance_km":0.0},
         "andet":{"antal":0,"tid_min":0.0}}
    for a in aktiviteter:
        try:
            dt = datetime.fromisoformat(a.get("start_date_local","").replace("Z",""))
            if dt.replace(tzinfo=timezone.utc) < start: continue
        except: continue
        tid = a.get("moving_time",0)/60; dist = a.get("distance",0)/1000
        t = a.get("type","")
        if t in ("Ride","VirtualRide"):
            s["cykel"]["antal"]+=1; s["cykel"]["tid_min"]+=tid; s["cykel"]["distance_km"]+=dist
        elif t in ("Run","TrailRun"):
            s["lob"]["antal"]+=1; s["lob"]["tid_min"]+=tid; s["lob"]["distance_km"]+=dist
        elif t=="Swim":
            s["svom"]["antal"]+=1; s["svom"]["tid_min"]+=tid; s["svom"]["distance_km"]+=dist
        else:
            s["andet"]["antal"]+=1; s["andet"]["tid_min"]+=tid
    for k in s.values():
        k["tid_min"]=round(k["tid_min"])
        if "distance_km" in k: k["distance_km"]=round(k["distance_km"],1)
    return s

@app.route("/robots.txt")
def robots(): return Response("User-agent: *\nAllow: /", mimetype="text/plain")

@app.route("/workouts")
def workouts_endpoint():
    data = load_data()
    if not data: return jsonify({"fejl": "Ingen data"}), 404
    antal = min(int(request.args.get("antal", 30)), 200)
    linjer = ["=== Strava Traeningsdata ===",
              f"Sidst synkroniseret: {data.get('synced_at','?')}",
              f"FTP: {FTP}W", f"Antal: {min(antal, len(data['aktiviteter']))}", ""]
    for a in data["aktiviteter"][:antal]:
        linjer.append(format_aktivitet(a)); linjer.append("")
    return Response("\n".join(linjer), mimetype="text/plain; charset=utf-8")

@app.route("/workouts/raw")
def workouts_raw():
    data = load_data()
    if not data: return jsonify({"fejl": "Ingen data"}), 404
    return jsonify(data["aktiviteter"])

@app.route("/summary/week")
def week_summary():
    data = load_data()
    if not data: return jsonify({"fejl": "Ingen data"}), 404
    nu = datetime.now(tz=timezone.utc)
    start = (nu-timedelta(days=nu.weekday())).replace(hour=0,minute=0,second=0,microsecond=0)
    return jsonify({"fra": start.strftime("%Y-%m-%d"),
                    "discipliner": aggregate(data["aktiviteter"], start)})

@app.route("/summary/month")
def month_summary():
    data = load_data()
    if not data: return jsonify({"fejl": "Ingen data"}), 404
    nu = datetime.now(tz=timezone.utc)
    start = nu.replace(day=1,hour=0,minute=0,second=0,microsecond=0)
    return jsonify({"fra": start.strftime("%Y-%m-%d"),
                    "discipliner": aggregate(data["aktiviteter"], start)})

# ============================================================
#  CLOUDFLARE TUNNEL
# ============================================================

def find_cloudflared():
    for sti in [os.path.join(os.path.dirname(os.path.abspath(__file__)), "cloudflared.exe"),
                os.path.join(os.getcwd(), "cloudflared.exe"),
                r"C:\Users\TorBenZito\PyCharmMiscProject\Ironman\cloudflared.exe"]:
        if os.path.exists(sti): return sti
    return None

def start_cloudflare():
    cf = find_cloudflared()
    if not cf: return None
    try:
        proc = subprocess.Popen([cf, "tunnel", "--url", f"http://localhost:{SERVER_PORT}"],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        start = time.time()
        while time.time()-start < 25:
            line = proc.stdout.readline()
            if not line: break
            m = re.search(r"https://[\w-]+\.trycloudflare\.com", line)
            if m: return m.group(0)
    except: pass
    return None

# ============================================================
#  HOVED
# ============================================================

def main():
    koor_sync()

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    threading.Thread(target=lambda: app.run(port=SERVER_PORT, host="0.0.0.0",
                                            debug=False, use_reloader=False), daemon=True).start()
    time.sleep(2)
    print("✅ Server korer")

    url = start_cloudflare()
    if url:
        print(f"✅ Tunnel aaben\n\n📋 Send til Claude: {url}/workouts")
    else:
        print("❌ Ingen tunnel. Brug: http://localhost:5000/workouts")
    print("\nTryk Ctrl+C for at stoppe.\n")

    try:
        while True:
            time.sleep(3600)
            koor_sync()
    except KeyboardInterrupt:
        print("\nStoppet.")

if __name__ == "__main__":
    main()