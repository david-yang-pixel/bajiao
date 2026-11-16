import os
import secrets


def get_or_create_token(directory: str) -> str:
    """Read the internal token file, creating it (0600) on first use."""
    path = os.path.join(directory, 'internal_token')
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        with open(path, encoding='utf-8') as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(token)
    os.chmod(path, 0o600)
    return token
