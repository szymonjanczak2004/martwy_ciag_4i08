let isUploading = false;

let voiceOutputEnabled = true;
let voiceRecognitionEnabled = false;
let lastSpokenFeedback = "";
let lastSpokenAt = 0;
let recognition = null;

function reloadVideoFeed() {
    const videoImg = document.querySelector('.video-feed-container img');
    videoImg.src = "/video_feed?" + new Date().getTime();
}

function showTab(tabId, clickedElement) {
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById(tabId).classList.add('active');
    clickedElement.classList.add('active');
}

function activateLive() {
    fetch('/set_live', { method: 'POST' })
        .then(() => {
            speakText("Przełączono na kamerę na żywo. Ustaw się bokiem.");
            reloadVideoFeed();
        });
}

function triggerUpload() {
    document.getElementById('video-upload-input').click();
}

document.getElementById('video-upload-input').addEventListener('change', function() {
    if (this.files && this.files[0]) {
        const formData = new FormData();
        formData.append('file', this.files[0]);

        isUploading = true;
        document.getElementById('ui-phase').innerText = "LOADING...";
        document.getElementById('ui-feedback').innerText = "Przesyłanie i analiza pliku...";
        speakText("Przesyłam plik do analizy.");

        fetch('/upload_video', { method: 'POST', body: formData })
            .then(response => response.json())
            .then(data => {
                isUploading = false;
                if (data.status === 'ok') {
                    speakText("Film został przesłany. Rozpoczynam analizę.");
                    reloadVideoFeed();
                } else {
                    speakText("Wystąpił błąd przesyłania pliku.");
                    alert("Błąd: " + data.error);
                }
            })
            .catch(error => {
                isUploading = false;
                console.error("Błąd przesyłania:", error);
                speakText("Wystąpił problem z przesłaniem pliku.");
                alert("Wystąpił problem z przesłaniem pliku.");
            });
    }
});

function generateSetInputs() {
    const num = document.getElementById('num-sets-input').value;
    const container = document.getElementById('sets-fields-container');
    container.innerHTML = '';

    for(let i = 1; i <= num; i++) {
        container.innerHTML += `
            <div class="set-input-row">
                <div>S${i} KG: <input type="number" class="w-input" placeholder="0"></div>
                <div>S${i} REPS: <input type="number" class="r-input" placeholder="0"></div>
            </div>
        `;
    }

    document.getElementById('step-1-sets').style.display = 'none';
    document.getElementById('step-2-details').style.display = 'block';
}

function resetBlockForm() {
    document.getElementById('step-1-sets').style.display = 'block';
    document.getElementById('step-2-details').style.display = 'none';
}

async function saveTrainingBlock() {
    const weights = document.querySelectorAll('.w-input');
    const reps = document.querySelectorAll('.r-input');
    const setsData = [];

    weights.forEach((w, index) => {
        setsData.push({ weight: w.value || 0, reps: reps[index].value || 0 });
    });

    const response = await fetch('/api/save_training_block', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sets: setsData })
    });

    if (response.ok) {
        speakText("Trening zapisany pomyślnie.");
        alert("Trening zapisany pomyślnie!");
        resetBlockForm();
    }
}

async function loadHistory() {
    const response = await fetch('/api/history');
    const data = await response.json();
    const tbody = document.getElementById('history-rows');
    tbody.innerHTML = '';

    data.forEach(item => {
        tbody.innerHTML += `
            <tr>
                <td>${item.date}</td>
                <td>${item.sets}</td>
                <td>${item.reps}</td>
                <td>${item.weight} kg</td>
            </tr>
        `;
    });
}

/* =========================
   GŁOS - ODTWARZANIE
========================= */
function speakText(text) {
    if (!voiceOutputEnabled || !text) return;

    const now = Date.now();

    if (text === lastSpokenFeedback && now - lastSpokenAt < 2500) {
        return;
    }

    lastSpokenFeedback = text;
    lastSpokenAt = now;

    window.speechSynthesis.cancel();

    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "pl-PL";
    utterance.rate = 1.0;
    utterance.pitch = 1.0;
    utterance.volume = 1.0;

    window.speechSynthesis.speak(utterance);
}

