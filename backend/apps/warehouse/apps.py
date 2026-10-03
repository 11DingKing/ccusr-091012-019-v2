import os
import sys

from django.apps import AppConfig
from django.conf import settings


def _should_start_worker():
    """仅在真实服务进程内启动作业，管理命令 / 测试 / 迁移不启动。"""
    if not getattr(settings, 'NOTIFICATION_WORKER_AUTOSTART', True):
        return False
    if os.environ.get('NOTIFICATION_ENABLE_WORKER') == '1':
        return True
    if 'PYTEST_CURRENT_TEST' in os.environ or 'pytest' in sys.modules:
        return False
    argv0 = (sys.argv[0] or '').lower()
    argv = ' '.join(sys.argv[1:]).lower()
    # Gunicorn / uvicorn / daphne 以自身为入口；runserver 是 Django 开发服务器。
    server_binaries = ('gunicorn', 'uvicorn', 'daphne', 'hypercorn')
    if any(binary in argv0 for binary in server_binaries):
        return True
    if 'runserver' in argv:
        return True
    return False


class WarehouseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.warehouse'
    verbose_name = '库房管理'

    def ready(self):
        # 进程内通知投递作业：业务服务进程启动时分批认领投递箱。
        if not _should_start_worker():
            return
        from .notifications.runner import start_worker
        start_worker()
