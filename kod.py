import os
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"

import cv2
import math
import time
import sqlite3
import threading
import queue
import json
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

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

START_EXIT_HIP_ANGLE = START_HIP_ANGLE_MAX + 8
START_REENTER_HIP_ANGLE = START_HIP_ANGLE_MAX - 4
TOP_EXIT_HIP_ANGLE = TOP_HIP_ANGLE_MIN - 10
START_HOLD_FRAMES_TO_CONFIRM = 3
CALIBRATION_HOLD_FRAMES_REQUIRED = 18
CALIBRATION_STRAIGHT_HIP_MIN = 160
CALIBRATION_STRAIGHT_KNEE_MIN = 160
BAR_PATH_MAXLEN = 150
BAR_PATH_X_DEVIATION_RATIO = 0.05
BAR_PATH_ERROR = "Sztanga ucieka od pionu"


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


def point_distance(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def make_bar_path_deque() -> Deque[Tuple[int, int]]:
    return deque(maxlen=BAR_PATH_MAXLEN)


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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS session_reps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            rep_index INTEGER NOT NULL,
            score REAL NOT NULL,
            duration_sec REAL NOT NULL,
            errors_json TEXT NOT NULL,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
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


def save_session(reps_total: int, reps_good: int, reps_bad: int, avg_score: float, duration_sec: float, source_type: str) -> int:
    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO sessions (created_at, reps_total, reps_good, reps_bad, avg_score, duration_sec, source_type)
        VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?)
        """,
        (reps_total, reps_good, reps_bad, avg_score, duration_sec, source_type),
    )
    session_id = int(cur.lastrowid)
    conn.commit()
    conn.close()
    return session_id


def save_session_reps(session_id: int, rep_results: List["RepResult"]):
    conn = sqlite3.connect(SESSION_DB)
    cur = conn.cursor()
    for rep in rep_results:
        cur.execute(
            """
            INSERT INTO session_reps (session_id, rep_index, score, duration_sec, errors_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(session_id),
                int(rep.index),
                float(rep.score),
                float(rep.duration_sec),
                json.dumps(rep.errors, ensure_ascii=False),
            ),
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
    started_at: float = 0.0
    ended_at: float = 0.0

    def finalize(self):
        penalty = len(set(self.errors)) * 12
        self.score = clamp(100.0 - penalty, 0.0, 100.0)


