import os
import sys

from django.apps import AppConfig


class WarehouseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.warehouse'
    verbose_name = '库房管理'

    def ready(self):
        # 默认不启动；通过环境变量开启进程内后台投递线程
        if os.environ.get('NOTIFICATION_WORKER_ENABLED', '').lower() != 'true':
            return
        if os.environ.get('NOTIFICATION_WORKER_NO_AUTOSTART'):
            return
        # runserver 的自动重载器：仅在真正服务的子进程（RUN_MAIN）中启动，避免重复
        if 'runserver' in sys.argv and 'RUN_MAIN' not in os.environ:
            return
        from .notifications.worker import start_background_worker
        start_background_worker()
