import logging
import threading
try:
    import queue
except ImportError:
    import Queue as queue


logger = logging.getLogger(__name__)


class ThreadPool(object):
    class Worker(threading.Thread):

        def __init__(self, tasks):
            threading.Thread.__init__(self)
            self.tasks = tasks
            self.daemon = True
            self.start()

        def run(self):
            while True:
                func, result_handler, args, kwargs = self.tasks.get()
                try:
                    result_handler(func(*args, **kwargs))
                except Exception as e:
                    logger.error(e)
                finally:
                    self.tasks.task_done()

    def __init__(self, num_workers):
        self.call_queue = queue.Queue()

        self.workers = [ThreadPool.Worker(self.call_queue)
                        for _ in range(num_workers)]

    def put(self, func, args, kwargs, result_handler=lambda x: x):
        self.call_queue.put((func, result_handler, args, kwargs))

    def map(self, func, args_list, result_handler=lambda x: x):
        for args, kwargs in args_list:
            self.put(func, args, kwargs, result_handler)

    def wait(self):
        self.call_queue.join()
