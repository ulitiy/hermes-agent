"""Authoritative text_link mentions; raw URL substrings are not triggers."""
from urllib.parse import parse_qs, urlparse


def text_link_targets_bot(url: str, bot_id, bot_username: str) -> bool:
    try:
        parsed = urlparse(url.strip())
        query = parse_qs(parsed.query)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    scheme = parsed.scheme.lower()
    if scheme == "tg":
        id_key = {"user": "id", "openmessage": "user_id"}.get(host)
        if id_key:
            try:
                return int(query.get(id_key, [""])[0]) == int(bot_id)
            except (ValueError, TypeError):
                return False
        if host == "resolve":
            target = query.get("domain", [""])[0].lstrip("@").lower()
            return bool(bot_username) and target == bot_username
        return False
    if scheme in {"http", "https"} and host in {"t.me", "telegram.me", "telegram.dog"}:
        target = parsed.path.strip("/").split("/", 1)[0].lstrip("@").lower()
        return bool(bot_username) and target == bot_username
    return False
