from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import pytz
from climate_control import ClimateControl, Price, is_float


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture()
def climate_control():
    with patch.object(ClimateControl, "__init__", lambda self: None):
        climate_control = ClimateControl()
        def debug_log(msg, level=None):
            if level == "DEBUG" or level is None:
                print(f"DEBUG: {msg}")
        climate_control.log = debug_log
        climate_control.args = {
            "sensor": {"pvpc_price": "sensor.esios_pvpc"},
            "input_boolean": {"enable": "input_boolean.appdaemon_climate_enable"},
            "input_number": {"min_hours_per_day": "input_number.appdaemon_climate_min_hours_per_day"},
            "input_select_heat_mode": "input_select.appdaemon_climate_heat_mode",
            "notify": {"enabled": True, "target": "test"},
            "climate": {
                "enabled": True,
                "entity": "input_select.heishamon_heatmode",
                "off_mode": "DHW",
            },
            "fallback_cheap_electricity_price": 0.09,
        }
        climate_control.get_timezone = MagicMock(return_value=pytz.timezone("Europe/Madrid"))
        climate_control.timers = []
        return climate_control


class TestIsFloat:
    def test_valid_float_string(self):
        assert is_float("3.14") is True

    def test_valid_integer_string(self):
        assert is_float("42") is True

    def test_negative_float(self):
        assert is_float("-2.5") is True

    def test_invalid_string(self):
        assert is_float("not a number") is False

    def test_empty_string(self):
        assert is_float("") is False

    def test_none_value(self):
        with pytest.raises(TypeError):
            is_float(None)


class TestPrice:
    def test_valid_price(self):
        price = Price(value=0.15, datetime=datetime(2023, 1, 1, 12))
        assert price.value == 0.15
        assert price.datetime == datetime(2023, 1, 1, 12)

    def test_zero_price(self):
        price = Price(value=0.0, datetime=datetime(2023, 1, 1, 12))
        assert price.value == 0.0

    def test_negative_price_raises(self):
        with pytest.raises(ValueError):
            Price(value=-0.1, datetime=datetime(2023, 1, 1, 12))

    def test_negative_price_small_value(self):
        with pytest.raises(ValueError):
            Price(value=-0.0001, datetime=datetime(2023, 1, 1, 12))


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


class TestCheaperHours:
    def test_cheaper_hours_basic(self, climate_control):
        prices = [
            Price(value=0.1, datetime=datetime(2023, 1, 1, 0)),
            Price(value=0.5, datetime=datetime(2023, 1, 1, 1)),
            Price(value=0.2, datetime=datetime(2023, 1, 1, 2)),
            Price(value=0.8, datetime=datetime(2023, 1, 1, 3)),
        ]
        result = climate_control._cheaper_hours(prices, 2)
        assert len(result) == 2
        assert result[0].value == 0.1
        assert result[1].value == 0.2

    def test_cheaper_hours_all(self, climate_control):
        prices = [Price(value=i * 0.1, datetime=datetime(2023, 1, 1, i)) for i in range(24)]
        result = climate_control._cheaper_hours(prices, 24)
        assert len(result) == 24

    def test_cheaper_hours_one(self, climate_control):
        prices = [
            Price(value=0.5, datetime=datetime(2023, 1, 1, 0)),
            Price(value=0.1, datetime=datetime(2023, 1, 1, 1)),
        ]
        result = climate_control._cheaper_hours(prices, 1)
        assert len(result) == 1
        assert result[0].value == 0.1

    def test_cheaper_hours_empty(self, climate_control):
        prices = []
        result = climate_control._cheaper_hours(prices, 1)
        assert len(result) == 0


class TestGenerateVegaDiagram:
    def test_generate_vega_diagram_basic(self, climate_control):
        datetimes = [datetime(2023, 1, 1, h) for h in [8, 12, 18]]
        result = climate_control._generate_vega_diagram(datetimes)
        
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_vega_diagram_empty(self, climate_control):
        result = climate_control._generate_vega_diagram([])
        
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_vega_diagram_all_hours(self, climate_control):
        datetimes = [datetime(2023, 1, 1, h) for h in range(24)]
        result = climate_control._generate_vega_diagram(datetimes)
        
        assert isinstance(result, str)


class TestChangeHvacMode:
    @pytest.mark.asyncio
    async def test_change_hvac_mode_enabled(self, climate_control):
        climate_control.args["climate"]["enabled"] = True
        climate_control.get_state = AsyncMock(return_value="Heat")
        climate_control.set_state = AsyncMock()
        climate_control.sleep = AsyncMock()
        climate_control.notify = AsyncMock()
        
        await climate_control._change_hvac_mode("Heat")
        
        climate_control.set_state.assert_called()

    @pytest.mark.asyncio
    async def test_change_hvac_mode_dry_run(self, climate_control):
        climate_control.args["climate"]["enabled"] = False
        climate_control.notify = AsyncMock()
        
        await climate_control._change_hvac_mode("Heat")
        
        climate_control.notify.assert_called()

    @pytest.mark.asyncio
    async def test_change_hvac_mode_notify_disabled(self, climate_control):
        climate_control.args["notify"]["enabled"] = False
        climate_control.args["climate"]["enabled"] = True
        climate_control.get_state = AsyncMock(return_value="Heat")
        climate_control.set_state = AsyncMock()
        climate_control.sleep = AsyncMock()
        
        await climate_control._change_hvac_mode("Heat")
        
        climate_control.set_state.assert_called()


class TestStartHvac:
    @pytest.mark.asyncio
    async def test_start_hvac(self, climate_control):
        climate_control.get_state = AsyncMock(return_value="Heat+DHW")
        climate_control._change_hvac_mode = AsyncMock()
        
        await climate_control._start_hvac()
        
        climate_control._change_hvac_mode.assert_called_with("Heat+DHW")


