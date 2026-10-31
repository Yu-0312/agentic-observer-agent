import sys
from pathlib import Path

# The agent runs as flat scripts (python3 -u baseline_agent.py); make them importable in tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
