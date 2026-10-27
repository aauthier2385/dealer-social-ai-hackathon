import unittest

from app.config import ConfigError, load_settings
from tests.helpers import FAKE_KEY, TEST_TOKEN, settings


class ConfigTests(unittest.TestCase):
    def test_defaults_are_safe(self):
        s = load_settings({"SERVICE_TOKEN": TEST_TOKEN})
        self.assertFalse(s.meta_enabled)  # Meta stays off unless explicitly turned on
        self.assertEqual(s.text_model, "muse-spark-1.3")
        self.assertEqual(s.meta_base_url, "https://api.meta.ai/v1")

    def test_short_or_missing_service_token_refuses_to_start(self):
        for bad in ["", "short", "x" * 31, "has a space in it 0123456789abcdef"]:
            with self.assertRaises(ConfigError):
                load_settings({"SERVICE_TOKEN": bad})

    def test_enabled_without_key_refuses_to_start(self):
        with self.assertRaises(ConfigError):
            load_settings({"SERVICE_TOKEN": TEST_TOKEN, "META_AI_ENABLED": "true"})

    def test_contributor_models_are_blocked(self):
        with self.assertRaises(ConfigError):
            settings(META_TEXT_MODEL="muse-spark-1.3-contributor")

    def test_base_url_must_be_https(self):
        with self.assertRaises(ConfigError):
            settings(META_BASE_URL="http://api.meta.ai/v1")

    def test_bad_boolean_and_number_values(self):
        with self.assertRaises(ConfigError):
            settings(META_AI_ENABLED="maybe")
        with self.assertRaises(ConfigError):
            settings(RATE_LIMIT_PER_CLIENT_PER_MINUTE="0")

    def test_repr_never_shows_secrets(self):
        text = repr(settings())
        self.assertNotIn(FAKE_KEY, text)
        self.assertNotIn(TEST_TOKEN, text)


if __name__ == "__main__":
    unittest.main()
