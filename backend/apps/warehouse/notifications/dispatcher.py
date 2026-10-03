"""事务性发件箱投递逻辑。

职责划分：

- :func:`enqueue_notification`：在调用方事务内写入投递箱，与业务变更同生共死；
- :func:`claim_batch`：用条件 UPDATE 原子认领一批到期记录，并捞回租约过期的记录；
- :func:`process_batch`：逐条调用发送器，落库成功 / 重试退避 / 最终失败。

投递语义为“至少一次 + 幂等键”：崩溃恢复后可能重复调用发送器，但发送器以
``idempotency_key`` 去重（模拟支持幂等键的下游通道），因此不会产生重复通知；
而记录在成功 / 终败前始终可被重新认领，因此不会丢失。
"""
import logging
import threading
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from ..models import NotificationDelivery, NotificationOutbox
from .senders import InvalidRecipientError, simulated_sender

logger = logging.getLogger('apps')

# 默认投递参数，可由 Django settings 覆盖。
DEFAULT_BATCH_SIZE = getattr(settings, 'NOTIFICATION_BATCH_SIZE', 50)
DEFAULT_LEASE_SECONDS = getattr(settings, 'NOTIFICATION_LEASE_SECONDS', 30)
DEFAULT_MAX_ATTEMPTS = getattr(settings, 'NOTIFICATION_MAX_ATTEMPTS', 5)
BACKOFF_BASE = getattr(settings, 'NOTIFICATION_BACKOFF_BASE_SECONDS', 1)
BACKOFF_CAP = getattr(settings, 'NOTIFICATION_BACKOFF_CAP_SECONDS', 300)

# 进程内投递串行锁：同进程的多个作业线程不会并发写库（SQLite 对并发写敏感），
# 也顺带杜绝同进程重复投递；跨进程 / 跨节点的互斥仍由数据库条件认领保证。
_dispatch_lock = threading.RLock()


def backoff_seconds(attempt):
    """指数退避：1, 2, 4, 8 ... 封顶，attempt 为本次失败的尝试序号（从 1 起）。"""
    delay = BACKOFF_BASE * (2 ** (attempt - 1))
    return min(delay, BACKOFF_CAP)


def enqueue_notification(event_type, aggregate_ref, recipient, payload,
                         idempotency_key, channel='simulated',
                         max_attempts=DEFAULT_MAX_ATTEMPTS, using='default'):
    """在当前事务内写入一条投递箱记录。

    必须由调用方包在业务事务中调用。``idempotency_key`` 唯一：重复事件被
    数据库唯一约束挡下并复用既有记录，返回 ``(记录, False)``，绝不重复通知。
    用保存点隔离唯一约束冲突，避免污染外层业务事务。
    """
    try:
        with transaction.atomic(using=using):
            outbox = NotificationOutbox.objects.using(using).create(
                event_type=event_type,
                aggregate_ref=aggregate_ref,
                recipient=recipient,
                channel=channel,
                payload=payload,
                idempotency_key=idempotency_key,
                max_attempts=max_attempts,
            )
    except IntegrityError:
        outbox = NotificationOutbox.objects.using(using).get(
            idempotency_key=idempotency_key
        )
        return outbox, False
    return outbox, True


def claim_batch(worker_id=None, batch_size=DEFAULT_BATCH_SIZE,
                lease_seconds=DEFAULT_LEASE_SECONDS, using='default'):
    """原子认领一批记录。

    用单条条件 UPDATE 抢占：WHERE 重新校验“到期或租约过期”，配合行级写锁
    （Postgres）或库级写锁（SQLite），并发作业各拿各的，绝不重复认领。
    ``claim_token`` 令本次认领略带唯一指纹，更新后据此精确取回。
    """
    worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
    claim_token = f"{worker_id}:{uuid.uuid4().hex}"
    now = timezone.now()
    lease_until = now + timedelta(seconds=lease_seconds)

    due = Q(status=NotificationOutbox.PENDING, available_at__lte=now)
    retry_due = Q(status=NotificationOutbox.FAILED, available_at__lte=now)
    stale_lease = Q(status=NotificationOutbox.SENDING, lease_expires_at__lt=now)
    claimable = due | retry_due | stale_lease

    with transaction.atomic(using=using):
        ids = list(
            NotificationOutbox.objects.using(using)
            .filter(claimable)
            .order_by('available_at', 'id')
            .values_list('id', flat=True)[:batch_size]
        )
        if not ids:
            return []
        # 条件更新：并发对手已认领（租约未到期）的行会因 WHERE 不再匹配而落选。
        claimed_count = (
            NotificationOutbox.objects.using(using)
            .filter(id__in=ids)
            .filter(claimable)
            .update(
                status=NotificationOutbox.SENDING,
                claimed_by=claim_token,
                claimed_at=now,
                lease_expires_at=lease_until,
            )
        )
        claimed = list(
            NotificationOutbox.objects.using(using)
            .filter(claimed_by=claim_token)
            .order_by('id')
        )
    if claimed_count != len(claimed):
        # 理论上不会发生；保留计数便于排查并发异常。
        logger.warning("认领数与取回数不一致: updated=%s fetched=%s",
                       claimed_count, len(claimed))
    return claimed


