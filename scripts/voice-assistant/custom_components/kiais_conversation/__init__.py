"""Kiais conversation agent: Home Assistant Assist on an OpenAI-compatible gateway."""

from __future__ import annotations

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_API_KEY, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_BASE_URL

PLATFORMS = [Platform.CONVERSATION]


async def async_check_gateway(
    hass: HomeAssistant, base_url: str, api_key: str | None
) -> list[str]:
    """Return the model ids the gateway lists; raise on auth or connection errors."""
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with session.get(
        f"{base_url.rstrip('/')}/models",
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status in (401, 403):
            raise ConfigEntryAuthFailed("invalid API key")
        resp.raise_for_status()
        body = await resp.json()
    return [m["id"] for m in body.get("data", []) if "id" in m]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the agent from a config entry."""
    try:
        await async_check_gateway(
            hass, entry.data[CONF_BASE_URL], entry.data.get(CONF_API_KEY)
        )
    except ConfigEntryAuthFailed:
        raise
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"gateway not reachable: {err}") from err
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
