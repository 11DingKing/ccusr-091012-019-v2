"""进程内通知投递作业。

轻量后台线程，周期性分批认领并处理投递箱。无需 Celery / Redis：
- 单实例内用线程锁保证只有一个作业循环；
- 多进程 / 多线程并发安全由数据库层条件认领保证（见 dispatcher.claim_batch）；
- 租约过期后，记录会被任意存活作业重新认领，作业崩溃不丢通知。
"""
import logging
import threading

from django.conf import settings

from .dispatcher import process_batch
from .senders import simulated_sender

logger = logging.getLogger('apps')


class NotificationWorker(threading.Thread):
    """周期性运行 :func:`process_batch` 的守护线程。"""

    def __init__(self, interval_seconds=2.0, batch_size=None,
                 lease_seconds=None, sender=None, worker_id=None):
        super().__init__(name='notification-worker', daemon=True)
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size or getattr(settings, 'NOTIFICATION_BATCH_SIZE', 50)
        self.lease_seconds = lease_seconds or getattr(
            settings, 'NOTIFICATION_LEASE_SECONDS', 30)
        self.sender = sender or simulated_sender
        self.worker_id = worker_id
        self._stop_event = threading.Event()

    def stop(self, timeout=None):
        self._stop_event.set()
        if timeout is not None:
            self.join(timeout)

    def run_once(self):
        try:
            return process_batch(
                worker_id=self.worker_id,
                batch_size=self.batch_size,
                lease_seconds=self.lease_seconds,
                sender=self.sender,
            )
        except Exception:
            logger.exception('通知投递作业本轮执行异常')
            return None

    def run(self):
        logger.info('进程内通知投递作业已启动，轮询间隔 %ss', self.interval_seconds)
        while not self._stop_event.is_set():
            stats = self.run_once()
            # 有待处理记录时尽快继续认领，空闲时按间隔休眠。
            if stats and stats['claimed'] > 0:
                continue
            self._stop_event.wait(self.interval_seconds)
        logger.info('进程内通知投递作业已停止')


_worker_lock = threading.Lock()
_worker = None


def start_worker(**kwargs):
    """启动全局唯一的进程内作业（幂等）。"""
    global _worker
    with _worker_lock:
        if _worker is not None and _worker.is_alive():
            return _worker
        _worker = NotificationWorker(**kwargs)
        _worker.start()
        return _worker


def stop_worker(timeout=5.0):
    global _worker
    with _worker_lock:
        worker = _worker
    if worker is not None:
        worker.stop(timeout=timeout)
