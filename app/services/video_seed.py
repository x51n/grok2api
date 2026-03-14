"""
Video seed persistence service.
"""

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles
import orjson

from app.core.config import get_config
from app.core.logger import logger
from app.core.storage import DATA_DIR, LocalStorage, RedisStorage, SQLStorage, get_storage


VIDEO_SEED_FILE = DATA_DIR / "video_seeds.json"
REDIS_VIDEO_SEED_PREFIX = "grok2api:video_seed:"
REDIS_VIDEO_SEED_EXPIRES = "grok2api:video_seed:expires"


def _json_dumps(data: Any) -> bytes:
    return orjson.dumps(data, option=orjson.OPT_INDENT_2)


class VideoSeedService:
    _sql_schema_lock = asyncio.Lock()
    _sql_schema_ready: set[int] = set()

    @staticmethod
    def retention_days() -> int:
        raw = get_config("video_seed.retention_days", 30)
        try:
            return max(1, int(raw or 30))
        except Exception:
            return 30

    @classmethod
    def retention_ms(cls) -> int:
        return cls.retention_days() * 24 * 60 * 60 * 1000

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @classmethod
    def _normalize_video_defaults(
        cls, video_defaults: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        raw = video_defaults if isinstance(video_defaults, dict) else {}
        normalized: Dict[str, Any] = {}

        aspect_ratio = cls._normalize_text(
            raw.get("aspectRatio") or raw.get("aspect_ratio")
        )
        if aspect_ratio:
            normalized["aspectRatio"] = aspect_ratio

        video_length = raw.get("videoLength", raw.get("video_length"))
        if video_length is not None and video_length != "":
            try:
                normalized["videoLength"] = int(video_length)
            except Exception:
                pass

        resolution_name = cls._normalize_text(
            raw.get("resolutionName") or raw.get("resolution_name")
        )
        if resolution_name:
            normalized["resolutionName"] = resolution_name

        preset = cls._normalize_text(raw.get("preset"))
        if preset:
            normalized["preset"] = preset

        return normalized

    @classmethod
    def build_seed_id(
        cls,
        *,
        image_id: str = "",
        image_url_upstream: str = "",
        parent_post_id: str = "",
    ) -> str:
        base = "|".join(
            [
                cls._normalize_text(image_id),
                cls._normalize_text(image_url_upstream),
                cls._normalize_text(parent_post_id),
            ]
        )
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:24]
        return f"seed_{digest}"

    @classmethod
    def build_seed_payload(
        cls,
        *,
        image_id: str = "",
        image_url_upstream: str = "",
        parent_post_id: str = "",
        video_defaults: Optional[Dict[str, Any]] = None,
        seed_id: str = "",
        created_at: Optional[int] = None,
        last_used_at: Optional[int] = None,
        expires_at: Optional[int] = None,
    ) -> Dict[str, Any]:
        image_id = cls._normalize_text(image_id)
        image_url_upstream = cls._normalize_text(image_url_upstream)
        parent_post_id = cls._normalize_text(parent_post_id)
        normalized_defaults = cls._normalize_video_defaults(video_defaults)
        seed_id = cls._normalize_text(seed_id) or cls.build_seed_id(
            image_id=image_id,
            image_url_upstream=image_url_upstream,
            parent_post_id=parent_post_id,
        )

        now_ms = cls._now_ms()
        created_at = created_at or now_ms
        last_used_at = last_used_at or created_at
        expires_at = expires_at or (last_used_at + cls.retention_ms())

        payload: Dict[str, Any] = {
            "seed_id": seed_id,
            "image_id": image_id,
            "image_url_upstream": image_url_upstream,
            "video_defaults": normalized_defaults,
            "created_at": int(created_at),
            "last_used_at": int(last_used_at),
            "expires_at": int(expires_at),
        }
        if parent_post_id:
            payload["parent_post_id"] = parent_post_id
        return payload

    @classmethod
    async def register_seed(
        cls,
        *,
        image_id: str = "",
        image_url_upstream: str = "",
        parent_post_id: str = "",
        video_defaults: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        await cls._cleanup_expired()
        payload = cls.build_seed_payload(
            image_id=image_id,
            image_url_upstream=image_url_upstream,
            parent_post_id=parent_post_id,
            video_defaults=video_defaults,
        )
        existing = await cls._load_seed(payload["seed_id"])
        if existing:
            payload["created_at"] = int(existing.get("created_at") or payload["created_at"])
        await cls._save_seed(payload)
        return payload

    @classmethod
    async def resolve_seed(cls, seed: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(seed, dict):
            return None

        await cls._cleanup_expired()

        seed_id = cls._normalize_text(seed.get("seed_id"))
        if seed_id:
            stored = await cls._load_seed(seed_id)
            if stored:
                now_ms = cls._now_ms()
                stored["last_used_at"] = now_ms
                stored["expires_at"] = now_ms + cls.retention_ms()
                await cls._save_seed(stored)
                return stored

        parent_post_id = cls._normalize_text(seed.get("parent_post_id"))
        image_url_upstream = cls._normalize_text(seed.get("image_url_upstream"))
        if not parent_post_id and not image_url_upstream:
            return None

        payload = cls.build_seed_payload(
            seed_id=seed_id,
            image_id=cls._normalize_text(seed.get("image_id")),
            image_url_upstream=image_url_upstream,
            parent_post_id=parent_post_id,
            video_defaults=seed.get("video_defaults"),
        )
        await cls._save_seed(payload)
        return payload

    @classmethod
    async def _cleanup_expired(cls) -> int:
        storage = get_storage()
        now_ms = cls._now_ms()
        if isinstance(storage, LocalStorage):
            return await cls._delete_expired_local(now_ms)
        if isinstance(storage, RedisStorage):
            return await cls._delete_expired_redis(storage, now_ms)
        if isinstance(storage, SQLStorage):
            await cls._ensure_sql_schema(storage)
            return await cls._delete_expired_sql(storage, now_ms)
        return 0

    @classmethod
    async def _load_seed(cls, seed_id: str) -> Optional[Dict[str, Any]]:
        if not seed_id:
            return None
        storage = get_storage()
        if isinstance(storage, LocalStorage):
            return await cls._load_local(seed_id)
        if isinstance(storage, RedisStorage):
            return await cls._load_redis(storage, seed_id)
        if isinstance(storage, SQLStorage):
            await cls._ensure_sql_schema(storage)
            return await cls._load_sql(storage, seed_id)
        return None

    @classmethod
    async def _save_seed(cls, payload: Dict[str, Any]) -> None:
        storage = get_storage()
        if isinstance(storage, LocalStorage):
            await cls._save_local(payload)
            return
        if isinstance(storage, RedisStorage):
            await cls._save_redis(storage, payload)
            return
        if isinstance(storage, SQLStorage):
            await cls._ensure_sql_schema(storage)
            await cls._save_sql(storage, payload)

    @staticmethod
    async def _read_local_all() -> Dict[str, Any]:
        if not VIDEO_SEED_FILE.exists():
            return {}
        try:
            async with aiofiles.open(VIDEO_SEED_FILE, "rb") as f:
                content = await f.read()
                if not content:
                    return {}
                data = orjson.loads(content)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.warning(f"Load video seeds failed: {e}")
            return {}

    @staticmethod
    async def _write_local_all(data: Dict[str, Any]) -> None:
        VIDEO_SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = VIDEO_SEED_FILE.with_suffix(".tmp")
        async with aiofiles.open(temp_path, "wb") as f:
            await f.write(_json_dumps(data))
        Path(temp_path).replace(VIDEO_SEED_FILE)

    @classmethod
    async def _load_local(cls, seed_id: str) -> Optional[Dict[str, Any]]:
        storage = get_storage()
        async with storage.acquire_lock("video_seeds_local", timeout=10):
            data = await cls._read_local_all()
            payload = data.get(seed_id)
            return payload if isinstance(payload, dict) else None

    @classmethod
    async def _save_local(cls, payload: Dict[str, Any]) -> None:
        storage = get_storage()
        async with storage.acquire_lock("video_seeds_local", timeout=10):
            data = await cls._read_local_all()
            data[payload["seed_id"]] = payload
            await cls._write_local_all(data)

    @classmethod
    async def _delete_expired_local(cls, now_ms: int) -> int:
        storage = get_storage()
        async with storage.acquire_lock("video_seeds_local", timeout=10):
            data = await cls._read_local_all()
            expired = [
                seed_id
                for seed_id, payload in data.items()
                if isinstance(payload, dict)
                and int(payload.get("expires_at") or 0) <= now_ms
            ]
            for seed_id in expired:
                data.pop(seed_id, None)
            if expired:
                await cls._write_local_all(data)
            return len(expired)

    @classmethod
    async def _load_redis(
        cls, storage: RedisStorage, seed_id: str
    ) -> Optional[Dict[str, Any]]:
        raw = await storage.redis.get(f"{REDIS_VIDEO_SEED_PREFIX}{seed_id}")
        if not raw:
            return None
        try:
            payload = orjson.loads(raw)
            return payload if isinstance(payload, dict) else None
        except Exception as e:
            logger.warning(f"Load Redis video seed failed: {e}")
            return None

    @classmethod
    async def _save_redis(cls, storage: RedisStorage, payload: Dict[str, Any]) -> None:
        async with storage.acquire_lock("video_seeds_redis", timeout=10):
            async with storage.redis.pipeline() as pipe:
                pipe.set(
                    f"{REDIS_VIDEO_SEED_PREFIX}{payload['seed_id']}",
                    orjson.dumps(payload).decode("utf-8"),
                )
                pipe.zadd(
                    REDIS_VIDEO_SEED_EXPIRES,
                    {payload["seed_id"]: int(payload["expires_at"])},
                )
                await pipe.execute()

    @classmethod
    async def _delete_expired_redis(cls, storage: RedisStorage, now_ms: int) -> int:
        async with storage.acquire_lock("video_seeds_redis", timeout=10):
            expired = await storage.redis.zrangebyscore(
                REDIS_VIDEO_SEED_EXPIRES, min=0, max=now_ms
            )
            if not expired:
                return 0
            async with storage.redis.pipeline() as pipe:
                for seed_id in expired:
                    pipe.delete(f"{REDIS_VIDEO_SEED_PREFIX}{seed_id}")
                pipe.zrem(REDIS_VIDEO_SEED_EXPIRES, *expired)
                await pipe.execute()
            return len(expired)

    @classmethod
    async def _ensure_sql_schema(cls, storage: SQLStorage) -> None:
        storage_id = id(storage)
        if storage_id in cls._sql_schema_ready:
            return

        async with cls._sql_schema_lock:
            if storage_id in cls._sql_schema_ready:
                return
            from sqlalchemy import text

            async with storage.engine.begin() as conn:
                await conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS video_seeds (
                            seed_id VARCHAR(64) PRIMARY KEY,
                            expires_at BIGINT NOT NULL,
                            updated_at BIGINT NOT NULL,
                            data TEXT NOT NULL
                        )
                        """
                    )
                )
                if storage.dialect in ("postgres", "postgresql", "pgsql"):
                    await conn.execute(
                        text(
                            "CREATE INDEX IF NOT EXISTS idx_video_seeds_expires_at ON video_seeds (expires_at)"
                        )
                    )
                else:
                    try:
                        await conn.execute(
                            text(
                                "CREATE INDEX idx_video_seeds_expires_at ON video_seeds (expires_at)"
                            )
                        )
                    except Exception:
                        pass
            cls._sql_schema_ready.add(storage_id)

    @classmethod
    async def _load_sql(
        cls, storage: SQLStorage, seed_id: str
    ) -> Optional[Dict[str, Any]]:
        from sqlalchemy import text

        async with storage.async_session() as session:
            res = await session.execute(
                text("SELECT data FROM video_seeds WHERE seed_id=:seed_id"),
                {"seed_id": seed_id},
            )
            row = res.first()
            if not row or not row[0]:
                return None
            try:
                payload = orjson.loads(row[0]) if isinstance(row[0], str) else row[0]
                return payload if isinstance(payload, dict) else None
            except Exception as e:
                logger.warning(f"Load SQL video seed failed: {e}")
                return None

    @classmethod
    async def _save_sql(cls, storage: SQLStorage, payload: Dict[str, Any]) -> None:
        from sqlalchemy import text

        updated_at = cls._now_ms()
        async with storage.acquire_lock("video_seeds_sql", timeout=10):
            async with storage.async_session() as session:
                if storage.dialect in ("mysql", "mariadb"):
                    stmt = text(
                        """
                        INSERT INTO video_seeds (seed_id, expires_at, updated_at, data)
                        VALUES (:seed_id, :expires_at, :updated_at, :data)
                        ON DUPLICATE KEY UPDATE
                            expires_at=VALUES(expires_at),
                            updated_at=VALUES(updated_at),
                            data=VALUES(data)
                        """
                    )
                elif storage.dialect in ("postgres", "postgresql", "pgsql"):
                    stmt = text(
                        """
                        INSERT INTO video_seeds (seed_id, expires_at, updated_at, data)
                        VALUES (:seed_id, :expires_at, :updated_at, :data)
                        ON CONFLICT (seed_id) DO UPDATE SET
                            expires_at=EXCLUDED.expires_at,
                            updated_at=EXCLUDED.updated_at,
                            data=EXCLUDED.data
                        """
                    )
                else:
                    stmt = text(
                        """
                        INSERT OR REPLACE INTO video_seeds (seed_id, expires_at, updated_at, data)
                        VALUES (:seed_id, :expires_at, :updated_at, :data)
                        """
                    )
                await session.execute(
                    stmt,
                    {
                        "seed_id": payload["seed_id"],
                        "expires_at": int(payload["expires_at"]),
                        "updated_at": updated_at,
                        "data": orjson.dumps(payload).decode("utf-8"),
                    },
                )
                await session.commit()

    @classmethod
    async def _delete_expired_sql(cls, storage: SQLStorage, now_ms: int) -> int:
        from sqlalchemy import text

        async with storage.acquire_lock("video_seeds_sql", timeout=10):
            async with storage.async_session() as session:
                res = await session.execute(
                    text("SELECT seed_id FROM video_seeds WHERE expires_at <= :now_ms"),
                    {"now_ms": now_ms},
                )
                rows = res.fetchall()
                if not rows:
                    return 0
                await session.execute(
                    text("DELETE FROM video_seeds WHERE expires_at <= :now_ms"),
                    {"now_ms": now_ms},
                )
                await session.commit()
                return len(rows)
