# ==============================================================
# CARLA – Dockerfile
# Containerisiertes Infrastructure Monitoring Dashboard.
# ==============================================================
FROM python:3.11-slim

# Installiere System-Abhängigkeiten und Docker CLI (für Lokal-Modus)
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    lsb-release \
    ssh \
    && curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y docker-ce-cli \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Abhängigkeiten kopieren und installieren
COPY installer.py .
RUN python3 installer.py

# Quellcode kopieren
COPY . .

# Ports freigeben
EXPOSE 8080

# Starte CARLA
CMD ["python3", "main.py"]
