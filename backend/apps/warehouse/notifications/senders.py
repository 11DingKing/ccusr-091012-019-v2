"""模拟通知发送器。

不连接任何真实外部服务：发送结果保存在进程内存中，验收时可直接读取。
通过脚本化故障支持退避重试、租约恢复和收件人失效场景：

- :class:`TransientSendError`：可恢复故障，投递箱会按退避策略重试；
- :class:`InvalidRecipientError`：收件人永久失效，投递箱直接转入死信，
  既不会无限重试，也不会丢失（仍可查询）。
"""
import threading
from collections import deque


class SenderError(Exception):
    """发送器故障基类。"""


class TransientSendError(SenderError):
    """临时性故障，允许退避后重试。"""


class InvalidRecipientError(SenderError):
    """收件人永久失效（空号、停用、退订等），不可重试。"""


class SimulatedSender:
    """带故障脚本与发送留痕的内存发送器。

    线程安全：进程内多个投递作业线程可并发调用。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # 已成功接收的消息：{idempotency_key: 发送次数}
        self.sent = {}
        # 有序留痕，便于验收断言
        self.sent_log = deque()
        # idempotency_key -> 剩余的临时故障次数
        self._failing = {}
        # 永久失效的收件人集合
        self.invalid_recipients = set()

    def reset(self):
        with self._lock:
            self.sent.clear()
            self.sent_log.clear()
            self._failing.clear()
            self.invalid_recipients.clear()

    def fail_times(self, idempotency_key, times):
        """令指定幂等键的前 ``times`` 次发送抛出临时故障。"""
        with self._lock:
            self._failing[idempotency_key] = times

    def mark_recipient_invalid(self, recipient):
        with self._lock:
            self.invalid_recipients.add(recipient)

    def send(self, outbox):
        """模拟一次发送。成功返回 None，否则抛出对应故障。

        已成功投递过的幂等键直接幂等返回（模拟下游通道的去重能力），
        租约恢复导致的重复调用不会产生第二条通知。
        """
        recipient = outbox.recipient
        with self._lock:
            # 幂等短路：下游已接收过该键，重复投递直接视为成功且不留新痕。
            if outbox.idempotency_key in self.sent:
                return
            if recipient in self.invalid_recipients:
                raise InvalidRecipientError(f"收件人已失效: {recipient}")
            remaining = self._failing.get(outbox.idempotency_key, 0)
            if remaining > 0:
                self._failing[outbox.idempotency_key] = remaining - 1
                error = TransientSendError(
                    f"模拟临时故障: {outbox.idempotency_key}，剩余 {remaining - 1} 次"
                )
            else:
                error = None

        # 故障在锁外抛出，模拟真实 IO 调用，避免持锁重入。
        if error is not None:
            raise error

        with self._lock:
            self.sent[outbox.idempotency_key] = self.sent.get(outbox.idempotency_key, 0) + 1
            self.sent_log.append({
                'idempotency_key': outbox.idempotency_key,
                'event_type': outbox.event_type,
                'recipient': recipient,
                'channel': outbox.channel,
                'payload': dict(outbox.payload),
            })

    def send_count(self, idempotency_key):
        with self._lock:
            return self.sent.get(idempotency_key, 0)


# 进程内默认发送器单例，作业与验收共用。
simulated_sender = SimulatedSender()
