"""日志安全与并发上下文的纯标准库验证。"""
import asyncio
import io
import logging
import unittest
from app.logging_config import RequestContextFilter, log_exception, request_id


class LoggingHelperTests(unittest.TestCase):
    def test_exception_drops_original_message_traceback_source_and_locals(self):
        logger = logging.getLogger("app.safe_exception_test")
        with self.assertLogs(logger, "ERROR") as logs:
            try:
                raise ValueError("PRIVATE_Q PRIVATE_A FAKE_SECRET_KEY")
            except ValueError as exc:
                log_exception(logger, exc)
        text = "".join(logs.output)
        self.assertIn("ValueError", text)
        self.assertIn("test_logging_helpers.py:", text)
        self.assertNotIn("PRIVATE", text)
        self.assertNotIn("FAKE_SECRET_KEY", text)
        self.assertIsNone(logs.records[0].exc_info[2])

    def test_contextvars_stay_isolated_in_concurrent_tasks_and_threads(self):
        async def work(value):
            token = request_id.set(value)
            try:
                await asyncio.sleep(0)
                record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "message", (), None)
                RequestContextFilter().filter(record)
                thread_value = await asyncio.to_thread(request_id.get)
                return record.request_id, thread_value
            finally:
                request_id.reset(token)
        async def run():
            return await asyncio.gather(*(work(str(i)) for i in range(10)))
        self.assertEqual(asyncio.run(run()), [(str(i), str(i)) for i in range(10)])
        self.assertEqual(request_id.get(), "-")

    def test_formatter_includes_request_id_and_no_request_body(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.addFilter(RequestContextFilter())
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s request_id=%(request_id)s %(message)s"))
        logger = logging.getLogger("app.formatter_test")
        old_level, old_propagate = logger.level, logger.propagate
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.addHandler(handler)
        token = request_id.set("request-42")
        try:
            logger.info("request_done method=POST status=200")
        finally:
            request_id.reset(token)
            logger.removeHandler(handler)
            logger.setLevel(old_level)
            logger.propagate = old_propagate
        self.assertIn("INFO app.formatter_test request_id=request-42 request_done", output.getvalue())
