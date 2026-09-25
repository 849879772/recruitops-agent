"""The product's only remote model provider: DeepSeek official APIs."""
from urllib.parse import urlsplit

DEEPSEEK_BASE = "https://api.deepseek.com"
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")
DEEPSEEK_MESSAGES = DEEPSEEK_BASE + "/anthropic/v1/messages"
DEEPSEEK_RESPONSES = DEEPSEEK_BASE + "/responses"


def official_base(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    if (parsed.scheme != "https" or parsed.hostname != "api.deepseek.com"
            or parsed.port not in (None, 443)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/v1")):
        raise ValueError("Only the official DeepSeek API is supported")
    return DEEPSEEK_BASE


def official_model(value: str) -> str:
    if value not in DEEPSEEK_MODELS:
        raise ValueError("Select deepseek-flash or deepseek-v4-pro")
    return value


def official_endpoint(value: str) -> str:
    parsed = urlsplit(value.strip().rstrip("/"))
    official_base(f"{parsed.scheme}://{parsed.netloc}")
    if (parsed.query or parsed.fragment or parsed.path not in {
            "/anthropic/v1/messages", "/chat/completions", "/responses",
            "/v1/chat/completions", "/v1/responses"}):
        raise ValueError("Unsupported DeepSeek endpoint")
    return value.strip().rstrip("/")