def _record_delivery(outbox, attempt_no, result, detail, using):
    NotificationDelivery.objects.using(using).create(
        outbox=outbox,
        attempt_no=attempt_no,
        result=result,
        claimed_by=outbox.claimed_by,
        detail=detail,
    )


def _deliver_one(outbox, sender, using='default'):
    """处理单条已认领记录。返回 'succeeded' / 'retried' / 'dead'。"""
    attempt_no = outbox.attempts + 1
    try:
        sender.send(outbox)
    except InvalidRecipientError as exc:
        # 收件人永久失效：直接终败，不再重试，留痕可查，不丢失。
        outbox.attempts = attempt_no
        outbox.status = NotificationOutbox.DEAD
        outbox.last_error = str(exc)
        outbox.sent_at = None
        outbox.dead_at = timezone.now()
        outbox.available_at = timezone.now() + timedelta(days=3650)
        outbox.lease_expires_at = None
        outbox.save(update_fields=[
            'attempts', 'status', 'last_error', 'dead_at',
            'available_at', 'lease_expires_at', 'updated_at',
        ])
        _record_delivery(outbox, attempt_no, NotificationDelivery.DEAD, str(exc), using)
        logger.warning("通知收件人失效，转入终败: outbox=%s recipient=%s",
                       outbox.id, outbox.recipient)
        return 'dead'
    except Exception as exc:
        outbox.attempts = attempt_no
        outbox.last_error = str(exc)
        if attempt_no >= outbox.max_attempts:
            # 退避重试耗尽：最终失败，停止一切重试，避免无限重复。
            outbox.status = NotificationOutbox.DEAD
            outbox.dead_at = timezone.now()
            outbox.available_at = timezone.now() + timedelta(days=3650)
            result = NotificationDelivery.DEAD
            logger.error("通知重试耗尽，转入终败: outbox=%s attempts=%s",
                         outbox.id, attempt_no)
        else:
            delay = backoff_seconds(attempt_no)
            outbox.status = NotificationOutbox.FAILED
            outbox.available_at = timezone.now() + timedelta(seconds=delay)
            result = NotificationDelivery.RETRY
            logger.info("通知发送失败，%ss 后重试: outbox=%s attempt=%s",
                        delay, outbox.id, attempt_no)
        outbox.lease_expires_at = None
        outbox.save(update_fields=[
            'attempts', 'status', 'last_error', 'dead_at',
            'available_at', 'lease_expires_at', 'updated_at',
        ])
        _record_delivery(outbox, attempt_no, result, str(exc), using)
        return 'dead' if result == NotificationDelivery.DEAD else 'retried'
    else:
        outbox.attempts = attempt_no
        outbox.status = NotificationOutbox.SUCCEEDED
        outbox.last_error = ''
        outbox.sent_at = timezone.now()
        outbox.lease_expires_at = None
        outbox.available_at = outbox.sent_at
        outbox.save(update_fields=[
            'attempts', 'status', 'last_error', 'sent_at',
            'lease_expires_at', 'available_at', 'updated_at',
        ])
        _record_delivery(outbox, attempt_no, NotificationDelivery.SUCCESS, '', using)
        return 'succeeded'


def process_batch(worker_id=None, batch_size=DEFAULT_BATCH_SIZE,
                  lease_seconds=DEFAULT_LEASE_SECONDS, sender=None,
                  using='default'):
    """认领并处理一批，返回各项计数统计。每条记录独立事务，互不拖累。"""
    sender = sender or simulated_sender
    with _dispatch_lock:
        claimed = claim_batch(
            worker_id=worker_id, batch_size=batch_size,
            lease_seconds=lease_seconds, using=using,
        )
        stats = {'claimed': len(claimed), 'succeeded': 0, 'retried': 0, 'dead': 0}
        for outbox in claimed:
            with transaction.atomic(using=using):
                locked = (
                    NotificationOutbox.objects.using(using)
                    .select_for_update()
                    .get(pk=outbox.pk)
                )
                outcome = _deliver_one(locked, sender, using=using)
                stats[outcome] += 1
    return stats


def run_dispatcher(max_batches=None, idle_batches=1, **kwargs):
    """连续分批投递，直到没有可投递记录（或达到批次数上限）。

    主要供管理命令、进程内作业与测试驱动使用。
    """
    total = {'claimed': 0, 'succeeded': 0, 'retried': 0, 'dead': 0, 'batches': 0}
    idle = 0
    while True:
        stats = process_batch(**kwargs)
        total['batches'] += 1
        for key in ('claimed', 'succeeded', 'retried', 'dead'):
            total[key] += stats[key]
        if stats['claimed'] == 0:
            idle += 1
            if idle >= idle_batches:
                break
        else:
            idle = 0
        if max_batches is not None and total['batches'] >= max_batches:
            break
    return total
