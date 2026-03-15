import os
from cryptography.fernet import Fernet


def get_key():
    key = os.getenv('ENCRYPTION_KEY')
    if not key:
        key = Fernet.generate_key().decode()
        print(f"[encryption] WARNING: No ENCRYPTION_KEY in .env. Generated: {key}")
        print("Add to .env: ENCRYPTION_KEY=" + key)
    return key.encode() if isinstance(key, str) else key


def encrypt_password(text: str) -> str:
    return Fernet(get_key()).encrypt(text.encode()).decode()


def decrypt_password(token: str) -> str:
    return Fernet(get_key()).decrypt(token.encode()).decode()
