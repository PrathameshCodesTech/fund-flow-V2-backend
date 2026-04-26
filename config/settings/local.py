from .base import *

DEBUG = True

ALLOWED_HOSTS = ["*"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

CORS_ALLOW_ALL_ORIGINS = True

# Email (Gmail SMTP)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_USE_SSL = False
EMAIL_TIMEOUT = 10
EMAIL_HOST_USER = "solodevbuilds@gmail.com"
EMAIL_HOST_PASSWORD = "qouh exkt ywep gwxq"
DEFAULT_FROM_EMAIL = "InvoFlow <solodevbuilds@gmail.com>"

# Vendor portal
VENDOR_PORTAL_BASE_URL = "http://localhost:8080"
