let isUploading = false;

// Globalne zmienne dla integracji AI z seriami treningowymi
let activeAnalyzingSetIndex = null;
let activeAnalyzingButton = null;
let analyzedSetsData = {}; // Format: { [setIndex]: [repDetails] }
let currentAISessionReps = []; // Dla bezpośredniego zapisu pojedynczej serii

function escapeHtml(unsafe) {
    return String(unsafe)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

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

async function sendAnalysisControl(action) {
    const response = await fetch('/api/control', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ action })
    });

    const data = await response.json();
    if (!response.ok || data.status !== 'ok') {
        alert(data.message || 'Nie udalo sie wykonac akcji.');
        return;
    }

    if (action === 'stop') {
        // Po zatrzymaniu pobierz ostateczne dane sesji i przypisz je
        setTimeout(fetchFinalStatsAfterStop, 1000);
    }
}

async function fetchFinalStatsAfterStop() {
    try {
        const resp = await fetch('/api/stats');
        const d = await resp.json();
        
        // Wyświetl ogólne błędy i powtórzenia w panelu
        renderImprovementSummary(d.improvement_summary || []);
        renderLiveErrorPanel(d.improvement_summary || [], `${d.phase}: ${d.feedback || ''}`);
        renderRepetitionDetailsList(d.rep_details || []);
        
        if (activeAnalyzingSetIndex !== null) {
            // Przypisz analizę powtórzeń do aktywnej serii
            analyzedSetsData[activeAnalyzingSetIndex] = d.rep_details || [];
            
            // Uzupełnij pole REPS
            const repInput = document.getElementById(`r-input-${activeAnalyzingSetIndex}`);
            if (repInput) {
                repInput.value = d.reps || 0;
            }
            
            alert(`Seria ${activeAnalyzingSetIndex} przeanalizowana! Wykryto powtórzeń: ${d.reps}. Uwagi zostały powiązane z tą serią.`);
            
            // Zresetuj przycisk
            if (activeAnalyzingButton) {
                activeAnalyzingButton.classList.remove('btn-analyzing-active');
                activeAnalyzingButton.innerText = '📹 GOTOWE';
                activeAnalyzingButton.style.backgroundColor = 'var(--main-green)';
            }
            activeAnalyzingSetIndex = null;
            activeAnalyzingButton = null;
        } else {
            // Standalone analiza (zapis z poziomu przycisku pod listą)
            currentAISessionReps = d.rep_details || [];
            
            const standaloneBlock = document.getElementById('standalone-save-container');
            if (standaloneBlock) {
                standaloneBlock.style.display = 'block';
            }
        }
    } catch (e) {
        console.error("Błąd podczas pobierania ostatecznych statystyk:", e);
    }
}

// Funkcja wywoływana przy kliknięciu przycisku "Analizuj tę serię"
function startAnalyzingSet(setIndex, buttonEl) {
    // Zresetuj poprzednio aktywny przycisk
    document.querySelectorAll('.btn-analyze-set-row').forEach(btn => {
        btn.classList.remove('btn-analyzing-active');
        btn.innerText = '📹 ANALIZUJ';
        btn.style.backgroundColor = '';
    });
    
    // Jeśli kliknięto ponownie tę samą serię - rozłącz
    if (activeAnalyzingSetIndex === setIndex) {
        activeAnalyzingSetIndex = null;
        activeAnalyzingButton = null;
        alert("Odłączono analizę od Serii " + setIndex);
        return;
    }
    
    activeAnalyzingSetIndex = setIndex;
    activeAnalyzingButton = buttonEl;
    
    // Oznacz przycisk jako aktywny
    buttonEl.classList.add('btn-analyzing-active');
    buttonEl.innerText = '📹 DETEKCJA...';
    
    // Ukryj standalone save block
    const standaloneBlock = document.getElementById('standalone-save-container');
    if (standaloneBlock) standaloneBlock.style.display = 'none';
    
    // Wyślij sygnał START do backendu
    sendAnalysisControl('start');
}

