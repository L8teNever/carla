// Global frontend variables
let fullInfrastructureData = null;

// Toast Notifications Helper (MD3 Snackbar style)
function showToast(message, type = 'success') {
    const container = document.getElementById('toast-container');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML = `
        <div class="toast-indicator"></div>
        <div class="toast-message">${message}</div>
    `;

    container.appendChild(toast);
    
    // Animate in
    setTimeout(() => toast.classList.add('active'), 50);

    // Auto remove
    setTimeout(() => {
        toast.classList.remove('active');
        setTimeout(() => toast.remove(), 300);
    }, 4000);
}

// Topbar Global Gauges (CPU, RAM, Disk)
async function startGlobalMetricsPolling() {
    async function updateGauges() {
        try {
            const r = await fetch('/api/metrics/server?limit=1');
            const history = r.ok ? await r.json() : [];
            if (history.length > 0) {
                const metrics = history[0];
                
                const cpuPct = Math.round(metrics.cpu_usage);
                const ramPct = Math.round((metrics.ram_usage / metrics.ram_total) * 100);
                const diskPct = Math.round((metrics.disk_used / metrics.disk_total) * 100);

                // Update progress bars if elements exist
                const gCpu = document.getElementById('gauge-cpu');
                const gRam = document.getElementById('gauge-ram');
                const gDisk = document.getElementById('gauge-disk');

                if (gCpu) gCpu.style.width = `${cpuPct}%`;
                if (gRam) gRam.style.width = `${ramPct}%`;
                if (gDisk) gDisk.style.width = `${diskPct}%`;

                // Update text values if elements exist
                const gCpuVal = document.getElementById('gauge-cpu-val');
                const gRamVal = document.getElementById('gauge-ram-val');
                const gDiskVal = document.getElementById('gauge-disk-val');

                if (gCpuVal) gCpuVal.innerText = `${cpuPct}%`;
                if (gRamVal) gRamVal.innerText = `${ramPct}%`;
                if (gDiskVal) gDiskVal.innerText = `${diskPct}%`;
            }
        } catch(e) { 
            console.error("Global gauge poll failure", e); 
        }
    }
    updateGauges();
    setInterval(updateGauges, 6000);
}

// Trigger Background Infrastructure Fetch (Manual Refresh)
async function triggerManualRefresh(onComplete = null) {
    const statusDot = document.getElementById('status-dot');
    const mobileStatusDot = document.getElementById('mobile-status-dot');
    const statusText = document.getElementById('status-text');
    
    if (statusDot) statusDot.className = "status-dot loading";
    if (mobileStatusDot) mobileStatusDot.className = "status-dot loading";
    if (statusText) statusText.textContent = "Aktualisiere...";
    
    try {
        const resp = await fetch('/api/refresh', { method: 'POST' });
        if (resp.ok) {
            showToast("Hintergrund-Fetch angestoßen...", "info");
            setTimeout(async () => {
                const r = await fetch('/api/full-infrastructure');
                if (r.ok) {
                    const data = await r.json();
                    fullInfrastructureData = data;
                    
                    if (statusDot) statusDot.className = "status-dot";
                    if (mobileStatusDot) mobileStatusDot.className = "status-dot";
                    if (statusText) statusText.textContent = "Verbunden";
                    
                    showToast("Daten erfolgreich aktualisiert!");
                    
                    if (onComplete) {
                        onComplete(data);
                    }
                    
                    // Trigger custom event so active pages can re-render if necessary
                    document.dispatchEvent(new CustomEvent('carlaRefresh', { detail: data }));
                }
            }, 3000);
        }
    } catch (e) {
        console.error(e);
        if (statusDot) statusDot.className = "status-dot";
        if (mobileStatusDot) mobileStatusDot.className = "status-dot";
        if (statusText) statusText.textContent = "Fehler";
        showToast("Aktualisierung fehlgeschlagen", "error");
    }
}

