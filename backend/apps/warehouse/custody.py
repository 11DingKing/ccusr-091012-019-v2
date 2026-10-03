"""监管状态变更服务：业务变更与通知投递箱在同一事务内提交。"""
import logging

from django.db import transaction

from .models import CustodyStatus, Goods
from .notifications.dispatcher import enqueue_notification

logger = logging.getLogger('apps')

ACTION_LABELS = dict(CustodyStatus.ACTION_CHOICES)


def custody_event_key(goods_id, action, event_id):
    return f"custody:{goods_id}:{action}:{event_id}"


def change_custody_status(goods, action, *, operator=None, reason='',
                          recipient=None, event_id=None):
    """执行冻结 / 驳回 / 放行，并在同一事务内写入投递箱。

    返回 ``(status_entry, outbox, created)``：

    - 业务记录与投递箱记录要么同时提交，要么同时回滚，调用方超时也不会丢通知；
    - ``event_id`` 相同的重复事件被投递箱唯一约束幂等挡下（``created=False``、
      ``status_entry=None``），不产生重复业务记录或重复通知；
      缺省时以本次变更生成唯一键。
    """
    if action not in dict(CustodyStatus.ACTION_CHOICES):
        raise ValueError(f"未知的监管状态动作: {action}")
    if not isinstance(goods, Goods):
        raise ValueError("goods 必须为 Goods 实例")

    recipient = recipient or _default_recipient(operator)

    with transaction.atomic():
        # 先落业务记录，自动主键可作为缺省幂等键；与投递箱同事务提交。
        entry = CustodyStatus.objects.create(
            goods=goods,
            action=action,
            reason=reason,
            operator=operator,
        )
        key = custody_event_key(goods.id, action, event_id or entry.id)
        payload = {
            'goods_id': goods.id,
            'goods_name': goods.name,
            'goods_code': goods.code,
            'action': action,
            'action_label': ACTION_LABELS.get(action, action),
            'reason': reason,
            'operator': getattr(operator, 'username', None),
            'status_id': entry.id,
        }
        outbox, created = enqueue_notification(
            event_type=f"custody.{action}",
            aggregate_ref=f"goods:{goods.id}",
            recipient=recipient,
            payload=payload,
            idempotency_key=key,
        )
        if not created:
            # 重复事件：撤销刚产生的业务记录，保持“一次事件一条记录一条通知”。
            entry.delete()
            entry = None
            logger.info("重复监管状态事件已忽略: key=%s", key)
    return entry, outbox, created


def _default_recipient(operator):
    if operator is None:
        return ''
    return operator.phone or operator.username