// Zapisz serię standalone bezpośrednio do historii
async function saveStandaloneSet() {
    const weight = document.getElementById('standalone-weight-input').value || 0;
    const repsCount = currentAISessionReps.length;
    
    if (repsCount === 0) {
        alert("Brak powtórzeń do zapisania.");
        return;
    }
    
    const setsData = [{
        weight: weight,
        reps: repsCount,
        ai_analysis: currentAISessionReps
    }];
    
    const response = await fetch('/api/save_training_block', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sets: setsData })
    });
    
    if (response.ok) {
        alert("Seria została pomyślnie zapisana w historii!");
        document.getElementById('standalone-save-container').style.display = 'none';
        currentAISessionReps = [];
    } else {
        alert("Wystąpił błąd podczas zapisu serii.");
    }
}

function renderImprovementSummary(summary) {
    const list = document.getElementById('improvement-summary-list');
    if (!summary || summary.length === 0) {
        list.innerHTML = '<li>Brak bledow technicznych w zarejestrowanych powtorzeniach.</li>';
        return;
    }

    list.innerHTML = summary.map(item => `
        <li>
            <strong>${escapeHtml(item.error)}</strong> (${item.count}x): ${escapeHtml(item.tip)}
        </li>
    `).join('');
}

function renderLiveErrorPanel(summary, feedback) {
    const list = document.getElementById('live-error-list');
    if (!list) return;

    if (summary && summary.length > 0) {
        list.innerHTML = summary.map(item => `
            <div class="rule-item">
                <strong>${escapeHtml(item.error)}</strong> (${item.count}x)<br>
                ${escapeHtml(item.tip)}
            </div>
        `).join('');
        return;
    }

    if (feedback && feedback.trim().length > 0) {
        list.innerHTML = `
            <div class="rule-item">Biezacy feedback: ${escapeHtml(feedback)}</div>
            <div class="rule-item">Po zakonczonej serii panel uzupelni najczestsze bledy i rekomendacje.</div>
        `;
        return;
    }

    list.innerHTML = `
        <div class="rule-item">Brak bledow technicznych do wyswietlenia.</div>
    `;
}

// Renderowanie szczegółów każdego powtórzenia w panelu analizy błędów
function renderRepetitionDetailsList(reps) {
    const section = document.getElementById('rep-details-section');
    const list = document.getElementById('rep-details-list');
    if (!section || !list) return;
    
    if (!reps || reps.length === 0) {
        section.style.display = 'none';
        list.innerHTML = '';
        return;
    }
    
    section.style.display = 'block';
    list.innerHTML = reps.map(rep => {
        const isGood = rep.status === 'good';
        const statusText = isGood ? 'ZALICZONE' : 'WYKRYTO BŁĘDY';
        const cardClass = isGood ? 'good' : 'bad';
        
        let errorsHtml = '';
        if (rep.errors && rep.errors.length > 0) {
            errorsHtml = `<ul class="rep-errors-list">` + 
                rep.errors.map(err => `<li>${escapeHtml(err)}</li>`).join('') + 
                `</ul>`;
        }
        
        let tipsHtml = '';
        if (rep.tips && rep.tips.length > 0) {
            tipsHtml = `<ul class="rep-tips-list">` + 
                rep.tips.map(tip => `<li>${escapeHtml(tip)}</li>`).join('') + 
                `</ul>`;
        } else if (!isGood) {
            tipsHtml = `<div class="rep-tips-list">Skonsultuj technikę i zwolnij tempo ruchu.</div>`;
        } else {
            tipsHtml = `<div class="rep-tips-list" style="color: green; font-weight: bold;">✓ Świetna technika! Utrzymaj tę formę.</div>`;
        }
        
        return `
            <div class="rep-card ${cardClass}">
                <div class="rep-card-header">
                    <span>POWTÓRZENIE ${rep.index} (${statusText})</span>
                    <span class="rep-card-score">Wynik: ${rep.score}%</span>
                </div>
                <div style="font-size: 0.95rem; margin-top: 5px;">
                    <strong>Czas trwania:</strong> ${rep.duration_sec}s
                </div>
                ${errorsHtml}
                ${tipsHtml}
            </div>
        `;
    }).join('');
}

