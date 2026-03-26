# --- Betriebsmodus ---
MODE = os.environ.get("CARLA_MODE", "ssh")

# --- SSH / Server ---
SSH_HOST = os.environ.get("CARLA_SSH_HOST", "deine-ip")
SSH_USER = os.environ.get("CARLA_SSH_USER", "root")
SSH_PASS = os.environ.get("CARLA_SSH_PASS", "dein-passwort")

# --- GitHub ---
GITHUB_TOKEN = os.environ.get("CARLA_GITHUB_TOKEN", "ghp_dein_token")

# --- Cloudflare ---
CF_API_TOKEN = os.environ.get("CARLA_CF_API_TOKEN", "cfut_dein_token")
CF_ACCOUNT_ID = os.environ.get("CARLA_CF_ACCOUNT_ID", "deine-id")

# --- Flask Server ---
HOST = "0.0.0.0"
PORT = 8080
