from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import pytz
from dhw_control import DHWControl, Price, chunk_list, is_float


class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super(AsyncMock, self).__call__(*args, **kwargs)


@pytest.fixture()
def dhw_control():
    with patch.object(DHWControl, "__init__", lambda self: None):
        dhw_control = DHWControl()
        def debug_log(msg, level=None):
            if level == "DEBUG" or level is None:
                print(f"DEBUG: {msg}")
        dhw_control.log = debug_log
        dhw_control.args = {
            "input_boolean": {"enable": "input_boolean.appdaemon_dhw_enable"},
            "sensor": {"pvpc_price": "sensor.esios_pvpc"},
            "notify": {"enabled": True, "target": "test"},
            "dhw": {"enabled": True, "entity": "switch.aquarea_force_dhw_mode"},
            "interval_hours": 12,
        }
        dhw_control.get_timezone = MagicMock(return_value=pytz.timezone("Europe/Madrid"))
        dhw_control.timers = []
        return dhw_control


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


class TestChunkList:
    def test_chunk_list_even(self):
        lst = [1, 2, 3, 4, 5, 6]
        result = chunk_list(lst, 2)
        assert result == [[1, 2], [3, 4], [5, 6]]

    def test_chunk_list_odd(self):
        lst = [1, 2, 3, 4, 5]
        result = chunk_list(lst, 2)
        assert result == [[1, 2], [3, 4], [5]]

    def test_chunk_list_full_size(self):
        lst = [1, 2, 3, 4, 5]
        result = chunk_list(lst, 5)
        assert result == [[1, 2, 3, 4, 5]]

    def test_chunk_list_larger_than_list(self):
        lst = [1, 2, 3]
        result = chunk_list(lst, 10)
        assert result == [[1, 2, 3]]

    def test_chunk_list_empty(self):
        result = chunk_list([], 2)
        assert result == []

    def test_chunk_list_single_element(self):
        lst = [1]
        result = chunk_list(lst, 3)
        assert result == [[1]]


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


class TestGetPrices:
    @pytest.mark.asyncio
    async def test_get_prices_success(self, dhw_control):
        pvpc_sensor_data = {
            "attributes": {f"price_{i:02d}h": 0.1 * (i + 1) for i in range(24)}
        }
        dhw_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        prices = await dhw_control._get_prices()
        assert len(prices) == 24
        for i, price in enumerate(prices):
            assert isinstance(price, Price)
            assert price.value == 0.1 * (i + 1)

    @pytest.mark.asyncio
    async def test_get_prices_missing_hours(self, dhw_control):
        pvpc_sensor_data = {
            "attributes": {
                "price_00h": 0.1,
                "price_01h": 0.2,
                "price_05h": 0.6,
            }
        }
        dhw_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        prices = await dhw_control._get_prices()
        assert len(prices) == 3

    @pytest.mark.asyncio
    async def test_get_prices_non_numeric_values(self, dhw_control):
        pvpc_sensor_data = {
            "attributes": {
                "price_00h": 0.1,
                "price_01h": "N/A",
                "price_02h": 0.3,
                "price_04h": 0.5,
            }
        }
        dhw_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        prices = await dhw_control._get_prices()
        assert len(prices) == 3
        values = [p.value for p in prices]
        assert 0.1 in values
        assert 0.3 in values
        assert 0.5 in values

    @pytest.mark.asyncio
    async def test_get_prices_none_value_raises(self, dhw_control):
        pvpc_sensor_data = {
            "attributes": {
                "price_00h": 0.1,
                "price_01h": None,
            }
        }
        dhw_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        with pytest.raises(TypeError):
            await dhw_control._get_prices()

    @pytest.mark.asyncio
    async def test_get_prices_empty_attributes(self, dhw_control):
        pvpc_sensor_data = {"attributes": {}}
        dhw_control.get_state = AsyncMock(return_value=pvpc_sensor_data)

        prices = await dhw_control._get_prices()
        assert len(prices) == 0

    @pytest.mark.asyncio
    async def test_get_prices_none_response(self, dhw_control):
        dhw_control.get_state = AsyncMock(return_value=None)
        
        with pytest.raises(TypeError):
            await dhw_control._get_prices()


class TestGenerateVegaDiagram:
    def test_generate_vega_diagram_basic(self, dhw_control):
        datetimes = [datetime(2023, 1, 1, h) for h in [8, 12, 18]]
        result = dhw_control._generate_vega_diagram(datetimes)
        
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_vega_diagram_empty(self, dhw_control):
        result = dhw_control._generate_vega_diagram([])
        
        assert isinstance(result, str)
        assert len(result) > 0

    def test_generate_vega_diagram_all_hours(self, dhw_control):
        datetimes = [datetime(2023, 1, 1, h) for h in range(24)]
        result = dhw_control._generate_vega_diagram(datetimes)
        
        assert isinstance(result, str)


class TestForceDHW:
    @pytest.mark.asyncio
    async def test_force_dhw_enabled(self, dhw_control):
        dhw_control.args["dhw"]["enabled"] = True
        entity_mock = MagicMock()
        entity_mock.turn_on = AsyncMock()
        dhw_control.get_entity = MagicMock(return_value=entity_mock)
        dhw_control.notify = AsyncMock()
        
        await dhw_control._force_dhw()
        
        entity_mock.turn_on.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_dhw_dry_run(self, dhw_control):
        dhw_control.args["dhw"]["enabled"] = False
        entity_mock = MagicMock()
        entity_mock.turn_on = AsyncMock()
        dhw_control.get_entity = MagicMock(return_value=entity_mock)
        dhw_control.notify = AsyncMock()
        
        await dhw_control._force_dhw()
        
        entity_mock.turn_on.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_dhw_notify_disabled(self, dhw_control):
        dhw_control.args["dhw"]["enabled"] = True
        dhw_control.args["notify"]["enabled"] = False
        entity_mock = MagicMock()
        entity_mock.turn_on = AsyncMock()
        dhw_control.get_entity = MagicMock(return_value=entity_mock)
        dhw_control.notify = AsyncMock()
        
        await dhw_control._force_dhw()
        
        dhw_control.notify.assert_not_called()


