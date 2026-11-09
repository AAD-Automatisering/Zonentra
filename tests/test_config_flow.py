"""Real config/options flow manager tests."""

from unittest.mock import patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.zonentra.const import DOMAIN


async def test_user_and_singleton(hass):
    """0.6: the first step chooses the type; the central configuration is unique."""
    with patch("custom_components.zonentra.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        assert result["type"] == FlowResultType.MENU
        assert result["menu_options"] == ["central", "zone"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "central"}
        )
        assert result["type"] == FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": "   "}
        )
        assert result["errors"] == {"name": "invalid_name"}
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"name": " Office "}
        )
        assert result["type"] == FlowResultType.CREATE_ENTRY
        assert result["data"] == {"entry_type": "central", "name": "Office"}
        assert result["result"].unique_id == "central"
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "central"}
        )
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "central_already_configured"