document.getElementById('video-upload-input').addEventListener('change', function() {
    if (this.files && this.files[0]) {
        const formData = new FormData();
        formData.append('file', this.files[0]);

        isUploading = true;
        const errorList = document.getElementById('live-error-list');
        if (errorList) {
            errorList.innerHTML = `
                <div class="rule-item">LOADING...</div>
                <div class="rule-item">Przesylanie i analiza pliku...</div>
            `;
        }

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
    
    // Czyszczenie poprzednich powiązań analiz
    analyzedSetsData = {};
    activeAnalyzingSetIndex = null;
    activeAnalyzingButton = null;

    for(let i=1; i<=num; i++) {
        container.innerHTML += `
            <div class="set-input-row" id="set-row-${i}">
                <div>S${i} KG: <input type="number" class="w-input" placeholder="0" id="w-input-${i}"></div>
                <div style="display: flex; align-items: center; justify-content: space-between;">
                    <span>S${i} REPS: <input type="number" class="r-input" placeholder="0" id="r-input-${i}" style="width: 50px;"></span>
                    <button type="button" class="btn btn-live btn-analyze-set-row" onclick="startAnalyzingSet(${i}, this)" style="padding: 4px 8px; font-size: 0.85rem; font-family: sans-serif; box-shadow: 2px 2px 0px #000; margin-left: 10px;">📹 ANALIZUJ</button>
                </div>
            </div>
        `;
    }
    document.getElementById('step-1-sets').style.display = 'none';
    document.getElementById('step-2-details').style.display = 'block';
}

function resetBlockForm() {
    document.getElementById('step-1-sets').style.display = 'block';
    document.getElementById('step-2-details').style.display = 'none';
    analyzedSetsData = {};
    activeAnalyzingSetIndex = null;
    activeAnalyzingButton = null;
}

async function saveTrainingBlock() {
    const weights = document.querySelectorAll('.w-input');
    const reps = document.querySelectorAll('.r-input');
    const setsData = [];

    weights.forEach((w, index) => {
        const setNum = index + 1;
        setsData.push({ 
            weight: w.value || 0, 
            reps: reps[index].value || 0,
            ai_analysis: analyzedSetsData[setNum] || null
        });
    });

    const response = await fetch('/api/save_training_block', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ sets: setsData })
    });

    if(response.ok) {
        alert("Trening zapisany pomyślnie!");
        resetBlockForm();
    } else {
        alert("Błąd podczas zapisywania bloku treningowego.");
    }
}

