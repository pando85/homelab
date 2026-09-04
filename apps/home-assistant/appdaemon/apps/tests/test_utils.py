import asyncio
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

import pytest
from utils import negative_price_notification, retry_with_backoff


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


class TestNegativePriceNotification:
    class FakePrice:
        def __init__(self, value, dt):
            self.value = value
            self.datetime = dt

    def test_no_negative_prices(self):
        prices = [self.FakePrice(0.1, datetime(2023, 1, 1, 10))]
        assert negative_price_notification(prices) is None

    def test_empty_prices(self):
        assert negative_price_notification([]) is None

    def test_negative_prices_listed_in_order(self):
        prices = [
            self.FakePrice(0.1, datetime(2023, 1, 1, 10)),
            self.FakePrice(-0.0106, datetime(2023, 1, 1, 13)),
            self.FakePrice(-0.0047, datetime(2023, 1, 1, 12)),
        ]
        msg = negative_price_notification(prices)
        assert msg == "Negative PVPC prices: 12-14h"

    def test_negative_prices_disjoint_ranges(self):
        prices = [
            self.FakePrice(-0.01, datetime(2023, 1, 1, 1)),
            self.FakePrice(-0.01, datetime(2023, 1, 1, 2)),
            self.FakePrice(-0.01, datetime(2023, 1, 1, 5)),
        ]
        msg = negative_price_notification(prices)
        assert msg == "Negative PVPC prices: 01-03h, 05-06h"

    def test_negative_price_last_hour_wraps(self):
        prices = [self.FakePrice(-0.01, datetime(2023, 1, 1, 23))]
        assert negative_price_notification(prices) == "Negative PVPC prices: 23-00h"
