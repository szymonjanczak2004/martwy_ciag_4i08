import os
import cv2
import time
import numpy as np
import sqlite3
from flask import Flask, render_template, Response, jsonify, request
from werkzeug.utils import secure_filename
from kod import DeadliftTrainer, SESSION_DB, init_db, save_session, save_session_reps

app = Flask(__name__)

# Konfiguracja folderu na przesłane filmy
UPLOAD_FOLDER = 'uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# Inicjalizacja globalna
vision_trainer = DeadliftTrainer()
cap = None
# Zmieniamy domyślny start na 'None' (pusty ekran, czeka na akcję)
current_source = None
last_saved_rep_count = 0

# ==========================================
# BAZA DANYCH - HISTORIA TRENINGÓW
# ==========================================
def init_training_db():
    init_db()
    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS manual_training_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            total_sets INTEGER,
            total_reps INTEGER,
            avg_weight REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS manual_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            block_id INTEGER,
            weight REAL,
            reps INTEGER,
            FOREIGN KEY(block_id) REFERENCES manual_training_blocks(id)
        )
    """)
    conn.commit()
    conn.close()

init_training_db()

ERROR_TIPS = {
    "Zaokraglone plecy": "Napnij brzuch i ustaw neutralny kregoslup, barki trzymaj nad sztanga.",
    "Biodra za nisko": "Podnies biodra odrobine wyzej przed oderwaniem sztangi od podlogi.",
    "Prowadz rece blizej nog": "Prowadz sztange blisko piszczeli i ud przez caly ruch.",
    "Nie opuszczaj zbyt mocno glowy": "Patrz przed siebie, utrzymuj szyje w neutralnej pozycji.",
    "Nie przeprostowuj sie u gory": "Na gorze zatrzymaj wyprost bez odchylania tulowia do tylu.",
    "Sztanga ucieka od pionu": "Prowadz sztange blizej linii srodka ciezkosci, unikaj odchylenia na boki.",
}


def build_improvement_summary():
    reps = vision_trainer.state.rep_results
    counts = {}
    for rep in reps:
        for err in rep.errors:
            counts[err] = counts.get(err, 0) + 1

    top_errors = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    summary = []
    for err, count in top_errors[:3]:
        summary.append({
            "error": err,
            "count": count,
            "tip": ERROR_TIPS.get(err, "Powtorz ruch wolniej i utrzymuj stabilna pozycje."),
        })

    return summary


def persist_current_ai_session_if_needed():
    global last_saved_rep_count

    reps_total = vision_trainer.state.rep_count
    if reps_total <= 0 or reps_total == last_saved_rep_count:
        return

    duration = max(0.0, time.time() - vision_trainer.state.start_time)
    avg_score = float(np.mean(vision_trainer.state.session_scores)) if vision_trainer.state.session_scores else 0.0
    session_id = save_session(
        reps_total=vision_trainer.state.rep_count,
        reps_good=vision_trainer.state.good_reps,
        reps_bad=vision_trainer.state.bad_reps,
        avg_score=avg_score,
        duration_sec=duration,
        source_type="flask_stream",
    )
    if vision_trainer.state.rep_results:
        save_session_reps(session_id, vision_trainer.state.rep_results)
    last_saved_rep_count = reps_total

# ==========================================
# STRUMIENIOWANIE OBRAZU I AI
# ==========================================
def get_stream():
    global cap, vision_trainer, current_source
    file_read_failures = 0
    max_file_read_failures = 25
    auto_video_started = False
    last_source_snapshot = None

    while True:
        source_snapshot = current_source
        if source_snapshot != last_source_snapshot:
            auto_video_started = False
            file_read_failures = 0
            last_source_snapshot = source_snapshot

        # 1. STAN BEZCZYNNOŚCI (pusty ekran, gdy nie ma wideo/kamery)
        if current_source is None:
            file_read_failures = 0
            # Generowanie czarnej klatki
            frame = np.zeros((480, 640, 3), dtype=np.uint8)
            # Rysowanie wyśrodkowanego napisu instruktażowego
            cv2.putText(frame, "Wlacz kamere lub", (150, 220), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(frame, "przeslij plik...", (200, 270), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2, cv2.LINE_AA)
            ret, buffer = cv2.imencode('.jpg', frame)

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
            time.sleep(0.1) # Opóźnienie, by nie obciążać procesora
            continue

        # 2. PRÓBA OTWARCIA ŹRÓDŁA
        if cap is None or not cap.isOpened():
            cap = cv2.VideoCapture(current_source)
            if not cap.isOpened():
                current_source = None
                cap = None
                continue

        # 3. ODCZYT KLATKI
        success, frame = cap.read()
        if not success:
            if isinstance(current_source, str):
                frame_count = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                current_pos = cap.get(cv2.CAP_PROP_POS_FRAMES) or 0
                reached_end = frame_count > 0 and current_pos >= (frame_count - 1)

                # Przy krótkim błędzie odczytu pliku nie kasuj od razu źródła.
                if reached_end:
                    if auto_video_started and vision_trainer.state.mode == "RECORDING":
                        vision_trainer.stop_series()
                        persist_current_ai_session_if_needed()
                    cap.release()
                    cap = None
                    current_source = None
                    auto_video_started = False
                    file_read_failures = 0
                else:
                    file_read_failures += 1
                    if file_read_failures >= max_file_read_failures:
                        if auto_video_started and vision_trainer.state.mode == "RECORDING":
                            vision_trainer.stop_series()
                            persist_current_ai_session_if_needed()
                        cap.release()
                        cap = None
                        current_source = None
                        auto_video_started = False
                        file_read_failures = 0
                    time.sleep(0.02)
            else:
                time.sleep(0.01)
            continue
        file_read_failures = 0

        # Lustro tylko dla kamery na żywo
        if current_source == 0:
            frame = cv2.flip(frame, 1)
        elif isinstance(current_source, str) and not auto_video_started:
            vision_trainer.start_series()
            auto_video_started = True

        processed = vision_trainer.process_frame(frame, "Live" if current_source == 0 else "Video File")
        ret, buffer = cv2.imencode('.jpg', processed)

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

# ==========================================
# ENDPOINTY (ŚCIEŻKI) APLIKACJI
# ==========================================
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video_feed')
def video_feed():
    return Response(get_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/stats')
def stats():
    rep_details = []
    for rep in vision_trainer.state.rep_results:
        rep_details.append({
            "index": rep.index,
            "score": round(rep.score, 1),
            "duration_sec": round(rep.duration_sec, 2),
            "errors": rep.errors,
            "tips": [ERROR_TIPS.get(e, "Popraw technike i utrzymuj kontrole ruchu.") for e in rep.errors],
            "status": "good" if (not rep.errors and rep.score >= 85) else "bad",
        })

    return jsonify({
        "reps": vision_trainer.state.rep_count,
        "good_reps": vision_trainer.state.good_reps,
        "bad_reps": vision_trainer.state.bad_reps,
        "mode": vision_trainer.state.mode,
        "phase": vision_trainer.state.current_phase,
        "feedback": vision_trainer.state.feedback,
        "rep_details": rep_details,
        "improvement_summary": build_improvement_summary(),
    })


@app.route('/api/control', methods=['POST'])
def control_analysis():
    action = (request.json or {}).get("action", "").strip().lower()

    if action == "start":
        vision_trainer.command_queue.put("start")
        return jsonify({"status": "ok", "message": "Rozpoczeto serie"})

    if action == "stop":
        vision_trainer.command_queue.put("stop")
        persist_current_ai_session_if_needed()
        return jsonify({"status": "ok", "message": "Zatrzymano i przechodze do analizy"})

    return jsonify({"status": "error", "message": "Nieznana akcja"}), 400

@app.route('/set_live', methods=['POST'])
def set_live():
    global current_source, cap, vision_trainer
    if cap: cap.release()
    current_source = 0
    vision_trainer.calibration_enabled = True
    vision_trainer.reset_session()
    return jsonify({"status": "ok"})

@app.route('/upload_video', methods=['POST'])
def upload_video():
    global current_source, cap, vision_trainer
    if 'file' not in request.files:
        return jsonify({"error": "Brak pliku"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Nie wybrano pliku"}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    if cap: cap.release()
    current_source = filepath
    vision_trainer.calibration_enabled = False
    vision_trainer.reset_session()

    return jsonify({"status": "ok", "filename": filename})

# Twardy reset z poziomu przeglądarki (uruchamiany m.in. przy odświeżeniu strony)
@app.route('/stop_feed', methods=['POST'])
def stop_feed():
    global current_source, cap, vision_trainer
    persist_current_ai_session_if_needed()
    if cap: cap.release()
    cap = None
    current_source = None
    vision_trainer.calibration_enabled = True
    vision_trainer.reset_session()
    return jsonify({"status": "ok"})

@app.route('/api/save_training_block', methods=['POST'])
def save_training_block():
    data = request.json
    sets = data.get('sets', [])
    if not sets: return jsonify({"error": "Brak danych"}), 400

    total_reps = sum(int(s['reps']) for s in sets)
    avg_weight = sum(float(s['weight']) for s in sets) / len(sets)

    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    cur.execute("INSERT INTO manual_training_blocks (date, total_sets, total_reps, avg_weight) VALUES (datetime('now', 'localtime'), ?, ?, ?)",
                (len(sets), total_reps, avg_weight))
    block_id = cur.lastrowid

    for s in sets:
        cur.execute("INSERT INTO manual_sets (block_id, weight, reps) VALUES (?, ?, ?)",
                    (block_id, s['weight'], s['reps']))

    conn.commit()
    conn.close()
    return jsonify({"status": "success"})

@app.route('/api/history')
def get_history():
    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    cur.execute("SELECT date, total_sets, total_reps, avg_weight FROM manual_training_blocks ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()

    history = []
    for r in rows:
        history.append({
            "date": r[0],
            "sets": r[1],
            "reps": r[2],
            "weight": round(r[3], 1)
        })
    return jsonify(history)

if __name__ == '__main__':
    app.run(debug=True, threaded=True)