function toggleVoiceOutput() {
    voiceOutputEnabled = !voiceOutputEnabled;

    const btn = document.getElementById("voice-toggle-btn");
    const status = document.getElementById("voice-status");

    if (voiceOutputEnabled) {
        btn.innerText = "🔊 GŁOS: ON";
        status.innerText = "Komunikaty głosowe aktywne";
        speakText("Komunikaty głosowe włączone.");
    } else {
        window.speechSynthesis.cancel();
        btn.innerText = "🔇 GŁOS: OFF";
        status.innerText = "Komunikaty głosowe wyłączone";
    }
}

/* =========================
   GŁOS - ROZPOZNAWANIE
========================= */
function setupRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
        document.getElementById("mic-status").innerText = "Ta przeglądarka nie obsługuje rozpoznawania mowy";
        return null;
    }

    const rec = new SpeechRecognition();
    rec.lang = "pl-PL";
    rec.continuous = true;
    rec.interimResults = false;

    rec.onresult = (event) => {
        const transcript = event.results[event.results.length - 1][0].transcript.toLowerCase().trim();
        console.log("Komenda głosowa:", transcript);
        handleVoiceCommand(transcript);
    };

    rec.onend = () => {
        if (voiceRecognitionEnabled && recognition) {
            try {
                recognition.start();
            } catch (e) {}
        }
    };

    rec.onerror = (event) => {
        console.log("Błąd rozpoznawania mowy:", event.error);
    };

    return rec;
}

function toggleVoiceRecognition() {
    if (!recognition) {
        recognition = setupRecognition();
        if (!recognition) return;
    }

    voiceRecognitionEnabled = !voiceRecognitionEnabled;

    const btn = document.getElementById("mic-toggle-btn");
    const status = document.getElementById("mic-status");

    if (voiceRecognitionEnabled) {
        btn.innerText = "🎤 MIKROFON: ON";
        status.innerText = "Rozpoznawanie mowy aktywne";
        speakText("Mikrofon włączony.");
        try {
            recognition.start();
        } catch (e) {}
    } else {
        btn.innerText = "🎤 MIKROFON: OFF";
        status.innerText = "Rozpoznawanie mowy wyłączone";
        speakText("Mikrofon wyłączony.");
        try {
            recognition.stop();
        } catch (e) {}
    }
}

function handleVoiceCommand(command) {
    if (command.includes("kamera") || command.includes("live")) {
        activateLive();
        return;
    }

    if (command.includes("film") || command.includes("plik") || command.includes("upload")) {
        speakText("Otwieram wybór pliku.");
        triggerUpload();
        return;
    }

    if (command.includes("stop") || command.includes("zatrzymaj")) {
        fetch('/stop_feed', { method: 'POST' }).then(() => {
            speakText("Zatrzymano analizę.");
            reloadVideoFeed();
        });
        return;
    }

    if (command.includes("trening")) {
        const tab = document.querySelectorAll('.tab')[0];
        showTab('training', tab);
        speakText("Otwieram zakładkę trening.");
        return;
    }

    if (command.includes("historia")) {
        const tab = document.querySelectorAll('.tab')[1];
        showTab('history', tab);
        loadHistory();
        speakText("Otwieram historię treningów.");
        return;
    }

    if (command.includes("statystyki")) {
        const tab = document.querySelectorAll('.tab')[2];
        showTab('stats', tab);
        speakText("Otwieram statystyki.");
        return;
    }
}

/* =========================
   ODCZYT KOMUNIKATÓW AI
========================= */
setInterval(async () => {
    if (document.getElementById('training').classList.contains('active') && !isUploading) {
        try {
            const resp = await fetch('/api/stats');
            const d = await resp.json();

            document.getElementById('ui-phase').innerText = d.phase;
            document.getElementById('ui-feedback').innerText = d.feedback;

            if (d.feedback) {
                speakText(d.feedback);
            }
        } catch (e) {
            console.log("Błąd pobierania statystyk:", e);
        }
    }
}, 700);

window.addEventListener('load', () => {
    fetch('/stop_feed', { method: 'POST' });
});
