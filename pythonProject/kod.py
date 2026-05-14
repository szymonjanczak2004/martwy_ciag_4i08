import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import cv2
import math
import time
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import mediapipe as mp

# =========================
# Opcjonalna synteza mowy
# =========================
try:
    import pyttsx3
    TTS_AVAILABLE = True
except Exception:
    TTS_AVAILABLE = False


# =========================
# Konfiguracja
# =========================
WINDOW_NAME = "Cyfrowy Trener - Martwy Ciag"
USE_VOICE = True and TTS_AVAILABLE
VOICE_COOLDOWN_SEC = 3.0

MIN_VISIBILITY = 0.55
SMOOTHING_ALPHA = 0.35

START_HIP_ANGLE_MIN = 65
START_HIP_ANGLE_MAX = 125

TOP_HIP_ANGLE_MIN = 155
TOP_KNEE_ANGLE_MIN = 150

ROUND_BACK_THRESHOLD = 145
LEAN_BACK_TOP_THRESHOLD = 195
KNEE_TOO_BENT_THRESHOLD = 105

HAND_TO_ANKLE_X_THRESHOLD = 0.11
HEAD_DOWN_THRESHOLD = 25

SESSION_DB = "deadlift_history.db"


# =========================
# Pomocnicze funkcje
# =========================
def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def angle_deg(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> float:
    ba = np.array([a[0] - b[0], a[1] - b[1]], dtype=np.float32)
    bc = np.array([c[0] - b[0], c[1] - b[1]], dtype=np.float32)

    norm_ba = np.linalg.norm(ba)
    norm_bc = np.linalg.norm(bc)

    if norm_ba < 1e-6 or norm_bc < 1e-6:
        return 0.0

    cosine = np.dot(ba, bc) / (norm_ba * norm_bc)
    cosine = clamp(float(cosine), -1.0, 1.0)
    return math.degrees(math.acos(cosine))


def midpoint(a: Tuple[float, float], b: Tuple[float, float]) -> Tuple[float, float]:
    return ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)


def draw_text_block(
        frame,
        lines: List[str],
        origin=(20, 30),
        line_height=28,
        color=(255, 255, 255),
        bg=(0, 0, 0),
        alpha=0.55,
):
    overlay = frame.copy()
    x, y = origin
    width = 700
    height = line_height * len(lines) + 20
    cv2.rectangle(overlay, (x - 10, y - 22), (x + width, y - 22 + height), bg, -1)
    cv2.addWeighted(overlay, alpha, frame, 1 - alpha, 0, frame)

    for i, line in enumerate(lines):
        cv2.putText(
            frame,
            line,
            (x, y + i * line_height),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            color,
            2,
            cv2.LINE_AA,
        )


# =========================
# Głos
# =========================
class VoiceAssistant:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and TTS_AVAILABLE
        self.last_spoken_at = 0.0
        self.last_message = ""
        self.engine = None

        if self.enabled:
            try:
                self.engine = pyttsx3.init()
                self.engine.setProperty("rate", 170)
                self.engine.setProperty("volume", 1.0)
            except Exception:
                self.enabled = False
                self.engine = None

    def say(self, message: str):
        if not self.enabled or self.engine is None:
            return

        now = time.time()
        if message == self.last_message and (now - self.last_spoken_at) < VOICE_COOLDOWN_SEC:
            return

        if (now - self.last_spoken_at) < 1.2:
            return

        self.last_message = message
        self.last_spoken_at = now

        def worker():
            try:
                self.engine.say(message)
                self.engine.runAndWait()
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()


# =========================
# Baza danych
# =========================
def init_db():
    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            reps_total INTEGER NOT NULL,
            reps_good INTEGER NOT NULL,
            reps_bad INTEGER NOT NULL,
            avg_score REAL NOT NULL,
            duration_sec REAL NOT NULL,
            source_type TEXT NOT NULL
        )
        """
    )
    conn.commit()

    # bezpieczna próba dodania kolumny dla starszej bazy
    try:
        cur.execute("ALTER TABLE sessions ADD COLUMN source_type TEXT NOT NULL DEFAULT 'unknown'")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    conn.close()


def save_session(reps_total: int, reps_good: int, reps_bad: int, avg_score: float, duration_sec: float, source_type: str):
    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sessions (created_at, reps_total, reps_good, reps_bad, avg_score, duration_sec, source_type)
        VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?)
        """,
        (reps_total, reps_good, reps_bad, avg_score, duration_sec, source_type),
    )
    conn.commit()
    conn.close()


