let isUploading = false;

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
            alert("Przełączono na kamerę na żywo. Ustaw się bokiem!");
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

        fetch('/upload_video', { method: 'POST', body: formData })
            .then(response => response.json())
            .then(data => {
                isUploading = false;
                if (data.status === 'ok') {
                    alert("Film przesłany: " + data.filename + ". Rozpoczynam analizę.");
                    reloadVideoFeed();
                } else {
                    alert("Błąd: " + data.error);
                }
            })
            .catch(error => {
                isUploading = false;
                console.error("Błąd przesyłania:", error);
                alert("Wystąpił problem z przesłaniem pliku.");
            });
    }
});

function generateSetInputs() {
    const num = document.getElementById('num-sets-input').value;
    const container = document.getElementById('sets-fields-container');
    container.innerHTML = '';

    for(let i=1; i<=num; i++) {
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

    if(response.ok) {
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

setInterval(async () => {
    if(document.getElementById('training').classList.contains('active') && !isUploading) {
        try {
            const resp = await fetch('/api/stats');
            const d = await resp.json();

            document.getElementById('ui-phase').innerText = d.phase;
            document.getElementById('ui-feedback').innerText = d.feedback;
        } catch (e) {
        }
    }
}, 500);

// --- NOWY KOD ---
// Zawsze gdy ładujemy lub odświeżamy stronę, upewnij się, że zrywamy obraz w tle
window.addEventListener('load', () => {
    fetch('/stop_feed', { method: 'POST' });
});