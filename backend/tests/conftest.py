import os
import sys
import tempfile
from pathlib import Path

# Settings are read at import time, so point the data dir somewhere disposable first.
_tmp = tempfile.mkdtemp(prefix="agentlab-test-")
os.environ["AGENTLAB_DATA_DIR"] = _tmp
os.environ["AGENTLAB_AUTH"] = "local"
os.environ["AGENTLAB_CHECK_URLS"] = "0"
os.environ["AGENTLAB_EXECUTION"] = "local"
os.environ["AGENTLAB_EXEC_TIMEOUT_S"] = "60"
os.environ["AGENTLAB_MONTHLY_CAP_USD"] = "100"
os.environ.pop("AGENTLAB_SERVER_KEYS", None)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
