from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from appdaemon import logging
from climate_control import ClimateControl, Price


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture()
def climate_control():
    with patch.object(ClimateControl, "__init__", lambda self: None):
        climate_control = ClimateControl()

        climate_control._logging = logging.Logging(None, "DEBUG")
        climate_control.logger = climate_control._logging.get_logger()
        climate_control.args = MagicMock(return_value={"sensor": {"pvpc_price": "sensor.esios_pvpc"}})
        climate_control.get_timezone = MagicMock(return_value="Europe/Madrid")
        return climate_control


@pytest.mark.asyncio
async def test_get_prices(climate_control):
    pvpc_sensor_data = {
        "attributes": {
            "price_00h": 0.1,
            "price_01h": 0.2,
            "price_02h": 0.3,
            "price_03h": 0.4,
            "price_04h": 0.5,
            "price_05h": 0.6,
            "price_06h": 0.7,
            "price_07h": 0.8,
            "price_08h": 0.9,
            "price_09h": 1.0,
            "price_10h": 1.1,
            "price_11h": 1.2,
            "price_12h": 1.3,
            "price_13h": 1.4,
            "price_14h": 1.5,
            "price_15h": 1.6,
            "price_16h": 1.7,
            "price_17h": 1.8,
            "price_18h": 1.9,
            "price_19h": 2.0,
            "price_20h": 2.1,
            "price_21h": 2.2,
            "price_22h": 2.3,
            "price_23h": 2.4,
        }
    }
    climate_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

    prices = await climate_control._get_prices()
    assert len(prices) == 24
    for price in prices:
        assert isinstance(price, Price)
        assert price.value > 0
        now = datetime.now()

        assert price.datetime >= datetime(now.year, now.month, now.day) and price.datetime <= datetime(
            now.year, now.month, now.day
        ) + timedelta(days=1)


@pytest.mark.asyncio
async def test_get_prices_change_hour(climate_control):
    pvpc_sensor_data = {
        "attributes": {
            "price_00h": 0.1,
            "price_01h": 0.2,
            "price_03h": 0.4,
            "price_04h": 0.5,
            "price_05h": 0.6,
            "price_06h": 0.7,
            "price_07h": 0.8,
            "price_08h": 0.9,
            "price_09h": 1.0,
            "price_10h": 1.1,
            "price_11h": 1.2,
            "price_12h": 1.3,
            "price_13h": 1.4,
            "price_14h": 1.5,
            "price_15h": 1.6,
            "price_16h": 1.7,
            "price_17h": 1.8,
            "price_18h": 1.9,
            "price_19h": 2.0,
            "price_20h": 2.1,
            "price_21h": 2.2,
            "price_22h": 2.3,
            "price_23h": 2.4,
        }
    }
    climate_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

    prices = await climate_control._get_prices()
    assert len(prices) == 23
    for price in prices:
        assert isinstance(price, Price)
        assert price.value > 0
        now = datetime.now()

        assert price.datetime >= datetime(now.year, now.month, now.day) and price.datetime <= datetime(
            now.year, now.month, now.day
        ) + timedelta(days=1)


@pytest.mark.asyncio
async def test_get_prices_negative_values(climate_control):
    pvpc_sensor_data = {
        "attributes": {
            "price_00h": 0.1,
            "price_01h": -0.2,
            "price_02h": 0.3,
            "price_03h": 0.4,
            "price_04h": 0.5,
            "price_05h": 0.6,
            "price_06h": 0.7,
            "price_07h": 0.8,
            "price_08h": 0.9,
            "price_09h": 1.0,
            "price_10h": 1.1,
            "price_11h": 1.2,
            "price_12h": 1.3,
            "price_13h": 1.4,
            "price_14h": 1.5,
            "price_15h": 1.6,
            "price_16h": 1.7,
            "price_17h": 1.8,
            "price_18h": 1.9,
            "price_19h": 2.0,
            "price_20h": 2.1,
            "price_21h": 2.2,
            "price_22h": 2.3,
            "price_23h": 2.4,
        }
    }
    climate_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

    with pytest.raises(ValueError):
        await climate_control._get_prices()


@pytest.mark.parametrize(
    "date_list, expected_result",
    [
        (
            [
                datetime(2023, 1, 1, 0, 0),
                datetime(2023, 1, 1, 2, 0),
                datetime(2023, 1, 1, 1, 0),
                datetime(2023, 1, 1, 4, 0),
                datetime(2023, 1, 1, 5, 0),
                datetime(2023, 1, 1, 6, 0),
            ],
            [
                [datetime(2023, 1, 1, 0, 0), datetime(2023, 1, 1, 1, 0), datetime(2023, 1, 1, 2, 0)],
                [datetime(2023, 1, 1, 4, 0), datetime(2023, 1, 1, 5, 0), datetime(2023, 1, 1, 6, 0)],
            ],
        ),
        (
            [
                datetime(2023, 1, 1, 0, 0),
                datetime(2023, 1, 1, 1, 0),
                datetime(2023, 1, 1, 2, 0),
                datetime(2023, 1, 1, 3, 0),
                datetime(2023, 1, 1, 6, 0),
            ],
            [
                [
                    datetime(2023, 1, 1, 0, 0),
                    datetime(2023, 1, 1, 1, 0),
                    datetime(2023, 1, 1, 2, 0),
                    datetime(2023, 1, 1, 3, 0),
                ],
                [datetime(2023, 1, 1, 6, 0)],
            ],
        ),
        (
            [
                datetime(2023, 1, 1, 10, 0),
                datetime(2023, 1, 1, 3, 0),
                datetime(2023, 1, 1, 21, 0),
                datetime(2023, 1, 1, 6, 0),
                datetime(2023, 1, 1, 20, 0),
            ],
            [
                [
                    datetime(2023, 1, 1, 3, 0),
                ],
                [
                    datetime(2023, 1, 1, 6, 0),
                ],
                [
                    datetime(2023, 1, 1, 10, 0),
                ],
                [
                    datetime(2023, 1, 1, 20, 0),
                    datetime(2023, 1, 1, 21, 0),
                ],
            ],
        ),
    ],
)
def test_group_for_scheduling(climate_control, date_list, expected_result):
    result = climate_control._group_for_scheduling(date_list)
    assert result == expected_result
