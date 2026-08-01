import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TokenDecryptionError(ValueError):
    pass


def _fernet_key():
    configured = settings.KNOWLEDGE_TOKEN_ENCRYPTION_KEY.strip()
    if configured:
        try:
            Fernet(configured.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ImproperlyConfigured(
                "KNOWLEDGE_TOKEN_ENCRYPTION_KEY 必须是 Fernet.generate_key() 生成的密钥。"
            ) from exc
        return configured.encode("ascii")
    if settings.DEBUG:
        digest = hashlib.sha256(
            f"knowledge-dev-only:{settings.SECRET_KEY}".encode("utf-8")
        ).digest()
        return base64.urlsafe_b64encode(digest)
    raise ImproperlyConfigured(
        "生产环境必须配置 KNOWLEDGE_TOKEN_ENCRYPTION_KEY，令牌不能明文保存。"
    )


def encrypt_json(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return Fernet(_fernet_key()).encrypt(serialized).decode("ascii")


def decrypt_json(ciphertext):
    if not ciphertext:
        return {}
    try:
        serialized = Fernet(_fernet_key()).decrypt(ciphertext.encode("ascii"))
        return json.loads(serialized.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError) as exc:
        raise TokenDecryptionError("已保存的 Microsoft 授权令牌无法解密。") from exc
