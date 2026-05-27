import os
import cv2
import time
import numpy as np
import sqlite3
from flask import Flask, render_template, Response, jsonify, request
from werkzeug.utils import secure_filename
from kod import DeadliftTrainer, SESSION_DB

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

# ==========================================
# BAZA DANYCH - HISTORIA TRENINGÓW
# ==========================================
def init_training_db():
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

# ==========================================
# STRUMIENIOWANIE OBRAZU I AI
# ==========================================
def get_stream():
    global cap, vision_trainer, current_source
    while True:
        # 1. STAN BEZCZYNNOŚCI (pusty ekran, gdy nie ma wideo/kamery)
        if current_source is None:
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
                continue

        # 3. ODCZYT KLATKI
        success, frame = cap.read()
        if not success:
            if isinstance(current_source, str):
                # Jeśli to był plik i się skończył -> Wróć do stanu bezczynności
                cap.release()
                cap = None
                current_source = None
                vision_trainer.reset_session()
            continue

        # Lustro tylko dla kamery na żywo
        if current_source == 0:
            frame = cv2.flip(frame, 1)

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
    return jsonify({
        "reps": vision_trainer.state.rep_count,
        "good_reps": vision_trainer.state.good_reps,
        "phase": vision_trainer.state.current_phase,
        "feedback": vision_trainer.state.feedback
    })

@app.route('/set_live', methods=['POST'])
def set_live():
    global current_source, cap, vision_trainer
    if cap: cap.release()
    current_source = 0
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
    vision_trainer.reset_session()

    return jsonify({"status": "ok", "filename": filename})

# Twardy reset z poziomu przeglądarki (uruchamiany m.in. przy odświeżeniu strony)
@app.route('/stop_feed', methods=['POST'])
def stop_feed():
    global current_source, cap, vision_trainer
    if cap: cap.release()
    cap = None
    current_source = None
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