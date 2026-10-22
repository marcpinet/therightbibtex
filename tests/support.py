"""À importer en premier dans chaque test : isole les tests de la vraie configuration
(dossier temporaire à la place du dossier de l'utilisateur, clés vides), sans réseau."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

if not os.environ.get("THERIGHTBIBTEX_TEST_HOME"):
    os.environ["THERIGHTBIBTEX_TEST_HOME"] = tempfile.mkdtemp(prefix="therightbibtex-tests-")
os.environ["THERIGHTBIBTEX_HOME"] = os.environ["THERIGHTBIBTEX_TEST_HOME"]
DATA_DIR = Path(os.environ["THERIGHTBIBTEX_HOME"])