# =========================
# Model stanu treningu
# =========================
@dataclass
class RepEvaluation:
    errors: List[str] = field(default_factory=list)
    score: float = 100.0

    def finalize(self):
        penalty = len(set(self.errors)) * 12
        self.score = clamp(100.0 - penalty, 0.0, 100.0)


@dataclass
class TrainerState:
    rep_count: int = 0
    good_reps: int = 0
    bad_reps: int = 0
    current_phase: str = "WAIT_START"
    feedback: str = "Wybierz zrodlo obrazu"
    rep_eval: Optional[RepEvaluation] = None
    session_scores: List[float] = field(default_factory=list)
    start_time: float = field(default_factory=time.time)


# =========================
# Główny trener
# =========================
class DeadliftTrainer:
    def __init__(self):
        self.mp_pose = mp.solutions.pose
        self.mp_draw = mp.solutions.drawing_utils

        self.pose = self.mp_pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            smooth_landmarks=True,
            enable_segmentation=False,
            min_detection_confidence=0.55,
            min_tracking_confidence=0.55,
        )

        self.voice = VoiceAssistant(enabled=USE_VOICE)
        self.state = TrainerState()
        self.prev_values = {}

    def smooth(self, key: str, value: float) -> float:
        if key not in self.prev_values:
            self.prev_values[key] = value
            return value
        smoothed = SMOOTHING_ALPHA * value + (1.0 - SMOOTHING_ALPHA) * self.prev_values[key]
        self.prev_values[key] = smoothed
        return smoothed

    def get_landmark_xyv(self, landmarks, idx: int):
        lm = landmarks[idx]
        return (lm.x, lm.y, lm.visibility)

    def choose_side(self, landmarks):
        left_ids = [
            self.mp_pose.PoseLandmark.LEFT_SHOULDER.value,
            self.mp_pose.PoseLandmark.LEFT_HIP.value,
            self.mp_pose.PoseLandmark.LEFT_KNEE.value,
            self.mp_pose.PoseLandmark.LEFT_ANKLE.value,
            self.mp_pose.PoseLandmark.LEFT_WRIST.value,
        ]
        right_ids = [
            self.mp_pose.PoseLandmark.RIGHT_SHOULDER.value,
            self.mp_pose.PoseLandmark.RIGHT_HIP.value,
            self.mp_pose.PoseLandmark.RIGHT_KNEE.value,
            self.mp_pose.PoseLandmark.RIGHT_ANKLE.value,
            self.mp_pose.PoseLandmark.RIGHT_WRIST.value,
        ]

        left_vis = np.mean([landmarks[i].visibility for i in left_ids])
        right_vis = np.mean([landmarks[i].visibility for i in right_ids])

        return "left" if left_vis >= right_vis else "right"

    def collect_points(self, landmarks):
        side = self.choose_side(landmarks)
        P = self.mp_pose.PoseLandmark

        if side == "left":
            shoulder = self.get_landmark_xyv(landmarks, P.LEFT_SHOULDER.value)
            hip = self.get_landmark_xyv(landmarks, P.LEFT_HIP.value)
            knee = self.get_landmark_xyv(landmarks, P.LEFT_KNEE.value)
            ankle = self.get_landmark_xyv(landmarks, P.LEFT_ANKLE.value)
            wrist = self.get_landmark_xyv(landmarks, P.LEFT_WRIST.value)
            ear = self.get_landmark_xyv(landmarks, P.LEFT_EAR.value)
        else:
            shoulder = self.get_landmark_xyv(landmarks, P.RIGHT_SHOULDER.value)
            hip = self.get_landmark_xyv(landmarks, P.RIGHT_HIP.value)
            knee = self.get_landmark_xyv(landmarks, P.RIGHT_KNEE.value)
            ankle = self.get_landmark_xyv(landmarks, P.RIGHT_ANKLE.value)
            wrist = self.get_landmark_xyv(landmarks, P.RIGHT_WRIST.value)
            ear = self.get_landmark_xyv(landmarks, P.RIGHT_EAR.value)

        min_vis = min(shoulder[2], hip[2], knee[2], ankle[2], wrist[2])
        visible = min_vis >= MIN_VISIBILITY

        points = {
            "side": side,
            "visible": visible,
            "shoulder": shoulder[:2],
            "hip": hip[:2],
            "knee": knee[:2],
            "ankle": ankle[:2],
            "wrist": wrist[:2],
            "ear": ear[:2],
        }
        return points

    def analyze_posture(self, pts) -> Tuple[dict, List[str]]:
        errors = []

        shoulder = pts["shoulder"]
        hip = pts["hip"]
        knee = pts["knee"]
        ankle = pts["ankle"]
        wrist = pts["wrist"]
        ear = pts["ear"]

        hip_angle = angle_deg(shoulder, hip, knee)
        knee_angle = angle_deg(hip, knee, ankle)
        back_angle = angle_deg(ear, shoulder, hip)
        head_angle = angle_deg(ear, shoulder, midpoint(hip, knee))
        hand_to_ankle_x = abs(wrist[0] - ankle[0])

        metrics = {
            "hip_angle": self.smooth("hip_angle", hip_angle),
            "knee_angle": self.smooth("knee_angle", knee_angle),
            "back_angle": self.smooth("back_angle", back_angle),
            "head_angle": self.smooth("head_angle", head_angle),
            "hand_to_ankle_x": self.smooth("hand_to_ankle_x", hand_to_ankle_x),
        }

        if metrics["back_angle"] < ROUND_BACK_THRESHOLD:
            errors.append("Zaokraglone plecy")

        if metrics["knee_angle"] < KNEE_TOO_BENT_THRESHOLD and metrics["hip_angle"] < 85:
            errors.append("Biodra za nisko")

        if metrics["hand_to_ankle_x"] > HAND_TO_ANKLE_X_THRESHOLD:
            errors.append("Prowadz rece blizej nog")

        if metrics["head_angle"] < HEAD_DOWN_THRESHOLD:
            errors.append("Nie opuszczaj zbyt mocno glowy")

        if self.state.current_phase == "TOP" and metrics["back_angle"] > LEAN_BACK_TOP_THRESHOLD:
            errors.append("Nie przeprostowuj sie u gory")

        return metrics, errors

    def update_state_machine(self, metrics: dict, current_errors: List[str]):
        hip_angle = metrics["hip_angle"]
        knee_angle = metrics["knee_angle"]

        in_start_position = START_HIP_ANGLE_MIN <= hip_angle <= START_HIP_ANGLE_MAX
        in_top_position = hip_angle >= TOP_HIP_ANGLE_MIN and knee_angle >= TOP_KNEE_ANGLE_MIN

        phase = self.state.current_phase

        if phase == "WAIT_START":
            self.state.feedback = "Ustaw pozycje startowa"
            if in_start_position:
                self.state.feedback = "Pozycja startowa wykryta - zacznij ruch"
                self.voice.say("Pozycja startowa wykryta")
                self.state.current_phase = "GOING_UP"
                self.state.rep_eval = RepEvaluation()

        elif phase == "GOING_UP":
            self.state.feedback = "Ruch w gore"
            if self.state.rep_eval is not None:
                self.state.rep_eval.errors.extend(current_errors)

            if in_top_position:
                self.state.current_phase = "TOP"
                self.state.feedback = "Pelny wyprost"

        elif phase == "TOP":
            self.state.feedback = "Kontrolowany powrot w dol"
            if self.state.rep_eval is not None:
                self.state.rep_eval.errors.extend(current_errors)

            if hip_angle < 135:
                self.state.current_phase = "GOING_DOWN"

        elif phase == "GOING_DOWN":
            self.state.feedback = "Odkladaj sztange z kontrola"
            if self.state.rep_eval is not None:
                self.state.rep_eval.errors.extend(current_errors)

            if in_start_position:
                self.finish_rep()
                self.state.current_phase = "GOING_UP"
                self.state.rep_eval = RepEvaluation()

    def finish_rep(self):
        self.state.rep_count += 1

        if self.state.rep_eval is None:
            self.state.rep_eval = RepEvaluation()

        self.state.rep_eval.finalize()
        score = self.state.rep_eval.score
        self.state.session_scores.append(score)

        uniq_errors = sorted(set(self.state.rep_eval.errors))
        if len(uniq_errors) == 0 and score >= 85:
            self.state.good_reps += 1
            self.state.feedback = f"Powtorzenie {self.state.rep_count} zaliczone"
            self.voice.say("Dobre powtorzenie")
        else:
            self.state.bad_reps += 1
            if uniq_errors:
                self.state.feedback = f"Powtorzenie {self.state.rep_count}: " + ", ".join(uniq_errors[:2])
                self.voice.say(uniq_errors[0])
            else:
                self.state.feedback = f"Powtorzenie {self.state.rep_count} z bledami"

    def draw_overlay(self, frame, metrics: dict, errors: List[str], source_label: str):
        avg_score = np.mean(self.state.session_scores) if self.state.session_scores else 0.0
        lines = [
            f"Zrodlo: {source_label}",
            f"Faza: {self.state.current_phase}",
            f"Powtorzenia: {self.state.rep_count}",
            f"Dobre: {self.state.good_reps} | Z bledami: {self.state.bad_reps}",
            f"Kat biodra: {metrics.get('hip_angle', 0):.1f}",
            f"Kat kolana: {metrics.get('knee_angle', 0):.1f}",
            f"Kat plecow: {metrics.get('back_angle', 0):.1f}",
            f"Feedback: {self.state.feedback}",
            f"Sredni wynik sesji: {avg_score:.1f}%",
        ]
        draw_text_block(frame, lines, origin=(20, 35), line_height=30)

        if errors:
            draw_text_block(
                frame,
                ["Bledy techniczne:"] + [f"- {e}" for e in sorted(set(errors))[:4]],
                origin=(20, 330),
                line_height=28,
                color=(255, 255, 255),
                bg=(40, 40, 180),
                alpha=0.6,
                )

        score = int(np.mean(self.state.session_scores[-5:])) if self.state.session_scores else 0
        bar_x, bar_y, bar_w, bar_h = 20, frame.shape[0] - 50, 300, 24
        cv2.rectangle(frame, (bar_x, bar_y), (bar_x + bar_w, bar_y + bar_h), (80, 80, 80), -1)
        fill = int(bar_w * clamp(score / 100.0, 0, 1))
        cv2.rectangle(frame, (bar_x, bar_y), (bar_x + fill, bar_y + bar_h), (0, 180, 0), -1)
        cv2.putText(
            frame,
            f"Jakosc: {score}%",
            (bar_x + 310, bar_y + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )

        cv2.putText(
            frame,
            "Q - wyjscie | R - reset sesji | SPACJA - pauza/wznow",
            (20, frame.shape[0] - 15),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (220, 220, 220),
            1,
            cv2.LINE_AA,
        )

    def reset_session(self):
        self.state = TrainerState()
        self.prev_values = {}

    def process_frame(self, frame, source_label: str):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        result = self.pose.process(rgb)

        metrics = {}
        errors = []

        if result.pose_landmarks:
            self.mp_draw.draw_landmarks(
                frame,
                result.pose_landmarks,
                self.mp_pose.POSE_CONNECTIONS,
                landmark_drawing_spec=self.mp_draw.DrawingSpec(color=(0, 255, 0), thickness=2, circle_radius=2),
                connection_drawing_spec=self.mp_draw.DrawingSpec(color=(255, 255, 0), thickness=2, circle_radius=2),
            )

            pts = self.collect_points(result.pose_landmarks.landmark)

            if pts["visible"]:
                metrics, errors = self.analyze_posture(pts)

                if errors:
                    priority = errors[0]
                    self.state.feedback = priority

                    if priority == "Zaokraglone plecy":
                        self.voice.say("Wyprostuj plecy")
                    elif priority == "Prowadz rece blizej nog":
                        self.voice.say("Prowadz rece blizej nog")
                    elif priority == "Biodra za nisko":
                        self.voice.say("Unies biodra nieco wyzej")

                self.update_state_machine(metrics, errors)
            else:
                self.state.feedback = "Pokaz cala sylwetke bokiem do kamery"
        else:
            self.state.feedback = "Nie wykryto sylwetki"

        self.draw_overlay(frame, metrics, errors, source_label)
        return frame


# =========================
# Wybór źródła obrazu
# =========================
def ask_video_source():
    print("\n=== WYBOR ZRODLA OBRAZU ===")
    print("1 - Kamera na zywo")
    print("2 - Wczesniej przygotowany film")

    while True:
        choice = input("Wpisz 1 albo 2: ").strip()

        if choice == "1":
            return {
                "type": "camera",
                "label": "kamera",
                "path": None
            }

        if choice == "2":
            while True:
                path = input("Podaj pelna sciezke do pliku wideo: ").strip().strip('"')
                if os.path.isfile(path):
                    return {
                        "type": "video_file",
                        "label": f"plik video: {os.path.basename(path)}",
                        "path": path
                    }
                print("Nie znaleziono pliku. Sprobuj ponownie.")

        print("Niepoprawny wybor. Wpisz 1 albo 2.")


def open_capture(source_info):
    if source_info["type"] == "camera":
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)
        return cap

    if source_info["type"] == "video_file":
        return cv2.VideoCapture(source_info["path"])

    return None


