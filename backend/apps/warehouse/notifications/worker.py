"""
进程内通知投递作业。

- run_worker：阻塞式循环，供管理命令 process_notifications 使用；
- start_background_worker：在应用进程内启动守护线程（settings.NOTIFICATION_WORKER_ENABLED=true）。

作业只做“认领-发送-记账”，自身崩溃也不影响数据：未完成记录的租约到期后，
下一轮（或另一进程）会重新认领，已成功收件人通过 delivered_recipients 去重。
"""
import logging
import threading
import time

from django.conf import settings
from django.utils import timezone

from .outbox import run_delivery_batch
from .senders import get_sender

logger = logging.getLogger('apps')

_started_lock = threading.Lock()
_background_started = False


def run_worker(max_loops=None, interval_seconds=None, batch_size=None,
               lease_seconds=None, owner=None):
    """
    阻塞式运行投递循环。

    max_loops 为 None 时持续运行；否则执行指定轮次后返回（测试/一次性处理用）。
    返回各轮统计之和。
    """
    interval = interval_seconds or getattr(settings, 'NOTIFICATION_WORKER_INTERVAL_SECONDS', 5)
    sender = get_sender()
    totals = {'claimed': 0, 'succeeded': 0, 'retry': 0, 'dead': 0, 'loops': 0}
    loops = 0
    while max_loops is None or loops < max_loops:
        try:
            stats = run_delivery_batch(
                sender=sender, owner=owner,
                batch_size=batch_size, lease_seconds=lease_seconds,
            )
            totals['claimed'] += stats['claimed']
            totals['succeeded'] += stats['succeeded']
            totals['retry'] += stats['retry']
            totals['dead'] += stats['dead']
        except Exception:
            # 作业循环不能因单轮异常退出；长驻线程中的断连也在下一轮自动重连
            logger.exception('通知投递作业轮询异常')
        finally:
            # 长驻线程不长期占用数据库连接，避免 MySQL/代理侧闲置断连
            if max_loops is None:
                from django.db import connection
                connection.close_if_unusable_or_obsolete()
        loops += 1
        totals['loops'] = loops
        if max_loops is None or loops < max_loops:
            time.sleep(interval)
    return totals


def start_background_worker():
    """在当前进程启动唯一的后台投递守护线程。"""
    global _background_started
    if not getattr(settings, 'NOTIFICATION_WORKER_ENABLED', False):
        return None
    with _started_lock:
        if _background_started:
            return None
        _background_started = True

    interval = getattr(settings, 'NOTIFICATION_WORKER_INTERVAL_SECONDS', 5)

    def _loop():
        logger.info('进程内通知投递作业已启动，轮询间隔 %s 秒', interval)
        run_worker(interval_seconds=interval)

    thread = threading.Thread(
        target=_loop, name='notification-outbox-worker', daemon=True,
    )
    thread.start()
    return thread
