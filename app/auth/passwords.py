"""Hash y verificación de contraseñas con argon2-cffi.

`verify` compara en tiempo constante. Un hash dummy iguala el coste cuando
el email no existe.
"""

import asyncio
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError

from app.config import Settings

_DUMMY_PASSWORD = "timing-equalization-dummy"


@lru_cache
def password_hasher(
    time_cost: int, memory_cost: int, parallelism: int
) -> PasswordHasher:
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=32,
        salt_len=16,
    )


def hasher_for(settings: Settings) -> PasswordHasher:
    return password_hasher(
        settings.auth_argon2_time_cost,
        settings.auth_argon2_memory_cost,
        settings.auth_argon2_parallelism,
    )


@lru_cache
def dummy_password_hash(time_cost: int, memory_cost: int, parallelism: int) -> str:
    return password_hasher(time_cost, memory_cost, parallelism).hash(_DUMMY_PASSWORD)


def dummy_hash_for(settings: Settings) -> str:
    return dummy_password_hash(
        settings.auth_argon2_time_cost,
        settings.auth_argon2_memory_cost,
        settings.auth_argon2_parallelism,
    )


async def hash_password(password: str, settings: Settings) -> str:
    hasher = hasher_for(settings)
    return await asyncio.to_thread(hasher.hash, password)


def _verify_sync(password: str, password_hash: str) -> bool:
    try:
        PasswordHasher().verify(password_hash, password)
    except VerifyMismatchError, VerificationError, InvalidHash, AttributeError:
        return False
    return True


async def verify_password(password: str, password_hash: str) -> bool:
    return await asyncio.to_thread(_verify_sync, password, password_hash)
