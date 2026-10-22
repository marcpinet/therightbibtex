"""Vérification d'une clé d'API auprès de son service, avant de l'enregistrer. Une panne
réseau n'empêche pas de l'enregistrer ; aucune vérification ne consomme de recherche."""
from __future__ import annotations

import httpx

SERPAPI = "https://serpapi.com"
S2_API = "https://api.semanticscholar.org/graph/v1"
OPENALEX_API = "https://api.openalex.org/works"


async def check_key(client: httpx.AsyncClient, service: str, value: str) -> str:
    """« » si la clé est acceptée (ou invérifiable pour l'instant), sinon l'erreur."""
    try:
        if service == "serpapi":
            r = await client.get(f"{SERPAPI}/account.json", params={"api_key": value}, timeout=10)
            if r.status_code in (401, 403) or (r.status_code == 200 and r.json().get("error")):
                return "SerpAPI rejects this key"
        elif service == "s2":
            r = await client.get(f"{S2_API}/paper/arXiv:1706.03762", params={"fields": "title"},
                                 headers={"x-api-key": value}, timeout=10)
            if r.status_code in (401, 403):
                return "Semantic Scholar rejects this key"
        elif service == "openalex":
            r = await client.get(f"{OPENALEX_API}/W2626778328", params={"select": "id", "api_key": value},
                                 timeout=10)
            if r.status_code in (401, 403):
                return "OpenAlex rejects this key"
    except (httpx.HTTPError, ValueError):
        pass
    return ""
