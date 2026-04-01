from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz
from last_connection_sensor import LastConnectionSensor


@pytest.fixture()
def last_connection_sensor():
    with patch.object(LastConnectionSensor, "__init__", lambda self: None):
        sensor = LastConnectionSensor()
        def debug_log(msg, level=None):
            if level == "DEBUG" or level is None:
                print(f"DEBUG: {msg}")
        sensor.log = debug_log
        sensor.run_minutely = MagicMock()
        sensor.get_timezone = MagicMock(return_value=pytz.timezone("Europe/Madrid"))
        sensor.set_state = MagicMock()
        return sensor


class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_registers_minutely_callback(self, last_connection_sensor):
        test_dt = datetime(2023, 1, 1, 12, 30, 45)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            await last_connection_sensor.initialize()
        
        last_connection_sensor.run_minutely.assert_called_once()
        call_args = last_connection_sensor.run_minutely.call_args
        assert call_args[0][0] == last_connection_sensor.update_last_connection


class TestUpdateLastConnection:
    @pytest.mark.asyncio
    async def test_update_last_connection_sets_state(self, last_connection_sensor):
        test_dt = datetime(2023, 1, 15, 14, 30, 45)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            await last_connection_sensor.update_last_connection({})
        
        last_connection_sensor.set_state.assert_called_once()
        call_args = last_connection_sensor.set_state.call_args
        assert call_args[0][0] == "sensor.last_appdaemon_connection"
        assert "2023-01-15" in call_args[1]["state"]

    @pytest.mark.asyncio
    async def test_update_last_connection_format(self, last_connection_sensor):
        test_dt = datetime(2023, 6, 15, 8, 5, 3)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            await last_connection_sensor.update_last_connection({})
        
        call_args = last_connection_sensor.set_state.call_args
        expected_format = "2023-06-15 08:05:03"
        assert call_args[1]["state"] == expected_format

    @pytest.mark.asyncio
    async def test_update_last_connection_midnight(self, last_connection_sensor):
        test_dt = datetime(2023, 12, 31, 0, 0, 0)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            await last_connection_sensor.update_last_connection({})
        
        call_args = last_connection_sensor.set_state.call_args
        expected_format = "2023-12-31 00:00:00"
        assert call_args[1]["state"] == expected_format

    @pytest.mark.asyncio
    async def test_update_last_connection_end_of_day(self, last_connection_sensor):
        test_dt = datetime(2023, 12, 31, 23, 59, 59)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            await last_connection_sensor.update_last_connection({})
        
        call_args = last_connection_sensor.set_state.call_args
        expected_format = "2023-12-31 23:59:59"
        assert call_args[1]["state"] == expected_format


class TestResilience:
    @pytest.mark.asyncio
    async def test_update_last_connection_exception_handling(self, last_connection_sensor):
        last_connection_sensor.set_state = MagicMock(side_effect=Exception("State error"))
        test_dt = datetime(2023, 1, 1, 12, 0, 0)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            with pytest.raises(Exception):
                await last_connection_sensor.update_last_connection({})

    @pytest.mark.asyncio
    async def test_initialize_with_timezone(self, last_connection_sensor):
        last_connection_sensor.run_minutely = MagicMock()
        last_connection_sensor.get_timezone = MagicMock(return_value=pytz.timezone("America/New_York"))
        test_dt = datetime(2023, 1, 1, 12, 30, 45)
        
        with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
            mock_datetime.now = MagicMock(return_value=test_dt)
            await last_connection_sensor.initialize()
        
        last_connection_sensor.get_timezone.assert_called()
        last_connection_sensor.run_minutely.assert_called_once()

    @pytest.mark.asyncio
    async def test_multiple_consecutive_updates(self, last_connection_sensor):
        for i in range(5):
            test_dt = datetime(2023, 1, 1, 12, 30, i)
            with patch('last_connection_sensor.datetime', wraps=datetime) as mock_datetime:
                mock_datetime.now = MagicMock(return_value=test_dt)
                await last_connection_sensor.update_last_connection({})
        
        assert last_connection_sensor.set_state.call_count == 5