def main():
    init_db()

    source_info = ask_video_source()
    trainer = DeadliftTrainer()
    cap = open_capture(source_info)

    if cap is None or not cap.isOpened():
        print("Nie mozna otworzyc wybranego zrodla obrazu.")
        return

    paused = False
    session_started = time.time()

    while True:
        if not paused:
            ok, frame = cap.read()
            if not ok:
                if source_info["type"] == "video_file":
                    print("Koniec pliku wideo.")
                else:
                    print("Blad odczytu z kamery.")
                break

            # lustro tylko dla kamery
            if source_info["type"] == "camera":
                frame = cv2.flip(frame, 1)

            frame = trainer.process_frame(frame, source_info["label"])

        display_frame = frame.copy()

        if paused:
            cv2.putText(
                display_frame,
                "PAUZA",
                (50, 80),
                cv2.FONT_HERSHEY_SIMPLEX,
                2.0,
                (0, 0, 255),
                4,
                cv2.LINE_AA,
            )

        cv2.imshow(WINDOW_NAME, display_frame)
        key = cv2.waitKey(1) & 0xFF

        if key in (ord('q'), 27):
            break
        elif key == ord('r'):
            trainer.reset_session()
        elif key == ord(' '):
            paused = not paused

    duration = time.time() - session_started
    avg_score = float(np.mean(trainer.state.session_scores)) if trainer.state.session_scores else 0.0

    save_session(
        reps_total=trainer.state.rep_count,
        reps_good=trainer.state.good_reps,
        reps_bad=trainer.state.bad_reps,
        avg_score=avg_score,
        duration_sec=duration,
        source_type=source_info["type"]
    )

    cap.release()
    cv2.destroyAllWindows()

    print("\n=== PODSUMOWANIE SESJI ===")
    print(f"Zrodlo:      {source_info['label']}")
    print(f"Powtorzenia: {trainer.state.rep_count}")
    print(f"Dobre:       {trainer.state.good_reps}")
    print(f"Z bledami:   {trainer.state.bad_reps}")
    print(f"Sredni wynik:{avg_score:.1f}%")
    print(f"Czas sesji:  {duration:.1f}s")
    print(f"Dane zapisano do bazy: {SESSION_DB}")


if __name__ == "__main__":
    main()