// Unified Page Initialization Flow
async function initCarlaPage(onDataLoaded = null) {
    const loaderMsg = document.getElementById('loader-msg');
    const mobileStatusDot = document.getElementById('mobile-status-dot');
    
    try {
        const res = await fetch('/api/full-infrastructure');
        if (res.status === 202) {
            if (loaderMsg) loaderMsg.innerText = "Warte auf erste Echtzeit-Daten...";
            if (mobileStatusDot) mobileStatusDot.className = "status-dot loading";
            setTimeout(() => initCarlaPage(onDataLoaded), 2000);
            return;
        }
        const data = await res.json();
        fullInfrastructureData = data;

        // Populate header details
        const osText = document.getElementById('os-text');
        if (osText) osText.innerText = data.os || "Debian Linux";
        
        const headerIp = document.getElementById('header-ip');
        if (headerIp) headerIp.innerText = window.location.hostname;

        // Start metric polling loops
        startGlobalMetricsPolling();

        if (mobileStatusDot) mobileStatusDot.className = "status-dot";

        // Run subpage custom rendering callback
        if (onDataLoaded) {
            await onDataLoaded(data);
        }

        // Hide loader overlay with fade out animation
        const loader = document.getElementById('app-loader');
        if (loader) {
            loader.style.opacity = '0';
            setTimeout(() => loader.style.display = 'none', 500);
        }
    } catch (e) {
        console.error("Init Error:", e);
        if (loaderMsg) loaderMsg.innerText = "Fehler beim Laden! Läuft Docker?";
    }

    // Check if redirecting from addStack shortcut
    if (window.location.search.includes('addStack=true')) {
        setTimeout(() => {
            if (typeof openDeployModal === 'function') {
                openDeployModal();
            } else {
                const addBtn = document.querySelector('[onclick*="openDeployModal"]');
                if (addBtn) addBtn.click();
            }
            // Clean URL parameter without reloading
            const cleanUrl = window.location.protocol + "//" + window.location.host + window.location.pathname;
            window.history.replaceState({path: cleanUrl}, '', cleanUrl);
        }, 400);
    }
}

// Search feature implementation
const SEARCHABLE_ITEMS = [
    { title: "Dashboard / Stacks", desc: "Übersicht aller Docker-Compose Projekte", type: "page", url: "/" },
    { title: "Live Map / Infrastruktur", desc: "Visuelle Darstellung von Containern und Netzwerken", type: "page", url: "/infrastructure" },
    { title: "Performance / Leistung", desc: "Auslastungs-Historie von CPU, RAM und Festplatten", type: "page", url: "/performance" },
    { title: "Timeline / Protokolle", desc: "Echtzeit-Logs und System-Ereignisse", type: "page", url: "/timeline" },
    { title: "Backup / Sicherung", desc: "Daten-Sicherung erstellen und verwalten", type: "page", url: "/backup" },
    { title: "Ports / Belegungen", desc: "Freie und belegte Netzwerk-Ports einsehen", type: "page", url: "/ports" },
    { title: "Netze / Docker Networks", desc: "Docker-Netzwerk-Konfigurationen verwalten", type: "page", url: "/networks" },
    { title: "Umleitungen / Nginx Proxy", desc: "Nginx Reverse Proxy Regeln & Weiterleitungen", type: "page", url: "/redirects" },
    { title: "Sites / Webseiten", desc: "Statische HTML/CSS Webseiten hosten", type: "page", url: "/sites" },
    { title: "Domains / Cloudflare DNS", desc: "Cloudflare Domain-Registrierungen und DNS-Einträge", type: "page", url: "/domains" },
    { title: "Dateien / File Manager", desc: "Dateisystem durchsuchen und verwalten", type: "page", url: "/filemanager" },
    { title: "Terminal / Konsole", desc: "Globales System-Terminal öffnen", type: "page", url: "/terminal" },
    { title: "Einstellungen / Settings", desc: "Konfigurationen anpassen und Backups einrichten", type: "page", url: "/settings" },
    
    // Core Actions
    { title: "Aktion: Daten aktualisieren (Refresh)", desc: "Docker-Infrastruktur neu einlesen", type: "action", action: "refresh" },
    { title: "Aktion: Neuen Stack hinzufügen (Add Stack)", desc: "Ein neues Docker-Compose Projekt anlegen", type: "action", action: "addStack" },
    { title: "Aktion: Backup starten (Run Backup)", desc: "System-Backup sofort ausführen", type: "action", action: "startBackup" }
];

let selectedResultIndex = -1;

