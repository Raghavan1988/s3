"""
Shared pytest setup.

Put the repository root on sys.path so unit tests can `import app...` directly.
(The boto3 integration suite doesn't need this -- it launches the server as a
subprocess from the repo root -- but the plane-level unit tests import the
modules in-process.)
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
