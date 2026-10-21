"""Chain constants for vibe/vibe on Robinhood Chain Testnet (chain id 46630).

Everything here was derived from decoded transactions on the explorer, because
the vibe/vibe contracts are not verified. Event layouts are documented in
decode.py.

Settings come from environment variables, so on Railway (or any host) you add
them in the service's Variables tab instead of editing code:

    ALCHEMY_API_KEY     just the key; the RPC URL is built from it (simplest)
    VIBESCREENER_RPC    a full RPC URL; wins over ALCHEMY_API_KEY
    VIBESCREENER_DB     database file path; wins over the Railway volume path
    VIBESCREENER_POLL   seconds between polls in follow/run mode (default 3)
    PORT                dashboard port (Railway sets it; default 8000)
    IPFS_GATEWAY        preferred IPFS gateway for token metadata and images (optional)
    VIBESCREENER_META   set to 0 to turn token metadata fetching off
    LLM_API_KEY         key for an OpenAI-compatible AI provider (or OPENROUTER_API_KEY); enables AI wording
    LLM_BASE_URL / LLM_MODEL   provider URL (default OpenRouter) and model name(s), comma separated
    ASK_FREE_PER_DAY / ASK_GLOBAL_PER_DAY   free questions per visitor and in total per day (default 5 / 40)
    BRIEF_EVERY_HOURS   how often the market brief is rewritten (default 3)
"""
import os

CHAIN_ID = 46630
PUBLIC_RPC = "https://rpc.testnet.chain.robinhood.com"
ALCHEMY_RPC = "https://robinhood-testnet.g.alchemy.com/v2/{key}"
EXPLORER = "https://explorer.testnet.chain.robinhood.com"


def _clean(value):
    """Env values pasted into dashboards often carry stray spaces, newlines or quotes."""
    return (value or "").strip().strip("'\"").strip()


def resolve_rpc_url(env=None):
    env = os.environ if env is None else env
    full = _clean(env.get("VIBESCREENER_RPC"))
    if full:
        return full
    key = _clean(env.get("ALCHEMY_API_KEY"))
    if key.lower().startswith("http"):
        return key.rstrip("/")            # a whole URL was pasted into the key variable
    if "/v2/" in key:
        key = key.split("/v2/")[-1].strip("/")
    if key:
        return ALCHEMY_RPC.format(key=key)
    return PUBLIC_RPC


def resolve_db_path(env=None):
    env = os.environ if env is None else env
    explicit = _clean(env.get("VIBESCREENER_DB"))
    if explicit:
        return explicit
    # Railway sets this itself when a volume is attached to the service.
    mount = _clean(env.get("RAILWAY_VOLUME_MOUNT_PATH"))
    if mount:
        return os.path.join(mount, "vibescreener.db")
    return "vibescreener.db"


def on_railway_without_storage(env=None):
    """True when running on Railway with nowhere persistent to keep the database."""
    env = os.environ if env is None else env
    return bool(env.get("RAILWAY_ENVIRONMENT_NAME") or env.get("RAILWAY_SERVICE_NAME")) and not (
        _clean(env.get("VIBESCREENER_DB")) or _clean(env.get("RAILWAY_VOLUME_MOUNT_PATH")))


def poll_seconds(env=None):
    env = os.environ if env is None else env
    try:
        return max(0.5, float(_clean(env.get("VIBESCREENER_POLL")) or 3.0))
    except ValueError:
        return 3.0


def _gateways(env=None):
    env = os.environ if env is None else env
    own = _clean(env.get("IPFS_GATEWAY"))
    base = ["https://ipfs.io/ipfs/", "https://gateway.pinata.cloud/ipfs/", "https://dweb.link/ipfs/"]
    return ([own.rstrip("/") + "/"] if own.startswith("http") else []) + [g for g in base if g != own]


def meta_disabled(env=None):
    env = os.environ if env is None else env
    return _clean(env.get("VIBESCREENER_META")).lower() in ("0", "false", "off", "no")


DEFAULT_MODELS = "meta-llama/llama-3.3-70b-instruct:free,openai/gpt-oss-120b:free"


def llm_settings(env=None):
    """Any OpenAI-compatible provider. Defaults to OpenRouter's free models; switch provider by
    changing LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in the Variables tab, no code change."""
    env = os.environ if env is None else env
    models = [m.strip() for m in _clean(env.get("LLM_MODEL") or DEFAULT_MODELS).split(",") if m.strip()]
    return {"base_url": (_clean(env.get("LLM_BASE_URL")) or "https://openrouter.ai/api/v1").rstrip("/"),
            "key": _clean(env.get("LLM_API_KEY") or env.get("OPENROUTER_API_KEY")),
            "models": models or DEFAULT_MODELS.split(",")}


def _float(env, name, default, lo, hi):
    try:
        return max(lo, min(hi, float(_clean(env.get(name)) or default)))
    except ValueError:
        return default


def brief_hours(env=None):
    env = os.environ if env is None else env
    return _float(env, "BRIEF_EVERY_HOURS", 3.0, 0.5, 48.0)


def ask_limits(env=None):
    """Daily caps for the free "Ask" box: per visitor, and for the whole site (protects the AI quota)."""
    env = os.environ if env is None else env
    return (int(_float(env, "ASK_FREE_PER_DAY", 5, 0, 1000)), int(_float(env, "ASK_GLOBAL_PER_DAY", 40, 1, 100000)))


def web_port(env=None):
    """Railway sets PORT for the public domain; VIBESCREENER_PORT overrides it."""
    env = os.environ if env is None else env
    for key in ("VIBESCREENER_PORT", "PORT"):
        try:
            return int(_clean(env.get(key)))
        except ValueError:
            continue
    return 8000


IPFS_GATEWAYS = _gateways()
RPC_URL = resolve_rpc_url()
DB_PATH = resolve_db_path()

# Contracts (lower-case)
FACTORY = "0xe794217880011f9ca6961340ed5c16ec9559fea0"  # emits one event per token launch
ROUTER = "0x2784448c519d01d3ae257c0aac0b7cdad8acceb1"   # entry point users call; emits one event per trade
VIBEVIBE_TOKEN = "0xf1f6b050550531226ee0257fec4a1e575ff4c341"  # quote token of some launches (e.g. JHJ)
ZERO = "0x" + "0" * 40

# Event topic0 hashes (observed on-chain)
TOPIC_LAUNCH = "0xa7e8032bfd07a9fbcde50eabe91eb2901faee6dbddd9cced579491d9b07ef5c8"      # factory
TOPIC_ROUTER = "0x008fbdc271d05d94425bb2599d7ec318a396bd1b5335b81fda077494ac0dae5c"      # router
TOPIC_CURVE_BUY = "0x55867fe9a84ea30ad247ef1bb703eba47fd9af476149002512a2814b1e3d8fb5"   # per-token curve
TOPIC_CURVE_SELL = "0x3899b2b9ba4dfe77b54ff5e6f921672b2a7563ade468f2a106b25a4b808d60de"  # per-token curve

WEI = 10 ** 18