function openGlobalSearch() {
    const backdrop = document.getElementById('search-modal-backdrop');
    if (!backdrop) return;

    // Close mobile bottom sheet if open
    const mobileSheet = document.getElementById('mobile-bottom-sheet');
    const mobileBackdrop = document.getElementById('bottom-sheet-backdrop');
    if (mobileSheet) mobileSheet.classList.remove('open');
    if (mobileBackdrop) mobileBackdrop.classList.remove('open');

    backdrop.classList.add('active');
    
    const input = document.getElementById('global-search-input');
    if (input) {
        input.value = "";
        input.focus();
    }
    
    selectedResultIndex = -1;
    renderSearchResults("");
}

function closeGlobalSearch() {
    const backdrop = document.getElementById('search-modal-backdrop');
    if (backdrop) {
        backdrop.classList.remove('active');
    }
}

function renderSearchResults(query) {
    const resultsList = document.getElementById('search-results-list');
    if (!resultsList) return;
    
    resultsList.innerHTML = "";
    selectedResultIndex = -1;
    
    const q = query.toLowerCase().trim();
    const filtered = SEARCHABLE_ITEMS.filter(item => 
        item.title.toLowerCase().includes(q) || 
        item.desc.toLowerCase().includes(q)
    );
    
    if (filtered.length === 0) {
        resultsList.innerHTML = `
            <div style="padding:24px; text-align:center; color:var(--md-text-muted); font-size:14px;">
                Keine passenden Seiten oder Aktionen gefunden.
            </div>
        `;
        return;
    }
    
    filtered.forEach((item, index) => {
        const div = document.createElement('div');
        div.className = "search-result-item";
        div.dataset.index = index;
        
        const isPage = item.type === "page";
        const iconSvg = isPage 
            ? `<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>`
            : `<svg viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>`;
            
        div.innerHTML = `
            <div class="search-result-details">
                <div class="search-result-icon">
                    ${iconSvg}
                </div>
                <div class="search-result-text">
                    <span class="search-result-title">${item.title}</span>
                    <span class="search-result-subtitle">${item.desc}</span>
                </div>
            </div>
            <span class="search-result-badge">${isPage ? 'Seite' : 'Aktion'}</span>
        `;
        
        div.addEventListener('click', () => triggerSearchAction(item));
        resultsList.appendChild(div);
    });
}

function triggerSearchAction(item) {
    closeGlobalSearch();
    if (item.type === 'page') {
        window.location.href = item.url;
    } else if (item.type === 'action') {
        if (item.action === 'refresh') {
            triggerManualRefresh();
        } else if (item.action === 'addStack') {
            const addBtn = document.querySelector('[onclick*="openDeployModal"]');
            if (addBtn) {
                addBtn.click();
            } else {
                window.location.href = "/?addStack=true";
            }
        } else if (item.action === 'startBackup') {
            window.location.href = "/backup";
        }
    }
}

// Bind events for global search
window.addEventListener('keydown', (e) => {
    // Ctrl + K or Cmd + K triggers search
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        openGlobalSearch();
    }
});

// Setup listeners on search input
document.addEventListener('DOMContentLoaded', () => {
    const searchInput = document.getElementById('global-search-input');
    
    if (searchInput) {
        searchInput.addEventListener('input', (e) => {
            renderSearchResults(e.target.value);
        });
        
        searchInput.addEventListener('keydown', (e) => {
            const items = document.querySelectorAll('.search-result-item');
            if (items.length === 0) return;
            
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                if (selectedResultIndex < items.length - 1) {
                    selectedResultIndex++;
                    updateSelectedSearchItem(items);
                }
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                if (selectedResultIndex > 0) {
                    selectedResultIndex--;
                    updateSelectedSearchItem(items);
                }
            } else if (e.key === 'Enter') {
                e.preventDefault();
                if (selectedResultIndex >= 0 && selectedResultIndex < items.length) {
                    items[selectedResultIndex].click();
                } else if (items.length > 0) {
                    items[0].click();
                }
            } else if (e.key === 'Escape') {
                e.preventDefault();
                closeGlobalSearch();
            }
        });
    }
});

function updateSelectedSearchItem(items) {
    items.forEach((item, index) => {
        if (index === selectedResultIndex) {
            item.classList.add('selected');
            item.scrollIntoView({ block: 'nearest' });
        } else {
            item.classList.remove('selected');
        }
    });
}
