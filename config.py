"""YC-only founder-angel discovery configuration."""

import os

# Database
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "investors")

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}"
    f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

# HTTP performance
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/121.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "10"))
RETRY_ATTEMPTS = int(os.getenv("RETRY_ATTEMPTS", "2"))
RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "0.5"))

# Run limits
MAX_PROFILES_PER_RUN = int(os.getenv("MAX_PROFILES_PER_RUN", "10"))
MAX_COMPANIES_TO_CHECK = int(os.getenv("MAX_COMPANIES_TO_CHECK", "30"))

FOUNDERS_TABLE = "founders"
SOURCE_NAME = "yc"

# Fallback when /companies listing is client-rendered (SPA)
YC_COMPANY_SLUGS: list[str] = [
    "airbnb",
    "stripe",
    "dropbox",
    "reddit",
    "instacart",
    "coinbase",
    "gitlab",
    "flexport",
    "brex",
    "unicornly",
    "openai",
    "doordash",
    "ginkgo-bioworks",
    "rippling",
    "deel",
    "scale",
    "ramp",
    "benchling",
    "fivetran",
    "whatnot",
    "mercury",
    "cruise",
    "twitch",
    "pagerduty",
    "zenefits",
    "mixpanel",
    "optimizely",
    "clever",
    "docker",
    "heap",
]

# Invalid company path segments (not startup pages)
YC_INVALID_COMPANY_SLUGS = frozenset(
    {
        "yc-startup-directory",
        "industry",
        "jobs",
        "founders",
        "verify",
    }
)

# --- Score-based validator ---

NAME_REJECT_SUBSTRINGS = (
    "network",
    "angels",
    "ventures",
    "capital",
    "fund",
    "alliance",
    "hub",
    "collective",
    "partners",
    "group",
    "incubator",
    "accelerator",
    "holdings",
    "investments",
    "management",
    "syndicate",
)

HARD_REJECT_PHRASES = (
    "venture capital",
    "venture capitalist",
    "general partner",
    "managing partner",
    "venture partner",
    "visiting partner",
    "group partner",
    "investment partner",
    "fund manager",
    "institutional investor",
    "principal at",
    "partner at y combinator",
    "general partner at yc",
    "president and ceo of y combinator",
    "managing director, investments",
    "vc firm",
    "vc fund",
    "angel network",
    "angel group",
    "angel forum",
    "angel squad",
    "syndicate",
    "accelerator",
    "collective",
)

FOUNDER_SIGNALS = (
    "founder",
    "co-founder",
    "built",
    "started",
    "founded",
)

INVESTOR_SIGNALS = (
    "angel investor",
    "investor",
    "invested in",
    "backed",
    "seed investor",
    "startup investor",
    "portfolio",
)

TECH_SIGNALS = (
    "software",
    "saas",
    "ai",
    "artificial intelligence",
    "machine learning",
    "developer tools",
    "devtools",
    "api",
    "apis",
    "cloud",
    "fintech",
    "payments",
    "infrastructure",
    "cybersecurity",
    "data",
    "analytics",
    "platform",
    "automation",
    "enterprise software",
    "robotics",
    "biotech",
    "healthtech",
    "devops",
    "open source",
    "engineering",
    "hardware",
    "iot",
    "web3",
    "crypto",
    "cryptocurrency",
    "blockchain",
    "protocol",
    "deep tech",
    "deeptech",
)

NON_TECH_REJECT = (
    "restaurant",
    "bakery",
    "fashion",
    "salon",
    "gym",
    "real estate",
    "construction",
    "food truck",
    "offline business",
    "apparel",
    "clothing",
    "furniture",
)

HARD_REJECT_KEYWORDS = (
    "venture capital",
    "vc",
    "gp",
    "general partner",
    "managing partner",
    "principal",
    "fund",
    "fund manager",
    "institutional investor",
    "syndicate",
    "angel network",
    "collective",
    "accelerator",
    "venture fund",
)

# Regex
EMAIL_PATTERN = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
PHONE_PATTERN = r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}"
PERSON_NAME_PATTERN = r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}$"
