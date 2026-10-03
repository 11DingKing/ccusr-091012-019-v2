"""
通知投递箱核心服务。

投递语义：
- 事件登记（enqueue_*）必须在业务事务内调用，业务成功与箱记录写入原子提交；
- (event_type, dedup_key) 唯一约束是重复事件的最后防线，并发/重试登记至多一条；
- 投递作业通过条件 UPDATE 分批认领（无需 SELECT ... FOR UPDATE，SQLite 亦可运行），
  崩溃遗留的 processing 记录在租约到期后可被其他作业实例重新认领；
- 临时失败按指数退避重试，达到 max_attempts 后置为 failed（死信），不会无限重复；
- 失效收件人（空号/已注销）从收件人列表中永久剔除并记入流水，不会阻塞其余收件人；
- delivered_recipients 持久化已成功收件人，租约恢复后不会对其重复发送。
"""
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from ..models import (
    CustodyGoods,
    CustodyStatusLog,
    NotificationDelivery,
    NotificationOutbox,
)
from .senders import TransientSendError, get_sender

logger = logging.getLogger('apps')


def worker_identity():
    """生成作业实例标识：进程号 + 线程内唯一短码。"""
    return f"{uuid.uuid4().hex[:12]}"


def backoff_delay_seconds(attempts):
    """指数退避：base * 2**(attempts-1)，并限制在 cap 内。"""
    base = getattr(settings, 'NOTIFICATION_BACKOFF_BASE_SECONDS', 30)
    cap = getattr(settings, 'NOTIFICATION_BACKOFF_CAP_SECONDS', 1800)
    return min(cap, base * (2 ** max(0, attempts - 1)))


def custody_dedup_key(goods_id, log_id):
    return f"custody-{goods_id}-log-{log_id}"


def enqueue_event(event_type, dedup_key, payload, recipients, max_attempts=None):
    """
    在当前事务内登记一条投递箱记录。

    必须由调用方包裹在 transaction.atomic() 中。唯一约束保证同一事件不会重复登记；
    若并发事务抢先插入，则取回既有记录（至少一次登记、绝不重复通知）。
    """
    full_payload = dict(payload)
    full_payload['dedup_key'] = dedup_key
    try:
        # 内层保存点：并发下唯一约束冲突只回滚保存点，外层业务事务不受污染
        with transaction.atomic():
            return NotificationOutbox.objects.create(
                event_type=event_type,
                dedup_key=dedup_key,
                payload=full_payload,
                recipients=list(recipients),
                max_attempts=max_attempts or getattr(settings, 'NOTIFICATION_MAX_ATTEMPTS', 5),
            )
    except IntegrityError:
        existing = NotificationOutbox.objects.filter(
            event_type=event_type, dedup_key=dedup_key
        ).first()
        if existing is not None:
            return existing
        raise


# ==================== 监管物资状态变化领域服务 ====================

class TransitionError(ValueError):
    """非法状态流转。"""


def default_recipients():
    """默认通知在职管理员（超级管理员/管理员）的用户名。"""
    from apps.authentication.models import User
    return list(
        User.objects.filter(is_active=True, role__in=['superadmin', 'admin'])
        .values_list('username', flat=True)
    )


