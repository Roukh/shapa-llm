"""Suite-wide isolation for the wiki registry (shapa/registry.py).

``init``/``bootstrap``/``fetch``/``upgrade`` register every wiki they see in
``~/.shapa/wikis.json``. Point ``$SHAPA_REGISTRY`` at a throwaway file before
any test runs, so neither in-process calls nor subprocesses that inherit the
environment ever write the real one.
"""

import atexit
import os
import shutil
import tempfile

_REGISTRY_DIR = tempfile.mkdtemp(prefix="shapa-test-registry-")
os.environ["SHAPA_REGISTRY"] = os.path.join(_REGISTRY_DIR, "wikis.json")
atexit.register(shutil.rmtree, _REGISTRY_DIR, True)