class TestStopHvac:
    @pytest.mark.asyncio
    async def test_stop_hvac(self, climate_control):
        climate_control._change_hvac_mode = AsyncMock()
        
        await climate_control._stop_hvac()
        
        climate_control._change_hvac_mode.assert_called_with(climate_control.args["climate"]["off_mode"])


class TestDailyRegisterSchedulers:
    @pytest.mark.asyncio
    async def test_daily_register_schedulers_disabled(self, climate_control):
        climate_control.get_state = AsyncMock(return_value="off")
        climate_control._register_schedulers = AsyncMock()
        
        await climate_control._daily_register_schedulers()
        
        climate_control._register_schedulers.assert_not_called()

    @pytest.mark.asyncio
    async def test_daily_register_schedulers_enabled(self, climate_control):
        climate_control.get_state = AsyncMock(return_value="on")
        climate_control._register_schedulers = AsyncMock()
        
        await climate_control._daily_register_schedulers()
        
        climate_control._register_schedulers.assert_called_once()

    @pytest.mark.asyncio
    async def test_daily_register_schedulers_exception_retry(self, climate_control):
        climate_control.get_state = AsyncMock(return_value="on")
        climate_control._register_schedulers = AsyncMock(side_effect=[Exception("Test error"), None])
        climate_control.notify = AsyncMock()
        
        with patch('climate_control.asyncio.sleep', new_callable=AsyncMock):
            await climate_control._daily_register_schedulers()
        
        assert climate_control._register_schedulers.call_count == 2


class TestUnregisterSchedulers:
    @pytest.mark.asyncio
    async def test_unregister_schedulers_empty(self, climate_control):
        with patch.object(type(climate_control), 'name', new_callable=PropertyMock, return_value="climate_control"):
            climate_control.AD = MagicMock()
            climate_control.AD.sched = MagicMock()
            climate_control.AD.sched.schedule = {}
            climate_control.cancel_timer = AsyncMock()
            
            await climate_control._unregister_schedulers()
            
            climate_control.cancel_timer.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregister_schedulers_with_timers(self, climate_control):
        with patch.object(type(climate_control), 'name', new_callable=PropertyMock, return_value="climate_control"):
            climate_control.AD = MagicMock()
            climate_control.AD.sched = MagicMock()
            climate_control.AD.sched.schedule = {
                "climate_control": {
                    "timer1": {"callback": MagicMock()},
                    "timer2": {"callback": MagicMock()},
                }
            }
            climate_control.cancel_timer = AsyncMock()
            
            await climate_control._unregister_schedulers()
            
            assert climate_control.cancel_timer.call_count == 2


class TestTerminate:
    @pytest.mark.asyncio
    async def test_terminate(self, climate_control):
        climate_control._unregister_schedulers = AsyncMock()
        
        await climate_control.terminate()
        
        climate_control._unregister_schedulers.assert_called_once()


class TestResilience:
    @pytest.mark.asyncio
    async def test_get_prices_non_numeric_values(self, climate_control):
        pvpc_sensor_data = {
            "attributes": {
                "price_00h": 0.1,
                "price_01h": "N/A",
                "price_02h": 0.3,
                "price_04h": 0.5,
            }
        }
        climate_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        prices = await climate_control._get_prices()
        assert len(prices) == 3
        values = [p.value for p in prices]
        assert 0.1 in values
        assert 0.3 in values
        assert 0.5 in values

    @pytest.mark.asyncio
    async def test_get_prices_none_value_raises(self, climate_control):
        pvpc_sensor_data = {
            "attributes": {
                "price_00h": 0.1,
                "price_01h": None,
            }
        }
        climate_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        with pytest.raises(TypeError):
            await climate_control._get_prices()

    @pytest.mark.asyncio
    async def test_get_prices_empty_attributes(self, climate_control):
        pvpc_sensor_data = {"attributes": {}}
        climate_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        prices = await climate_control._get_prices()
        assert len(prices) == 0

    @pytest.mark.asyncio
    async def test_change_hvac_mode_wait_for_state(self, climate_control):
        climate_control.args["climate"]["enabled"] = True
        climate_control.get_state = AsyncMock(side_effect=["DHW", "DHW", "Heat"])
        climate_control.set_state = AsyncMock()
        climate_control.sleep = AsyncMock()
        climate_control.notify = AsyncMock()
        
        await climate_control._change_hvac_mode("Heat")
        
        assert climate_control.get_state.call_count == 3
        assert climate_control.sleep.call_count == 2

    @pytest.mark.asyncio
    async def test_notify_exception_does_not_break_daily_scheduler(self, climate_control):
        climate_control.get_state = AsyncMock(return_value="on")
        climate_control._register_schedulers = AsyncMock()
        climate_control.notify = AsyncMock(side_effect=Exception("Notify failed"))
        
        await climate_control._daily_register_schedulers()
        
        climate_control._register_schedulers.assert_called_once()

    @pytest.mark.asyncio
    async def test_group_for_scheduling_empty(self, climate_control):
        result = climate_control._group_for_scheduling([])
        assert result == []

    @pytest.mark.asyncio
    async def test_group_for_scheduling_single(self, climate_control):
        result = climate_control._group_for_scheduling([datetime(2023, 1, 1, 5)])
        assert result == [[datetime(2023, 1, 1, 5)]]

    @pytest.mark.asyncio
    async def test_group_for_scheduling_full_day(self, climate_control):
        datetimes = [datetime(2023, 1, 1, h) for h in range(24)]
        result = climate_control._group_for_scheduling(datetimes)
        assert len(result) == 1
        assert len(result[0]) == 24