def change_custody_status(goods, to_status, operator, remark='',
                          recipients=None, idempotency_key=None):
    """
    在同一数据库事务内：写入状态日志、更新物资状态、登记通知投递箱。

    返回 (goods, log, outbox, duplicate)。
    - 传入 idempotency_key 且事件已存在时，直接返回既有记录，不产生新日志/新通知；
    - 任一步骤失败整体回滚，不会出现“业务成功却无待通知记录”。
    """
    if to_status not in dict(CustodyGoods.STATUS_CHOICES):
        raise TransitionError(f'未知状态: {to_status}')

    # 幂等重试：键已存在时直接返回首次结果（即使本次请求体与首次相同、
    # 当前物资状态已经变化，也不能再报“无需变更”）。
    if idempotency_key:
        existing_log = CustodyStatusLog.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existing_log is not None:
            existing_goods = CustodyGoods.objects.get(pk=existing_log.custody_goods_id)
            outbox = NotificationOutbox.objects.get(
                event_type=NotificationOutbox.EVENT_CUSTODY_STATUS_CHANGED,
                dedup_key=custody_dedup_key(existing_log.custody_goods_id, existing_log.id),
            )
            return existing_goods, existing_log, outbox, True

    with transaction.atomic():
        if idempotency_key:
            try:
                # 内层保存点：两个请求并发使用同一新键时，唯一约束只放行一个，
                # 冲突方回滚保存点后取回既有记录，不污染外层事务。
                with transaction.atomic():
                    goods, log, outbox = _do_change(
                        goods, to_status, operator, remark,
                        recipients, idempotency_key,
                    )
                return goods, log, outbox, False
            except IntegrityError:
                existing_log = CustodyStatusLog.objects.filter(
                    idempotency_key=idempotency_key
                ).first()
                if existing_log is None:
                    raise
                existing_goods = CustodyGoods.objects.get(pk=existing_log.custody_goods_id)
                outbox = NotificationOutbox.objects.get(
                    event_type=NotificationOutbox.EVENT_CUSTODY_STATUS_CHANGED,
                    dedup_key=custody_dedup_key(existing_log.custody_goods_id, existing_log.id),
                )
                return existing_goods, existing_log, outbox, True

        goods, log, outbox = _do_change(
            goods, to_status, operator, remark, recipients, None
        )
        return goods, log, outbox, False


def _do_change(goods, to_status, operator, remark, recipients, idempotency_key):
    from_status = goods.status
    if from_status == to_status:
        raise TransitionError('物资已是该状态，无需变更')

    log = CustodyStatusLog(
        custody_goods=goods,
        from_status=from_status,
        to_status=to_status,
        operator=operator,
        remark=remark,
        idempotency_key=idempotency_key,
    )
    log.save()

    goods.status = to_status
    goods.save(update_fields=['status', 'updated_at'])

    recipient_list = recipients if recipients is not None else default_recipients()
    outbox = enqueue_event(
        event_type=NotificationOutbox.EVENT_CUSTODY_STATUS_CHANGED,
        dedup_key=custody_dedup_key(goods.pk, log.id),
        payload={
            'goods_id': goods.pk,
            'goods_name': goods.name,
            'goods_code': goods.code,
            'from_status': from_status,
            'to_status': to_status,
            'from_status_display': dict(CustodyGoods.STATUS_CHOICES).get(from_status, from_status),
            'to_status_display': dict(CustodyGoods.STATUS_CHOICES).get(to_status, to_status),
            'operator': operator.username if operator else '',
            'remark': remark,
        },
        recipients=recipient_list,
    )
    return goods, log, outbox


# ==================== 投递作业 ====================

def _due_condition(now):
    from django.db.models import Q
    return (
        Q(status=NotificationOutbox.STATUS_PENDING, available_at__lte=now)
        | Q(status=NotificationOutbox.STATUS_PROCESSING, lease_expires_at__lt=now)
    )


def claim_batch(owner=None, batch_size=None, lease_seconds=None, now=None):
    """
    分批认领到期记录：待投递且到可认领时间，或租约已过期的投递中记录。

    采用“候选查询 + 带前置条件的 UPDATE”：两个作业实例并发时，
    UPDATE 的 WHERE 会重新校验状态/租约，每条记录只会被一个实例抢到。
    """
    now = now or timezone.now()
    owner = owner or worker_identity()
    batch_size = batch_size or getattr(settings, 'NOTIFICATION_BATCH_SIZE', 20)
    lease_seconds = lease_seconds or getattr(settings, 'NOTIFICATION_LEASE_SECONDS', 60)
    expiry = now + timedelta(seconds=lease_seconds)

    candidate_ids = list(
        NotificationOutbox.objects.filter(_due_condition(now))
        .order_by('id')
        .values_list('id', flat=True)[:batch_size]
    )
    if not candidate_ids:
        return []

    with transaction.atomic():
        # 前置条件在 UPDATE 时重新判定，杜绝两个作业同时认领到同一行
        NotificationOutbox.objects.filter(id__in=candidate_ids)\
            .filter(_due_condition(now))\
            .update(
                status=NotificationOutbox.STATUS_PROCESSING,
                lease_owner=owner,
                lease_expires_at=expiry,
                attempts=F('attempts') + 1,
            )
        claimed = list(
            NotificationOutbox.objects.filter(
                id__in=candidate_ids,
                status=NotificationOutbox.STATUS_PROCESSING,
                lease_owner=owner,
                lease_expires_at=expiry,
            ).order_by('id')
        )
    return claimed


