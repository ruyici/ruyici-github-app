import hashlib
import hmac


def sign_payload(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, sig_header: str) -> bool:
    if not sig_header or not sig_header.lower().startswith("sha256="):
        return False
    provided = sig_header.split("=", 1)[1]
    expected = sign_payload(secret, body)
    return hmac.compare_digest(provided, expected)
