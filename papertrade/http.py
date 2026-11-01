import urllib.request

USER_AGENT = "Mozilla/5.0 (PaperTrade educational simulator)"


def fetch(url: str, timeout: float = 20, headers: dict | None = None) -> bytes:
    """GET a URL. Feeds take this as a parameter so tests can swap in canned responses."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()