@dataclass
class RepResult:
    index: int
    score: float
    errors: List[str]
    duration_sec: float


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
    mode: str = "IDLE"  # IDLE -> RECORDING -> ANALYZING
    rep_results: List[RepResult] = field(default_factory=list)
    analysis_rep_idx: int = 0
    start_hold_frames: int = 0
    is_calibrated: bool = False
    calibration_hold_frames: int = 0
    dyn_start_hip_min: float = START_HIP_ANGLE_MIN
    dyn_start_hip_max: float = START_HIP_ANGLE_MAX
    top_hip_angle_min: float = TOP_HIP_ANGLE_MIN
    top_knee_angle_min: float = TOP_KNEE_ANGLE_MIN
    start_exit_hip_angle: float = START_EXIT_HIP_ANGLE
    start_reenter_hip_angle: float = START_REENTER_HIP_ANGLE
    top_exit_hip_angle: float = TOP_EXIT_HIP_ANGLE
    torso_leg_ratio: float = 1.0
    calibration_ratio_samples: List[float] = field(default_factory=list)
    bar_path: Deque[Tuple[int, int]] = field(default_factory=make_bar_path_deque)


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
        self.command_queue: "queue.Queue[str]" = queue.Queue()
        self.calibration_enabled = True

    def apply_body_ratio_calibration(self, ratio: float):
        ratio = clamp(ratio, 0.55, 1.55)
        # Niskie T/N (dlugie nogi) -> wyzszy wymagany kat biodra w pozycji startowej.
        delta = clamp((0.92 - ratio) * 35.0, -8.0, 10.0)

        self.state.torso_leg_ratio = ratio
        self.state.dyn_start_hip_min = START_HIP_ANGLE_MIN + delta
        self.state.dyn_start_hip_max = START_HIP_ANGLE_MAX + delta
        self.state.start_exit_hip_angle = self.state.dyn_start_hip_max + 8.0
        self.state.start_reenter_hip_angle = self.state.dyn_start_hip_max - 4.0
        self.state.top_hip_angle_min = TOP_HIP_ANGLE_MIN
        self.state.top_knee_angle_min = TOP_KNEE_ANGLE_MIN
        self.state.top_exit_hip_angle = TOP_HIP_ANGLE_MIN - 10.0
        self.state.is_calibrated = True

    def append_bar_path_point(self, frame, wrist_norm: Tuple[float, float]):
        h, w = frame.shape[:2]
        self.state.bar_path.append((int(wrist_norm[0] * w), int(wrist_norm[1] * h)))

    def check_bar_path_vertical_deviation(self, frame_width: int) -> bool:
        if len(self.state.bar_path) < 2:
            return False
        xs = [p[0] for p in self.state.bar_path]
        return (max(xs) - min(xs)) > (BAR_PATH_X_DEVIATION_RATIO * frame_width)

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

    def start_series(self):
        self.reset_session()
        self.state.mode = "RECORDING"
        self.state.start_time = time.time()

        if self.calibration_enabled:
            self.state.current_phase = "CALIBRATING"
            self.state.is_calibrated = False
            self.state.calibration_hold_frames = 0
            self.state.calibration_ratio_samples = []
            self.state.feedback = "Stan prosto bokiem do kamery, aby przeprowadzic kalibracje"
            self.voice.say("Stan prosto bokiem do kamery, aby przeprowadzic kalibracje")
        else:
            self.state.current_phase = "WAIT_START"
            self.state.is_calibrated = True
            self.state.feedback = "Analiza pliku wideo - wykrywanie powtorzen"

    def stop_series(self):
        if self.state.mode != "RECORDING":
            self.state.mode = "ANALYZING"
            self.state.feedback = "Tryb analizy"
            return

        self.state.mode = "ANALYZING"
        self.state.rep_eval = None
        self.state.current_phase = "STOPPED"
        self.state.feedback = "STOP: analiza serii"
        self.voice.say("Stop. Analiza serii")
        self.state.analysis_rep_idx = 0

    def handle_pending_commands(self):
        while True:
            try:
                cmd = self.command_queue.get_nowait()
            except queue.Empty:
                break

            cmd = (cmd or "").strip().lower()
            if cmd == "start":
                self.start_series()
            elif cmd == "stop":
                self.stop_series()

    def update_state_machine(self, metrics: dict, current_errors: List[str]):
        if self.state.mode != "RECORDING":
            return
        if self.state.current_phase == "CALIBRATING":
            return

        hip_angle = metrics["hip_angle"]
        knee_angle = metrics["knee_angle"]

        in_start_position = self.state.dyn_start_hip_min <= hip_angle <= self.state.dyn_start_hip_max
        in_top_position = hip_angle >= self.state.top_hip_angle_min and knee_angle >= self.state.top_knee_angle_min

        phase = self.state.current_phase

        if phase == "WAIT_START":
            self.state.feedback = "Ustaw pozycje startowa"
            if in_start_position:
                self.state.feedback = "Pozycja startowa wykryta - rozpocznij ruch w gore"
                self.state.current_phase = "READY"
                self.state.start_hold_frames = 0

        elif phase == "READY":
            self.state.feedback = "Czekam na rozpoczecie ruchu"
            if in_start_position and hip_angle <= self.state.start_reenter_hip_angle:
                self.state.start_hold_frames += 1
            else:
                self.state.start_hold_frames = 0

            if hip_angle >= self.state.start_exit_hip_angle:
                self.state.current_phase = "GOING_UP"
                self.state.rep_eval = RepEvaluation(started_at=time.time())
                self.state.bar_path.clear()
                self.state.start_hold_frames = 0

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

            if hip_angle < self.state.top_exit_hip_angle:
                self.state.current_phase = "GOING_DOWN"

        elif phase == "GOING_DOWN":
            self.state.feedback = "Odkladaj sztange z kontrola"
            if self.state.rep_eval is not None:
                self.state.rep_eval.errors.extend(current_errors)

            if in_start_position:
                self.state.start_hold_frames += 1
            else:
                self.state.start_hold_frames = 0

            if self.state.start_hold_frames >= START_HOLD_FRAMES_TO_CONFIRM:
                self.finish_rep()
                self.state.current_phase = "READY"
                self.state.rep_eval = None
                self.state.start_hold_frames = 0
                self.state.bar_path.clear()

    def finish_rep(self):
        self.state.rep_count += 1

        if self.state.rep_eval is None:
            self.state.rep_eval = RepEvaluation()

        if self.state.rep_eval.started_at <= 0:
            self.state.rep_eval.started_at = time.time()
        self.state.rep_eval.ended_at = time.time()
        self.state.rep_eval.finalize()
        score = self.state.rep_eval.score
        self.state.session_scores.append(score)

        uniq_errors = sorted(set(self.state.rep_eval.errors))
        duration_sec = max(0.0, self.state.rep_eval.ended_at - self.state.rep_eval.started_at)
        self.state.rep_results.append(
            RepResult(
                index=self.state.rep_count,
                score=float(score),
                errors=uniq_errors,
                duration_sec=float(duration_sec),
            )
        )
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
        # Parametry i statystyki tylko w UI (Flask) / stanie sesji — nie rysujemy na obrazie.
        if len(self.state.bar_path) >= 2:
            pts = np.array(list(self.state.bar_path), dtype=np.int32)
            cv2.polylines(frame, [pts], isClosed=False, color=(255, 255, 0), thickness=3)

    def reset_session(self):
        self.state = TrainerState()
        self.prev_values = {}

    def process_frame(self, frame, source_label: str):
        self.handle_pending_commands()
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

                if self.state.mode == "RECORDING" and self.state.current_phase == "CALIBRATING":
                    straight_pose = (
                        metrics["hip_angle"] >= CALIBRATION_STRAIGHT_HIP_MIN and
                        metrics["knee_angle"] >= CALIBRATION_STRAIGHT_KNEE_MIN
                    )
                    if straight_pose:
                        self.state.calibration_hold_frames += 1
                        torso_len = point_distance(pts["shoulder"], pts["hip"])
                        leg_len = point_distance(pts["hip"], pts["ankle"])
                        if leg_len > 1e-5:
                            self.state.calibration_ratio_samples.append(torso_len / leg_len)
                    else:
                        self.state.calibration_hold_frames = 0
                        self.state.calibration_ratio_samples = []

                    hold = self.state.calibration_hold_frames
                    self.state.feedback = (
                        f"Stan prosto bokiem do kamery, aby przeprowadzic kalibracje ({hold}/{CALIBRATION_HOLD_FRAMES_REQUIRED})"
                    )
                    if hold >= CALIBRATION_HOLD_FRAMES_REQUIRED and self.state.calibration_ratio_samples:
                        ratio = float(np.median(self.state.calibration_ratio_samples))
                        self.apply_body_ratio_calibration(ratio)
                        self.state.current_phase = "WAIT_START"
                        self.state.calibration_hold_frames = 0
                        self.state.calibration_ratio_samples = []
                        self.state.feedback = "Kalibracja zakonczona. Ustaw pozycje startowa."
                        self.voice.say("Kalibracja zakonczona")
                elif self.state.mode == "RECORDING" and self.state.current_phase in ("GOING_UP", "GOING_DOWN"):
                    self.append_bar_path_point(frame, pts["wrist"])
                    if (
                        self.state.current_phase == "GOING_UP" and
                        self.state.rep_eval is not None and
                        self.check_bar_path_vertical_deviation(frame.shape[1])
                    ):
                        if BAR_PATH_ERROR not in self.state.rep_eval.errors:
                            self.state.rep_eval.errors.append(BAR_PATH_ERROR)

                if errors and self.state.mode == "RECORDING" and self.state.current_phase != "CALIBRATING":
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

    def command_input_worker():
        while True:
            try:
                cmd = input().strip()
            except Exception:
                break
            if not cmd:
                continue
            trainer.command_queue.put(cmd)

    threading.Thread(target=command_input_worker, daemon=True).start()
    print("\nKomendy: wpisz 'start' aby rozpoczac serie, 'stop' aby zakonczyc i przejsc do analizy.\n")

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
        elif key == 81:  # left arrow
            if trainer.state.mode == "ANALYZING" and trainer.state.rep_results:
                trainer.state.analysis_rep_idx = max(0, trainer.state.analysis_rep_idx - 1)
        elif key == 83:  # right arrow
            if trainer.state.mode == "ANALYZING" and trainer.state.rep_results:
                trainer.state.analysis_rep_idx = min(len(trainer.state.rep_results) - 1, trainer.state.analysis_rep_idx + 1)

    duration = time.time() - session_started
    avg_score = float(np.mean(trainer.state.session_scores)) if trainer.state.session_scores else 0.0

    session_id = save_session(
        reps_total=trainer.state.rep_count,
        reps_good=trainer.state.good_reps,
        reps_bad=trainer.state.bad_reps,
        avg_score=avg_score,
        duration_sec=duration,
        source_type=source_info["type"]
    )
    if trainer.state.rep_results:
        save_session_reps(session_id, trainer.state.rep_results)

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
