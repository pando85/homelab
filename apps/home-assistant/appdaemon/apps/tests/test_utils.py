import asyncio
from unittest.mock import MagicMock, AsyncMock

import pytest
from utils import retry_with_backoff


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self):
        func = AsyncMock(return_value="success")
        result = await retry_with_backoff(func, max_retries=3)
        assert result == "success"
        assert func.call_count == 1

    @pytest.mark.asyncio
    async def test_success_second_attempt(self):
        func = AsyncMock(side_effect=[Exception("fail"), "success"])
        result = await retry_with_backoff(func, max_retries=3, initial_delay=0.1)
        assert result == "success"
        assert func.call_count == 2

    @pytest.mark.asyncio
    async def test_success_third_attempt(self):
        func = AsyncMock(side_effect=[Exception("fail1"), Exception("fail2"), "success"])
        result = await retry_with_backoff(func, max_retries=3, initial_delay=0.1)
        assert result == "success"
        assert func.call_count == 3

    @pytest.mark.asyncio
    async def test_all_attempts_fail(self):
        func = AsyncMock(side_effect=Exception("always fails"))
        with pytest.raises(Exception, match="always fails"):
            await retry_with_backoff(func, max_retries=3, initial_delay=0.1)
        assert func.call_count == 3

    @pytest.mark.asyncio
    async def test_exponential_backoff_respects_max_delay(self):
        func = AsyncMock(side_effect=[Exception("fail"), "success"])
        import time

        start = time.time()
        await retry_with_backoff(func, max_retries=3, initial_delay=0.1, max_delay=0.15)
        elapsed = time.time() - start

        assert elapsed < 0.2
        assert func.call_count == 2

    @pytest.mark.asyncio
    async def test_logger_called_on_failure(self):
        logger = MagicMock()
        logger.warning = MagicMock()
        func = AsyncMock(side_effect=[Exception("fail"), "success"])

        await retry_with_backoff(
            func, max_retries=3, initial_delay=0.1, logger=logger, operation_name="test_op"
        )

        logger.warning.assert_called_once()
        call_args = logger.warning.call_args[0][0]
        assert "test_op" in call_args
        assert "attempt 1/3" in call_args

    @pytest.mark.asyncio
    async def test_custom_max_retries(self):
        func = AsyncMock(side_effect=Exception("fail"))

        with pytest.raises(Exception):
            await retry_with_backoff(func, max_retries=5, initial_delay=0.01)

        assert func.call_count == 5
