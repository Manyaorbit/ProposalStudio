"""Settings, read once from environment variables (and a local .env file when running on your own computer).

Two modes:
  APP_ENV=local       (default) listens on 127.0.0.1 only; plain HTTP is fine.
  APP_ENV=production  for a website: HTTPS only, a host allow-list, authenticator-app code required.
"""
import os

from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _list(name):
    return [h.strip().lower() for h in os.getenv(name, "").split(",") if h.strip()]


class Config:
    production = os.getenv("APP_ENV", "local").strip().lower() == "production"

    # Production listens on all interfaces because the hosting platform's HTTPS proxy sits in front.
    host = os.getenv("APP_HOST", "0.0.0.0" if production else "127.0.0.1")
    port = int(os.getenv("PORT", "5055"))

    user = os.getenv("APP_USER", "").strip()
    password_hash = os.getenv("APP_PASSWORD_HASH", "").strip().encode()
    # Base32 secret for the authenticator app (Google/Microsoft Authenticator, Authy, ...).
    totp_secret = os.getenv("TOTP_SECRET", "").strip().replace(" ", "").upper()
    # Lets the one-time /setup page work until a login exists. Remove it afterwards.
    setup_token = os.getenv("SETUP_TOKEN", "").strip()

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "gpt-4.1").strip()

    https = production or os.getenv("APP_HTTPS", "0") == "1"
    trust_proxy = os.getenv("TRUST_PROXY", "1" if production else "0") == "1"

    # Public host names this site answers to, e.g. "proposals.example.com". Anything else gets a 404.
    extra_hosts = _list("ALLOWED_HOSTS")
    if os.getenv("RENDER_EXTERNAL_HOSTNAME"):                       # set automatically on Render
        extra_hosts.append(os.environ["RENDER_EXTERNAL_HOSTNAME"].lower())

    # Demo mode: no OpenAI calls, placeholder text. For trying the interface only.
    mock = os.getenv("PROPOSAL_AI_MOCK", "0") == "1"

    max_upload_bytes = 12 * 1024 * 1024
    idle_seconds = 15 * 60

    @property
    def ai_ready(self) -> bool:
        return self.mock or bool(self.openai_key)

    @property
    def allowed_hosts(self):
        hosts = set(self.extra_hosts)
        if not self.production:
            hosts |= {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        return hosts

    @property
    def allowed_origins(self):
        origins = set()
        for h in self.allowed_hosts:
            origins |= {f"https://{h}"} if self.production else {f"http://{h}", f"https://{h}"}
        return origins

    @property
    def setup_enabled(self) -> bool:
        return len(self.setup_token) >= 24 and not self.password_hash


cfg = Config()
