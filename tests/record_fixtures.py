"""Enregistre un jeu d'essai à partir d'une vraie recherche (réseau + cache local).

  python tests/record_fixtures.py <nom> "<requête>"

Le jeu est écrit dans tests/fixtures/<nom>.json.gz ; test_engine.py le rejoue.
Ré-enregistrer un jeu change ses réponses : revoir alors les attentes du test."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from therightbibtex import engine  # noqa: E402
from therightbibtex import query  # noqa: E402
import replay  # noqa: E402


async def main(name: str, text: str):
    http = replay.RecordingHttp()
    s = engine.Search(http, query.parse_input(text))
    works = []
    async for ev in s.stream():
        if ev["type"] == "results":
            works = ev["works"]
    await http.close()
    replay.save(name, text, http.log)
    print(f"{name} : {len(http.log)} réponses enregistrées, {len(works)} œuvre(s)")
    for w in works[:3]:
        rec = next(v for v in w["versions"] if v["id"] == w["recommended"])
        print(f"  {w['score']:.3f} {w['confidence'] or '-':<6} {w['title'][:70]} | {rec['label']} ({rec['status']})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
