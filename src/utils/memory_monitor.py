"""Memory usage monitoring for long-running processes."""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass
from datetime import datetime

import psutil

from src.utils.logger import get_logger

logger = get_logger("memory_monitor")


@dataclass
class MemorySnapshot:
    """A snapshot of current memory usage."""

    timestamp: datetime
    rss_mb: float
    vms_mb: float
    percent: float
    available_mb: float


class MemoryMonitor:
    """Monitors memory usage and triggers alerts/GC when thresholds are exceeded."""

    def __init__(
        self,
        limit_mb: int = 1024,
        check_interval: int = 60,
        alert_threshold_percent: float = 80.0,
    ):
        self.limit_mb = limit_mb
        self.check_interval = check_interval
        self.alert_threshold_percent = alert_threshold_percent
        self._history: list[MemorySnapshot] = []
        self._running = False
        self._task: asyncio.Task | None = None

    def get_current_usage(self) -> MemorySnapshot:
        """Get current process memory usage."""
        process = psutil.Process()
        mem_info = process.memory_info()
        vm = psutil.virtual_memory()

        snapshot = MemorySnapshot(
            timestamp=datetime.now(),
            rss_mb=mem_info.rss / (1024 * 1024),
            vms_mb=mem_info.vms / (1024 * 1024),
            percent=process.memory_percent(),
            available_mb=vm.available / (1024 * 1024),
        )
        self._history.append(snapshot)

        # Keep only last 1000 snapshots
        if len(self._history) > 1000:
            self._history = self._history[-1000:]

        return snapshot

    def check_memory(self) -> bool:
        """Check if memory usage is within limits. Returns True if OK."""
        snapshot = self.get_current_usage()

        if snapshot.rss_mb > self.limit_mb:
            logger.warning(
                "Memory usage %.1f MB exceeds limit %d MB. Triggering GC.",
                snapshot.rss_mb,
                self.limit_mb,
            )
            gc.collect()
            # Re-check after GC
            snapshot = self.get_current_usage()
            if snapshot.rss_mb > self.limit_mb:
                logger.error(
                    "Memory usage still %.1f MB after GC. Limit: %d MB",
                    snapshot.rss_mb,
                    self.limit_mb,
                )
                return False

        if snapshot.percent > self.alert_threshold_percent:
            logger.warning(
                "Process memory usage at %.1f%% (threshold: %.1f%%)",
                snapshot.percent,
                self.alert_threshold_percent,
            )

        return True

    async def start_monitoring(self):
        """Start the background memory monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Memory monitor started (limit=%dMB, interval=%ds)",
            self.limit_mb,
            self.check_interval,
        )

    async def stop_monitoring(self):
        """Stop the background memory monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Memory monitor stopped")

    async def _monitor_loop(self):
        """Background loop that periodically checks memory."""
        while self._running:
            try:
                self.check_memory()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Memory monitor error: %s", exc)
                await asyncio.sleep(self.check_interval)

    def get_history(self) -> list[MemorySnapshot]:
        """Get memory usage history."""
        return list(self._history)

    def get_stats(self) -> dict:
        """Get memory usage statistics."""
        if not self._history:
            return {}
        rss_values = [s.rss_mb for s in self._history]
        return {
            "current_rss_mb": rss_values[-1],
            "min_rss_mb": min(rss_values),
            "max_rss_mb": max(rss_values),
            "avg_rss_mb": sum(rss_values) / len(rss_values),
            "samples": len(rss_values),
            "limit_mb": self.limit_mb,
        }
