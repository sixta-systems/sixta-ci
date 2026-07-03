"""Minimal settings for the sixta-review demo — just enough for sqlmigrate.

Database config comes from DATABASE_URL (as set by the sixta-review CI job
template) without external dependencies.
"""

import os
from urllib.parse import urlparse

SECRET_KEY = "demo-not-secret"
DEBUG = False
INSTALLED_APPS = ["shop"]
USE_TZ = True

_url = urlparse(os.environ.get("DATABASE_URL", "postgres://sixta_ci:sixta_ci@localhost:5432/sixta_ci"))
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _url.path.lstrip("/") or "sixta_ci",
        "USER": _url.username or "",
        "PASSWORD": _url.password or "",
        "HOST": _url.hostname or "localhost",
        "PORT": str(_url.port or 5432),
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
