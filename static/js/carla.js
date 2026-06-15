// Global frontend variables
let fullInfrastructureData = null;

// Toast Notifications Helper
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
    const statusText = document.getElementById('status-text');
    
    if (statusDot) statusDot.className = "status-dot loading";
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
                    if (statusText) statusText.textContent = "Verbunden";
                    
                    showToast("Daten erfolgreich aktualisiert!");
                    
                    if (onComplete) {
                        onComplete(data);
                    }
                }
            }, 3000);
        }
    } catch (e) {
        console.error(e);
        if (statusDot) statusDot.className = "status-dot";
        if (statusText) statusText.textContent = "Fehler";
        showToast("Aktualisierung fehlgeschlagen", "error");
    }
}

// Responsive Sidebar Mobile Drawer Toggle
function toggleMobileMenu() {
    const rail = document.querySelector('.app-rail');
    if (rail) {
        rail.classList.toggle('open');
    }
}

// Unified Page Initialization Flow
async function initCarlaPage(onDataLoaded = null) {
    try {
        const res = await fetch('/api/full-infrastructure');
        if (res.status === 202) {
            const loaderMsg = document.getElementById('loader-msg');
            if (loaderMsg) loaderMsg.innerText = "Warte auf erste Echtzeit-Daten...";
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
        const loaderMsg = document.getElementById('loader-msg');
        if (loaderMsg) loaderMsg.innerText = "Fehler beim Laden! Läuft Docker?";
    }
}
