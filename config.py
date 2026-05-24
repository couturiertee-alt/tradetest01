import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent
TRANSCRIPTS_DIR = ROOT / "transcripts"
STRATEGIES_DIR = ROOT / "strategies" / "output"
DB_PATH = ROOT / "db" / "strategies.db"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"

EXCHANGE_NAME = os.getenv("EXCHANGE_NAME", "binance")
EXCHANGE_API_KEY = os.getenv("EXCHANGE_API_KEY", "")
EXCHANGE_SECRET = os.getenv("EXCHANGE_SECRET", "")

# Ensure dirs exist at import time
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
STRATEGIES_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
