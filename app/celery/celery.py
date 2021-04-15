import time

from celery import Celery, Task
from flask import g, request
from flask.ctx import has_app_context, has_request_context


def make_task(app):
    class NotifyTask(Task):
        abstract = True
        start = None

        def on_success(self, retval, task_id, args, kwargs):
            elapsed_time = time.monotonic() - self.start
            delivery_info = self.request.delivery_info or {}
            queue_name = delivery_info.get('routing_key', 'none')

            app.logger.info(
                "Celery task {task_name} (queue: {queue_name}) took {time}".format(
                    task_name=self.name,
                    queue_name=queue_name,
                    time="{0:.4f}".format(elapsed_time)
                )
            )

            app.statsd_client.timing(
                "celery.{queue_name}.{task_name}.success".format(
                    task_name=self.name,
                    queue_name=queue_name
                ), elapsed_time
            )

        def on_failure(self, exc, task_id, args, kwargs, einfo):
            delivery_info = self.request.delivery_info or {}
            queue_name = delivery_info.get('routing_key', 'none')

            app.logger.exception(
                "Celery task {task_name} (queue: {queue_name}) failed".format(
                    task_name=self.name,
                    queue_name=queue_name,
                )
            )

            app.statsd_client.incr(
                "celery.{queue_name}.{task_name}.failure".format(
                    task_name=self.name,
                    queue_name=queue_name
                )
            )

            super().on_failure(exc, task_id, args, kwargs, einfo)

        def __call__(self, *args, **kwargs):
            # ensure task has flask context to access config, logger, etc
            with app.app_context():
                self.start = time.monotonic()
                # Remove piggyback values from kwargs
                # Add 'request_id' to 'g' so that it gets logged
                g.request_id = kwargs.pop('request_id', None)

                return super().__call__(*args, **kwargs)

        def apply_async(self, args=None, kwargs=None, **other_kwargs):
            kwargs = kwargs or {}

            if has_request_context() and hasattr(request, 'request_id'):
                kwargs['request_id'] = request.request_id
            elif has_app_context() and 'request_id' in g:
                kwargs['request_id'] = g.request_id

            return super().apply_async(args, kwargs, **other_kwargs)

    return NotifyTask


class NotifyCelery(Celery):

    def init_app(self, app):
        super().__init__(
            app.import_name,
            broker=app.config['celery']['broker_url'],
            task_cls=make_task(app),
        )

        self.conf.update(app.config['celery'])

    def send_task(self, name, args=None, kwargs=None, **other_kwargs):
        kwargs = kwargs or {}

        if has_request_context() and hasattr(request, 'request_id'):
            kwargs['request_id'] = request.request_id
        elif has_app_context() and 'request_id' in g:
            kwargs['request_id'] = g.request_id

        return super().send_task(name, args, kwargs, **other_kwargs)
