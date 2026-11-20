"""Scripted News Replay & RabbitMQ + Dead-Letter Queue (DLQ) Pipeline (Decision D2).

Architecture:
- Producer reads `seed/news_script.jsonl` and publishes each raw line to
  exchange `news.direct` (routing_key `news.item`).
- Durable queue `news_tasks` is configured with:
    `x-dead-letter-exchange = "news.dlx"`
    `x-dead-letter-routing-key = "news.dead"`
- Consumer parses and validates each message with `NewsItemIn`.
  - Valid items are sanitized and indexed idempotently into SQLite + FTS5.
  - Malformed JSON or schema-invalid items trigger `nack(requeue=False)`, routing
    them automatically to `news.dlq` via `news.dlx` without blocking valid news.
- Includes an automatic in-memory AMQP-equivalent fallback when RabbitMQ container
  is not running locally, preserving the exact same validation, idempotency, and
  DLQ routing semantics for standalone testing.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional
import uuid

from pydantic import ValidationError

from .config import settings
from .db import NewsDatabase
from .models import NewsItemIn

logger = logging.getLogger("service_c.ingestion")


class NewsIngestionPipeline:
    """Manages RabbitMQ (or local fallback) scripted news replay, consumption, and DLQ routing."""

    def __init__(self, db: NewsDatabase) -> None:
        self.db = db
        self.broker_mode: str = "in_memory_fallback"
        self.rabbitmq_connected: bool = False
        self._amqp_conn = None
        self._amqp_channel = None
        self._replay_queues: dict[str, asyncio.Queue[dict]] = {}

    def process_raw_news_payload(self, raw_line: str) -> dict:
        """Consumer handler: validates, sanitizes, and indexes a single raw payload or routes to DLQ."""
        stripped = raw_line.strip()
        if not stripped:
            return {"status": "skipped_empty"}

        try:
            parsed = json.loads(stripped)
            if not isinstance(parsed, dict):
                raise ValueError("News payload must be a JSON object.")
            news_item = NewsItemIn.model_validate(parsed)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            error_msg = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            dlq_id = self.db.record_dlq_item(
                raw_payload=stripped,
                error_reason=error_msg,
                routing_key=settings.dlq_routing_key,
            )
            logger.warning("Routed malformed news payload to DLQ (dlq_id=%s): %s", dlq_id, error_msg)
            return {
                "status": "routed_to_dlq",
                "dlq_id": dlq_id,
                "error": error_msg,
            }

        inserted, index_ver, was_sanitized = self.db.ingest_news_item(news_item)
        return {
            "status": "indexed" if inserted else "deduplicated_noop",
            "news_id": news_item.news_id,
            "index_version": index_ver,
            "sanitized": was_sanitized,
        }

    async def init_broker(self) -> None:
        """Connects to RabbitMQ and declares `news.direct`, `news_tasks`, `news.dlx`, and `news.dlq`."""
        if not settings.rabbitmq_enabled:
            self.broker_mode = "in_memory_fallback"
            self.rabbitmq_connected = False
            return

        try:
            import aio_pika  # type: ignore

            self._amqp_conn = await asyncio.wait_for(
                aio_pika.connect_robust(settings.rabbitmq_url),
                timeout=2.5,
            )
            self._amqp_channel = await self._amqp_conn.channel()
            await self._amqp_channel.set_qos(prefetch_count=10)

            # 1. Declare Dead-Letter Exchange (news.dlx) and Dead-Letter Queue (news.dlq)
            dlx = await self._amqp_channel.declare_exchange(
                settings.dlx_exchange,
                aio_pika.ExchangeType.DIRECT,
                durable=True,
            )
            dlq = await self._amqp_channel.declare_queue(
                settings.dlq_queue,
                durable=True,
            )
            await dlq.bind(dlx, routing_key=settings.dlq_routing_key)

            # 2. Declare primary exchange (news.direct) and queue (news_tasks) with DLX arguments
            exchange = await self._amqp_channel.declare_exchange(
                settings.news_exchange,
                aio_pika.ExchangeType.DIRECT,
                durable=True,
            )
            queue = await self._amqp_channel.declare_queue(
                settings.news_queue,
                durable=True,
                arguments={
                    "x-dead-letter-exchange": settings.dlx_exchange,
                    "x-dead-letter-routing-key": settings.dlq_routing_key,
                },
            )
            await queue.bind(exchange, routing_key=settings.news_routing_key)

            # 3. Attach consumer to `news_tasks`
            async def _on_amqp_message(message) -> None:
                raw_body = message.body.decode("utf-8", errors="replace")
                outcome = self.process_raw_news_payload(raw_body)
                headers = getattr(message, "headers", None) or {}
                replay_id = headers.get("replay_id")
                if isinstance(replay_id, bytes):
                    replay_id = replay_id.decode("utf-8", errors="ignore")
                if isinstance(replay_id, str) and replay_id in self._replay_queues:
                    await self._replay_queues[replay_id].put(outcome)
                if outcome.get("status") == "routed_to_dlq":
                    # Reject with requeue=False -> RabbitMQ routes message via news.dlx to news.dlq
                    await message.nack(requeue=False)
                else:
                    await message.ack()

            await queue.consume(_on_amqp_message)

            # 4. Also consume from `news.dlq` in read/mirror mode if messages arrive externally
            self.broker_mode = "rabbitmq_amqp"
            self.rabbitmq_connected = True
            logger.info("Connected to RabbitMQ (%s) with DLX/DLQ topology ready.", settings.rabbitmq_url)
        except Exception as exc:
            self.broker_mode = "in_memory_fallback"
            self.rabbitmq_connected = False
            logger.info(
                "RabbitMQ not reachable (%s); operating in deterministic local queue mode with DLQ persistence.",
                exc,
            )

    async def close(self) -> None:
        if self._amqp_conn is not None:
            try:
                await self._amqp_conn.close()
            except Exception:
                pass
            self._amqp_conn = None
            self.rabbitmq_connected = False

    @staticmethod
    def _tally_outcomes(outcomes: list[dict]) -> tuple[int, int, int, int]:
        indexed_new = 0
        deduplicated = 0
        routed_to_dlq = 0
        sanitized_count = 0
        for outcome in outcomes:
            st = outcome.get("status")
            if st == "indexed":
                indexed_new += 1
            elif st == "deduplicated_noop":
                deduplicated += 1
            elif st == "routed_to_dlq":
                routed_to_dlq += 1
            if outcome.get("sanitized"):
                sanitized_count += 1
        return indexed_new, deduplicated, routed_to_dlq, sanitized_count

    async def publish_raw_line(self, raw_line: str, headers: Optional[dict] = None) -> dict:
        """Publishes a raw JSONL line through RabbitMQ (if connected) or local consumer pipeline."""
        if self.rabbitmq_connected and self._amqp_channel is not None:
            try:
                import aio_pika  # type: ignore

                exchange = await self._amqp_channel.get_exchange(settings.news_exchange)
                await exchange.publish(
                    aio_pika.Message(
                        body=raw_line.encode("utf-8"),
                        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                        headers=headers,
                    ),
                    routing_key=settings.news_routing_key,
                )
                # Give async consumer brief moment to process for synchronous API callers
                await asyncio.sleep(0.02)
                return {"published_via": "rabbitmq_amqp"}
            except Exception as exc:
                logger.warning("AMQP publish failed (%s); falling back to direct processor.", exc)

        return self.process_raw_news_payload(raw_line)

    async def replay_script_file(self, script_path: Optional[str] = None) -> dict:
        """Replays all lines from `seed/news_script.jsonl` into the ingestion pipeline."""
        path = Path(script_path or settings.seed_news_path)
        if not path.exists():
            return {
                "replayed_lines": 0,
                "indexed_new": 0,
                "deduplicated": 0,
                "routed_to_dlq": 0,
                "sanitized_injections": 0,
                "error": f"Script file not found: {path}",
            }

        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        outcomes: list[dict] = []

        if self.rabbitmq_connected and self._amqp_channel is not None:
            replay_id = uuid.uuid4().hex
            queue: asyncio.Queue[dict] = asyncio.Queue()
            self._replay_queues[replay_id] = queue
            try:
                amqp_expected = 0
                for line in lines:
                    res = await self.publish_raw_line(line, headers={"replay_id": replay_id})
                    if "status" in res:
                        outcomes.append(res)
                    else:
                        amqp_expected += 1
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2.5
                while amqp_expected > 0:
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        outcomes.append(await asyncio.wait_for(queue.get(), timeout=remaining))
                        amqp_expected -= 1
                    except asyncio.TimeoutError:
                        break
            finally:
                self._replay_queues.pop(replay_id, None)
        else:
            for line in lines:
                outcomes.append(self.process_raw_news_payload(line))

        indexed_new, deduplicated, routed_to_dlq, sanitized_count = self._tally_outcomes(outcomes)

        return {
            "broker_mode": self.broker_mode,
            "script_file": str(path.name),
            "replayed_lines": len(lines),
            "indexed_new": indexed_new,
            "deduplicated": deduplicated,
            "routed_to_dlq": routed_to_dlq,
            "sanitized_injections": sanitized_count,
            "index_version": self.db.get_index_version(),
            "dlq_count": len(self.db.list_dlq_items()),
        }
