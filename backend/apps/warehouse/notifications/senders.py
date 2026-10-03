"""
通知发送器：不连接真实外部服务，全部为可预测的模拟实现。

统一接口 send(payload, recipients) -> SendResult：
- 成功返回 SendResult；
- 临时失败抛 TransientSendError，投递作业据此安排退避重试；
- 失效收件人通过 SendResult.invalid_recipients 返回，作业据此永久剔除，
  避免对空号/已注销收件人无限重试。
"""
import importlib
import threading
from dataclasses import dataclass, field

from django.conf import settings


class TransientSendError(Exception):
    """可重试的临时发送失败（对端超时、网络抖动等）。"""


@dataclass
class SendResult:
    ok: bool
    invalid_recipients: list = field(default_factory=list)
    error: str = ""


class BaseSender:
    """发送器基类，真实实现接入外部服务时实现 send 即可。"""

    def send(self, payload, recipients):
        raise NotImplementedError


class MockSender(BaseSender):
    """
    模拟发送器：默认对全部有效收件人发送成功，并把每次发送记录到类内存，
    供验收断言“确实发出过且只发了一次”。

    通过类方法制造确定性故障：
    - fail_times(n, message)：接下来 n 次整体临时失败（模拟超时/网络抖动）；
    - fail_once(key, message)：处理指定 dedup_key 时临时失败一次；
    - mark_invalid(recipients)：把收件人标记为永久失效（如空号/已注销）。
    """

    _lock = threading.Lock()
    sent: list = []
    _global_fail_times = 0
    _global_error = "mock transient failure"
    _per_key_failures: dict = {}
    _invalid: frozenset = frozenset()

    @classmethod
    def reset(cls):
        with cls._lock:
            cls.sent = []
            cls._global_fail_times = 0
            cls._global_error = "mock transient failure"
            cls._per_key_failures = {}
            cls._invalid = frozenset()

    @classmethod
    def fail_times(cls, times, message="mock transient failure"):
        with cls._lock:
            cls._global_fail_times = times
            cls._global_error = message

    @classmethod
    def fail_once(cls, key, message="mock transient failure"):
        with cls._lock:
            cls._per_key_failures[key] = cls._per_key_failures.get(key, 0) + 1
            cls._global_error = message

    @classmethod
    def mark_invalid(cls, recipients):
        with cls._lock:
            cls._invalid = frozenset(set(cls._invalid) | set(recipients))

    def send(self, payload, recipients):
        key = payload.get("dedup_key", "")
        invalid = sorted(set(recipients) & set(MockSender._invalid))

        with MockSender._lock:
            transient = False
            if MockSender._global_fail_times > 0:
                MockSender._global_fail_times -= 1
                transient = True
            elif MockSender._per_key_failures.get(key, 0) > 0:
                MockSender._per_key_failures[key] -= 1
                transient = True
            error = MockSender._global_error
            if transient:
                # 临失败不产生发送记录，模拟“调用方超时、未知是否送达”
                raise TransientSendError(error)
            valid = [r for r in recipients if r not in invalid]
            MockSender.sent.append({"dedup_key": key, "recipients": valid})

        return SendResult(ok=True, invalid_recipients=invalid)


def get_sender():
    """按 settings.NOTIFICATION_SENDER 惰性实例化发送器。"""
    cls_path = getattr(settings, "NOTIFICATION_SENDER", "") or (
        "apps.warehouse.notifications.senders.MockSender"
    )
    module_path, cls_name = cls_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, cls_name)()
