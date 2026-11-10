"""Public stack-copying operations with sanitizer fiber notifications."""
import gc
import threading
import unittest

from greenlet import greenlet, getcurrent


class SanitizerFiberTests(unittest.TestCase):

    def test_builtin_callback_resumes_its_c_stack(self):
        parent = getcurrent()

        class Size:
            def __index__(self):
                value = parent.switch('size')
                return value

        for unused in range(100):
            worker = greenlet(lambda: [None] * Size())
            self.assertEqual(worker.switch(), 'size')
            self.assertEqual(worker.switch(3), [None, None, None])
            self.assertTrue(worker.dead)

    def test_suspended_and_finished_stacks_coexist(self):
        parent = getcurrent()
        workers = [greenlet(lambda: parent.switch('waiting')) for unused in range(20)]
        for worker in workers:
            self.assertEqual(worker.switch(), 'waiting')
        for value, worker in enumerate(reversed(workers)):
            self.assertEqual(worker.switch(value), value)
            self.assertTrue(worker.dead)
            gc.collect()

    def test_exception_returns_to_the_original_parent(self):
        parent = getcurrent()

        def work():
            try:
                parent.switch('waiting')
            except ValueError:
                return 'caught'

        for unused in range(100):
            worker = greenlet(work)
            self.assertEqual(worker.switch(), 'waiting')
            self.assertEqual(worker.throw(ValueError('ordinary cancellation')), 'caught')
            self.assertTrue(worker.dead)

    def test_native_threads_keep_independent_switch_state(self):
        barrier = threading.Barrier(2)
        results, errors = [], []

        def thread_work():
            try:
                parent = getcurrent()
                barrier.wait(timeout=5)
                for value in range(100):
                    worker = greenlet(lambda: parent.switch('waiting'))
                    self.assertEqual(worker.switch(), 'waiting')
                    self.assertEqual(worker.switch(value), value)
                    self.assertTrue(worker.dead)
                results.append(threading.get_native_id())
            except BaseException as error:
                errors.append(error)

        threads = [threading.Thread(target=thread_work) for unused in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 2)


if __name__ == '__main__':
    unittest.main()