async function loadHistory() {
    const response = await fetch('/api/history');
    const data = await response.json();
    const tbody = document.getElementById('history-rows');
    tbody.innerHTML = '';

    if (!data || data.length === 0) {
        tbody.innerHTML = `<tr><td colspan="4" style="text-align: center;">Brak danych w historii.</td></tr>`;
        return;
    }

    data.forEach(item => {
        let setsHtml = '';
        if (item.sets && item.sets.length > 0) {
            item.sets.forEach((set, sIdx) => {
                let repBreakdownHtml = '';
                let hasAI = set.ai_analysis && set.ai_analysis.length > 0;
                
                if (hasAI) {
                    repBreakdownHtml = `
                        <div class="history-rep-grid" id="history-reps-${set.id}" style="display: none;">
                            ${set.ai_analysis.map(rep => {
                                const isGood = rep.status === 'good' || !rep.errors || rep.errors.length === 0;
                                const repClass = isGood ? 'good' : 'bad';
                                const statusLabel = isGood ? 'Zaliczone' : 'Wykryte błędy';
                                const errs = rep.errors && rep.errors.length > 0 ? rep.errors.join(', ') : 'brak';
                                const tips = rep.tips && rep.tips.length > 0 ? rep.tips.join(' | ') : (isGood ? 'Świetna technika' : 'Zwolnij tempo ruchu');
                                return `
                                    <div class="history-rep-item ${repClass}">
                                        <strong>Powtórzenie ${rep.index}</strong> (${statusLabel} | Wynik: ${rep.score}%)<br>
                                        <span style="color: #cc0000; font-weight: bold;">Błędy:</span> ${escapeHtml(errs)}<br>
                                        <span style="color: #444; font-style: italic;">Wskazówki:</span> ${escapeHtml(tips)}
                                    </div>
                                `;
                            }).join('')}
                        </div>
                    `;
                }
                
                setsHtml += `
                    <div class="history-set-item">
                        <div class="history-set-header">
                            <span>SERIA ${sIdx + 1}: ${set.weight} kg x ${set.reps} powtórzeń</span>
                            ${hasAI ? `<button type="button" class="btn-toggle-rep" onclick="event.stopPropagation(); toggleHistoryReps(${set.id}, this)">Pokaż analizę AI (${set.ai_analysis.length})</button>` : `<span style="font-size: 0.85rem; color: #666; font-family: Arial;">(Brak analizy AI)</span>`}
                        </div>
                        ${repBreakdownHtml}
                    </div>
                `;
            });
        } else {
            setsHtml = '<p>Brak szczegółów serii.</p>';
        }

        tbody.innerHTML += `
            <tr class="history-row" onclick="toggleHistoryDetails(${item.id})">
                <td>${item.date} 🔍</td>
                <td>${item.sets_count}</td>
                <td>${item.total_reps}</td>
                <td>${item.avg_weight} kg</td>
            </tr>
            <tr class="history-details-row" id="history-details-${item.id}" style="display: none;">
                <td colspan="4">
                    <div class="history-details-content">
                        <h4 class="history-details-title">Szczegóły treningu z dnia ${item.date}</h4>
                        ${setsHtml}
                    </div>
                </td>
            </tr>
        `;
    });
}

function toggleHistoryDetails(blockId) {
    const el = document.getElementById(`history-details-${blockId}`);
    if (el) {
        el.style.display = el.style.display === 'none' ? 'table-row' : 'none';
    }
}

function toggleHistoryReps(setId, btn) {
    const el = document.getElementById(`history-reps-${setId}`);
    if (el) {
        if (el.style.display === 'none') {
            el.style.display = 'grid';
            btn.innerText = 'Ukryj analizę';
        } else {
            el.style.display = 'none';
            btn.innerText = 'Pokaż analizę';
        }
    }
}

setInterval(async () => {
    if(document.getElementById('training').classList.contains('active') && !isUploading) {
        try {
            const resp = await fetch('/api/stats');
            const d = await resp.json();
            
            renderImprovementSummary(d.improvement_summary || []);
            renderLiveErrorPanel(d.improvement_summary || [], `${d.phase}: ${d.feedback || ''}`);
            
            // Renderuj powtórzenia na żywo, jeśli są wykryte
            if (d.rep_details && d.rep_details.length > 0) {
                renderRepetitionDetailsList(d.rep_details);
            }
            
            // Jeśli analizujemy konkretną zaplanowaną serię, aktualizuj powtórzenia w czasie rzeczywistym!
            if (activeAnalyzingSetIndex !== null && d.mode === 'RECORDING') {
                const repInput = document.getElementById(`r-input-${activeAnalyzingSetIndex}`);
                if (repInput) {
                    repInput.value = d.reps || 0;
                }
            }
        } catch (e) {
        }
    }
}, 500);

// --- Zrywanie obrazu w tle ---
window.addEventListener('load', () => {
    fetch('/stop_feed', { method: 'POST' });
});