def _record_delivery(outbox, result, error='', success_count=0,
                     invalid_recipients=None, failed_recipients=None):
    NotificationDelivery.objects.create(
        outbox=outbox,
        attempt=outbox.attempts,
        result=result,
        lease_owner=outbox.lease_owner or '',
        success_count=success_count,
        invalid_recipients=invalid_recipients or [],
        failed_recipients=failed_recipients or [],
        error=error,
    )


def _finish_success(outbox, delivered_now, invalid):
    delivered = sorted(set(outbox.delivered_recipients or []) | set(delivered_now))
    outbox.delivered_recipients = delivered
    # 失效收件人永久剔除；已成功收件人也从待发列表移除（租约恢复后不重复发送）
    outbox.recipients = [
        r for r in outbox.recipients
        if r not in invalid and r not in set(delivered)
    ]
    _record_delivery(
        outbox,
        NotificationDelivery.RESULT_SUCCESS,
        success_count=len(delivered_now),
        invalid_recipients=sorted(invalid),
    )
    outbox.status = NotificationOutbox.STATUS_SUCCEEDED
    outbox.succeeded_at = timezone.now()
    outbox.available_at = outbox.succeeded_at
    outbox.lease_owner = ''
    outbox.lease_expires_at = None
    outbox.save()
    return 'succeeded'


def _finish_failure(outbox, error, now):
    """临时失败：未达上限退避重试，达到上限置死信，均释放租约。"""
    exhausted = outbox.attempts >= outbox.max_attempts
    pending_targets = [
        r for r in (outbox.recipients or [])
        if r not in set(outbox.delivered_recipients or [])
    ]
    if exhausted:
        outbox.status = NotificationOutbox.STATUS_FAILED
        outbox.available_at = now
        result = NotificationDelivery.RESULT_DEAD
    else:
        outbox.status = NotificationOutbox.STATUS_PENDING
        delay = backoff_delay_seconds(outbox.attempts)
        outbox.available_at = now + timedelta(seconds=delay)
        result = NotificationDelivery.RESULT_RETRY
    outbox.last_error = error[:5000]
    outbox.lease_owner = ''
    outbox.lease_expires_at = None
    outbox.save()
    _record_delivery(
        outbox, result, error=error,
        failed_recipients=pending_targets,
    )
    return 'dead' if exhausted else 'retry'


def process_row(outbox, sender=None, now=None):
    """处理一条已认领记录，返回 succeeded/retry/dead。"""
    now = now or timezone.now()
    sender = sender or get_sender()
    delivered = set(outbox.delivered_recipients or [])
    targets = [r for r in (outbox.recipients or []) if r not in delivered]

    with transaction.atomic():
        if not targets:
            # 收件人全部已成功（如崩溃恢复后重入）
            return _finish_success(outbox, delivered_now=[], invalid=set())
        try:
            result = sender.send(outbox.payload, targets)
        except TransientSendError as exc:
            return _finish_failure(outbox, str(exc) or 'transient failure', now)
        except Exception as exc:
            # 发送器未知异常不吞掉，按可重试失败处理直至死信
            logger.exception('通知投递发生未知异常: outbox=%s', outbox.id)
            return _finish_failure(outbox, f'unexpected error: {exc}', now)

        invalid = set(result.invalid_recipients or [])
        if not result.ok and not invalid:
            # 发送器明确报告失败却没有区分失效收件人：按可重试失败处理
            return _finish_failure(
                outbox, result.error or 'sender reported failure', now
            )
        delivered_now = [r for r in targets if r not in invalid]
        return _finish_success(outbox, delivered_now=delivered_now, invalid=invalid)


def run_delivery_batch(sender=None, owner=None, batch_size=None,
                       lease_seconds=None, now=None):
    """认领并处理一批，返回统计 {claimed, succeeded, retry, dead}。"""
    sender = sender or get_sender()
    claimed = claim_batch(
        owner=owner, batch_size=batch_size,
        lease_seconds=lease_seconds, now=now,
    )
    stats = {'claimed': len(claimed), 'succeeded': 0, 'retry': 0, 'dead': 0}
    for outbox in claimed:
        outcome = process_row(outbox, sender=sender, now=now)
        stats[outcome] = stats.get(outcome, 0) + 1
    return stats
