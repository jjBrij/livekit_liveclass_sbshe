
from pathlib import Path
import os
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from project root
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Core security settings (from environment)
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "unsafe-dev-key-change-me")
DEBUG = os.environ.get("DEBUG", "True").lower() == "true"

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------
INSTALLED_APPS = [
    # Django defaults
     "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third-party
    "rest_framework",
    "corsheaders",
   
    "channels",

    # Local apps
    "apps.core",
    "apps.classes",
    "apps.livekit",
    "apps.realtime",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",         
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",     
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"   # used from Block 9

# ---------------------------------------------------------------------------
# Database — SQLite for Block 1 only.
# Block 2 switches to PostgreSQL via DATABASE_URL.
# ---------------------------------------------------------------------------
import dj_database_url
DATABASE_URL = os.environ.get("DATABASE_URL", "")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Add it to your .env file. "
        "Example: postgres://liveclass:liveclass@localhost:5432/liveclass"
    )

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,        # keep connections alive for 10 minutes
        conn_health_checks=True, # verify dead connections before reuse
    )
}
# ---------------------------------------------------------------------------
# Password validation (kept default; we do not use Django auth for users)
# ---------------------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# ---------------------------------------------------------------------------
# Logging — structured, console-only for now. Extended in Block 24.
# ---------------------------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "apps": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"     # adjust to your primary audience
USE_I18N = True
USE_TZ = True                  # always store UTC in DB

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.core.authentication.AuthServerAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "apps.core.permissions.IsAuthenticatedViaAuthServer",
    ],
    "EXCEPTION_HANDLER": "apps.core.exceptions.envelope_exception_handler",
    "UNAUTHENTICATED_USER": None,
}

CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = []       # populate in production

# ---------------------------------------------------------------------------
# App-specific placeholders (read now, used later)
# ---------------------------------------------------------------------------
AUTH_SERVER_URL = os.environ.get("AUTH_SERVER_URL", "")

REDIS_URL = os.environ.get("REDIS_URL", "")



LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "")
LIVEKIT_TOKEN_TTL_SECONDS = int(os.environ.get("LIVEKIT_TOKEN_TTL_SECONDS", 6 * 60 * 60))
def _derive_livekit_http_url(ws_url: str) -> str:
    if not ws_url:
        return ""
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://"):]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://"):]
    return ws_url

LIVEKIT_HTTP_URL = _derive_livekit_http_url(LIVEKIT_URL)
ASGI_APPLICATION = "config.asgi.application"
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
            # Keep small in dev; tune in Block 20.
            "capacity": 1500,
            "expiry": 60,
        },
    },
}


# Webhook path (used only for documentation; the actual route is in URLs)
LIVEKIT_WEBHOOK_PATH = "/api/livekit/webhooks/egress/"

# S3-compatible storage (used by LiveKit Egress server-side; Django reads these
# in Block 15 for presigned URLs).
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_REGION = os.environ.get("S3_REGION", "")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "")

# Default layout for RoomComposite Egress.
LIVEKIT_EGRESS_DEFAULT_LAYOUT = os.environ.get("LIVEKIT_EGRESS_DEFAULT_LAYOUT", "grid")