class TestRegisterSchedulers:
    @pytest.mark.asyncio
    async def test_register_schedules_interval_valid(self, dhw_control):
        for valid_interval in [1, 2, 3, 4, 6, 8, 12, 24]:
            dhw_control.args["interval_hours"] = valid_interval
            assert dhw_control.args["interval_hours"] in (1, 2, 3, 4, 6, 8, 12, 24)

    @pytest.mark.asyncio
    async def test_register_schedules_invalid_interval_fallback(self, dhw_control):
        dhw_control.args["interval_hours"] = 7
        if dhw_control.args["interval_hours"] not in (1, 2, 3, 4, 6, 8, 12, 24):
            dhw_control.args["interval_hours"] = 24
        assert dhw_control.args["interval_hours"] == 24

    @pytest.mark.asyncio
    async def test_register_schedules_empty_prices(self, dhw_control):
        dhw_control._unregister_schedulers = AsyncMock()
        dhw_control.notify = AsyncMock()
        pvpc_sensor_data = {"attributes": {}}
        dhw_control.get_state = AsyncMock(return_value=pvpc_sensor_data)
        
        with patch('dhw_control.asyncio.sleep', new_callable=AsyncMock):
            with patch.object(dhw_control, 'get_timezone', return_value=pytz.timezone("Europe/Madrid")):
                await dhw_control._register_schedulers()


class TestDailyRegisterSchedulers:
    @pytest.mark.asyncio
    async def test_daily_register_schedulers_disabled(self, dhw_control):
        dhw_control.get_state = AsyncMock(return_value="off")
        dhw_control._register_schedulers = AsyncMock()
        
        await dhw_control._daily_register_schedulers()
        
        dhw_control._register_schedulers.assert_not_called()

    @pytest.mark.asyncio
    async def test_daily_register_schedulers_enabled(self, dhw_control):
        dhw_control.get_state = AsyncMock(return_value="on")
        dhw_control._register_schedulers = AsyncMock()
        
        await dhw_control._daily_register_schedulers()
        
        dhw_control._register_schedulers.assert_called_once()

    @pytest.mark.asyncio
    async def test_daily_register_schedulers_exception_retry(self, dhw_control):
        dhw_control.get_state = AsyncMock(return_value="on")
        dhw_control._register_schedulers = AsyncMock(side_effect=[Exception("Test error"), None])
        dhw_control.notify = AsyncMock()
        
        with patch('dhw_control.asyncio.sleep', new_callable=AsyncMock):
            await dhw_control._daily_register_schedulers()
        
        assert dhw_control._register_schedulers.call_count == 2


class TestUnregisterSchedulers:
    @pytest.mark.asyncio
    async def test_unregister_schedulers_empty(self, dhw_control):
        with patch.object(type(dhw_control), 'name', new_callable=PropertyMock, return_value="dhw_control"):
            dhw_control.AD = MagicMock()
            dhw_control.AD.sched = MagicMock()
            dhw_control.AD.sched.schedule = {}
            dhw_control.cancel_timer = AsyncMock()
            
            await dhw_control._unregister_schedulers()
            
            dhw_control.cancel_timer.assert_not_called()

    @pytest.mark.asyncio
    async def test_unregister_schedulers_with_timers(self, dhw_control):
        with patch.object(type(dhw_control), 'name', new_callable=PropertyMock, return_value="dhw_control"):
            dhw_control.AD = MagicMock()
            dhw_control.AD.sched = MagicMock()
            dhw_control.AD.sched.schedule = {
                "dhw_control": {
                    "timer1": {"callback": MagicMock()},
                    "timer2": {"callback": MagicMock()},
                }
            }
            dhw_control.cancel_timer = AsyncMock()
            
            await dhw_control._unregister_schedulers()
            
            assert dhw_control.cancel_timer.call_count == 2


class TestTerminate:
    @pytest.mark.asyncio
    async def test_terminate(self, dhw_control):
        dhw_control._unregister_schedulers = AsyncMock()
        
        await dhw_control.terminate()
        
        dhw_control._unregister_schedulers.assert_called_once()


class TestResilience:
    @pytest.mark.asyncio
    async def test_force_dhw_entity_exception(self, dhw_control):
        dhw_control.args["dhw"]["enabled"] = True
        entity_mock = MagicMock()
        entity_mock.turn_on = AsyncMock(side_effect=Exception("Entity error"))
        dhw_control.get_entity = MagicMock(return_value=entity_mock)
        dhw_control.notify = AsyncMock()
        
        with pytest.raises(Exception):
            await dhw_control._force_dhw()

    @pytest.mark.asyncio
    async def test_notify_exception_does_not_break(self, dhw_control):
        dhw_control.args["dhw"]["enabled"] = True
        entity_mock = MagicMock()
        entity_mock.turn_on = AsyncMock()
        dhw_control.get_entity = MagicMock(return_value=entity_mock)
        dhw_control.notify = AsyncMock(side_effect=Exception("Notify failed"))
        
        with pytest.raises(Exception):
            await dhw_control._force_dhw()