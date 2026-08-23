from astrbot_plugin_super_draw.settings import Settings


def test_settings_only_reads_available_models():
    settings = Settings(
        {
            "api_providers": [
                {
                    "name": "Images",
                    "api_type": "openai",
                    "api_keys": ["key"],
                    "available_models": ["gpt-image"],
                    "generation_models": ["ignored"],
                    "edit_models": ["ignored"],
                }
            ]
        }
    )

    assert settings.models == ["Images/gpt-image"]
    assert settings.select().model == "gpt-image"


def test_settings_rejects_unknown_protocols():
    settings = Settings(
        {
            "api_providers": [
                {
                    "api_type": "unknown",
                    "api_keys": ["key"],
                    "available_models": ["model"],
                }
            ]
        }
    )

    assert settings.providers == []


def test_settings_replaces_a_missing_selected_model():
    settings = Settings(
        {
            "generation": {"model": "Images/removed"},
            "api_providers": [
                {"name": "Images", "api_keys": ["key"], "available_models": ["image"]}
            ],
        }
    )

    assert settings.modelKey == "Images/image"
