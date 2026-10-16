"""Chain constants for vibe/vibe on Robinhood Chain Testnet (chain id 46630).

Everything here was derived from decoded transactions on the explorer, because
the vibe/vibe contracts are not verified. Event layouts are documented in
decode.py.
"""
import os

CHAIN_ID = 46630
RPC_URL = os.environ.get("VIBESCREENER_RPC", "https://rpc.testnet.chain.robinhood.com")
EXPLORER = "https://explorer.testnet.chain.robinhood.com"
DB_PATH = os.environ.get("VIBESCREENER_DB", "vibescreener.db")

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
