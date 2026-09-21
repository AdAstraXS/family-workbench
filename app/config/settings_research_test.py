"""投研模块独立离线测试设置。

不导入 config.settings、不读取 dotenv、不包含真实凭据；
数据库固定为 SQLite 内存库，缓存用 LocMem，文件存储用系统临时目录。
供 investment_research 的测试、系统检查与迁移检查使用。
"""
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "research-test-only-not-for-production"
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "family_core",
    "portfolio",
    "option_wheel",
    "ledger",
    "ipo",
    "notes",
    "knowledge",
    "reading",
    "trading_journal",
    "macro",
    "ai_analysis",
    "intelligence",
    "dashboard",
    "investment_research",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "family_core.middleware.ActiveHouseholdMemberMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls_research_test"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "family_core.context_processors.page_navigation",
                "family_core.context_processors.site_identity",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "research-test",
    }
}

PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = Path(tempfile.mkdtemp(prefix="fw-research-test-static-"))
STATICFILES_DIRS = [BASE_DIR / "static"]

MEDIA_URL = "media/"
MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="fw-research-test-media-"))

KNOWLEDGE_FILE_ROOT = Path(tempfile.mkdtemp(prefix="fw-research-test-knowledge-"))
KNOWLEDGE_MICROSOFT_CLIENT_ID = ""
KNOWLEDGE_MICROSOFT_CLIENT_SECRET = ""
KNOWLEDGE_MICROSOFT_TENANT = "common"
KNOWLEDGE_MICROSOFT_REDIRECT_URI = ""
KNOWLEDGE_MICROSOFT_SCOPES = ["Notes.Read", "User.Read"]
KNOWLEDGE_TOKEN_ENCRYPTION_KEY = ""
KNOWLEDGE_MAX_RESOURCE_BYTES = 25 * 1024 * 1024

# M2A-2 SEC EDGAR（离线测试值，不读环境变量、不含真实凭据）
RESEARCH_SEC_USER_AGENT = "research-test-agent/1.0 (offline)"
RESEARCH_SEC_DOCUMENT_MAX_BYTES = 1024 * 1024
RESEARCH_SEC_TIMEOUT_SECONDS = 2
RESEARCH_SEC_MAX_RESPONSE_BYTES = 1024 * 1024
RESEARCH_SEC_RATE_LIMIT_PER_SECOND = 5.0
RESEARCH_SEC_MAX_RETRIES = 3
RESEARCH_SEC_BACKOFF_SECONDS = 0.01

# M2A-3 Microsoft IR（离线测试值，不读环境变量、不含真实凭据）
RESEARCH_MICROSOFT_IR_TIMEOUT_SECONDS = 2
RESEARCH_MICROSOFT_IR_MAX_RESPONSE_BYTES = 1024 * 1024
RESEARCH_MICROSOFT_IR_MAX_CONTENT_CHARS = 200000

FUTU_OPEND_HOST = "localhost"
FUTU_OPEND_PORT = 11111

OPTION_WHEEL_EXECUTION_ENABLED = False

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"
