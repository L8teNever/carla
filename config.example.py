# --- Betriebsmodus ---
# 'ssh'  : Monitoriert einen entfernten Server (Standard)
# 'local': Monitoriert den lokalen Linux-Server, auf dem CARLA läuft
MODE = "ssh"

# --- SSH / Server (Nur für MODE='ssh' relevant) ---
SSH_HOST = "deine-ip"
SSH_USER = "root"
SSH_PASS = "dein-passwort"

# --- GitHub ---
GITHUB_TOKEN = "ghp_dein_token"

# --- Cloudflare ---
CF_API_TOKEN = "cfut_dein_token"
CF_ACCOUNT_ID = "deine-id"

# --- Flask Server ---
HOST = "0.0.0.0"
PORT = 8080
