"""Config flow for the Kiais conversation integration."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_API_KEY, CONF_LLM_HASS_API, CONF_MODEL, CONF_PROMPT
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import llm
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TemplateSelector,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import async_check_gateway
from .const import (
    CONF_BASE_URL,
    CONF_MAX_HISTORY,
    CONF_MAX_TOKENS,
    CONF_TEMPERATURE,
    CONF_TIMEOUT,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_HISTORY,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_PROMPT,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT,
    DOMAIN,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL, default=DEFAULT_BASE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Optional(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Required(CONF_MODEL, default=DEFAULT_MODEL): str,
    }
)


class KiaisConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the gateway URL, key and model."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match(
                {CONF_BASE_URL: user_input[CONF_BASE_URL], CONF_MODEL: user_input[CONF_MODEL]}
            )
            try:
                models = await async_check_gateway(
                    self.hass, user_input[CONF_BASE_URL], user_input.get(CONF_API_KEY)
                )
            except ConfigEntryAuthFailed:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, ValueError):
                errors["base"] = "cannot_connect"
            else:
                if models and user_input[CONF_MODEL] not in models:
                    errors[CONF_MODEL] = "unknown_model"
                else:
                    return self.async_create_entry(
                        title=f"Kiais {user_input[CONF_MODEL]}",
                        data=user_input,
                        options={
                            CONF_PROMPT: DEFAULT_PROMPT,
                            CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
                        },
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return KiaisOptionsFlow()


class KiaisOptionsFlow(OptionsFlow):
    """Prompt, tools and limits."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            if not user_input.get(CONF_LLM_HASS_API):
                user_input.pop(CONF_LLM_HASS_API, None)
            return self.async_create_entry(data=user_input)

        apis = [
            SelectOptionDict(label=api.name, value=api.id)
            for api in llm.async_get_apis(self.hass)
        ]
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PROMPT,
                    description={"suggested_value": opts.get(CONF_PROMPT, DEFAULT_PROMPT)},
                ): TemplateSelector(),
                vol.Optional(
                    CONF_LLM_HASS_API,
                    description={"suggested_value": opts.get(CONF_LLM_HASS_API)},
                ): SelectSelector(SelectSelectorConfig(options=apis, multiple=True)),
                vol.Optional(
                    CONF_TEMPERATURE,
                    default=opts.get(CONF_TEMPERATURE, DEFAULT_TEMPERATURE),
                ): NumberSelector(NumberSelectorConfig(min=0, max=2, step=0.05)),
                vol.Optional(
                    CONF_MAX_TOKENS, default=opts.get(CONF_MAX_TOKENS, DEFAULT_MAX_TOKENS)
                ): NumberSelector(NumberSelectorConfig(min=32, max=4096, step=1)),
                vol.Optional(
                    CONF_TIMEOUT, default=opts.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
                ): NumberSelector(NumberSelectorConfig(min=5, max=300, step=1)),
                vol.Optional(
                    CONF_MAX_HISTORY,
                    default=opts.get(CONF_MAX_HISTORY, DEFAULT_MAX_HISTORY),
                ): NumberSelector(NumberSelectorConfig(min=0, max=100, step=1)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
