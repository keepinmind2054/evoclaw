import asyncio
import logging
logger = logging.getLogger(__name__)


class VectorIngestor:
    BATCH_SIZE = 10
    INTERVAL_SECS = 30

    def __init__(self, vector_store, db_module):
        self._vs = vector_store  # VectorStore instance
        self._db = db_module
        self._running = False

    async def run_forever(self):
        self._running = True
        logger.info("VectorIngestor started")
        while self._running:
            try:
                count = await self._ingest_pending()
                if count:
                    logger.info("VectorIngestor: vectorized %d entries", count)
            except Exception as e:
                logger.error("VectorIngestor error: %s", e)
            await asyncio.sleep(self.INTERVAL_SECS)

    async def stop(self):
        self._running = False

    async def _ingest_pending(self) -> int:
        entries = self._db.get_unvectorized_warm_logs(limit=self.BATCH_SIZE)
        count = 0
        for entry in entries:
            try:
                if not self._vs._available:
                    break
                await self._vs.store(
                    entry["id"],
                    entry["content"],
                    entry["jid"],
                    "private",
                )
                self._db.mark_warm_log_vectorized(entry["id"])
                count += 1
            except Exception as e:
                logger.warning("VectorIngestor: failed entry %s: %s", entry["id"], e)
        return count
