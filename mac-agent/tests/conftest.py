import os
import sys
import tempfile
from pathlib import Path

os.environ["RELAY_HOME"] = tempfile.mkdtemp(prefix="relay-test-")
os.environ["RELAY_FAKE_HOST"] = "1"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
