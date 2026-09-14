import unittest
from unittest.mock import patch

from abita_s2s.config import load_middleware_config


class MiddlewareConfigTests(unittest.TestCase):
    def config(self, **env):
        with patch.dict("os.environ", env, clear=True):
            return load_middleware_config()

    def test_sandbox_is_default_and_uses_sandbox_office(self):
        config = self.config(
            SANDBOX_AMD_API_URL="https://sandbox.example/",
            SANDBOX_AMD_API_TOKEN="sandbox-secret",
        )
        self.assertEqual(config.base_url, "https://sandbox.example")
        self.assertEqual(config.office_override, "spring_hill")
        self.assertNotIn("sandbox-secret", repr(config))

    def test_missing_sandbox_never_falls_back_to_production(self):
        with self.assertRaises(ValueError):
            self.config(AMD_API_URL="https://prod.example", AMD_API_TOKEN="production")

    def test_isolation_rejects_shared_origin_or_token(self):
        env = dict(
            SANDBOX_AMD_API_URL="https://sandbox.example",
            SANDBOX_AMD_API_TOKEN="sandbox",
            AMD_API_URL="https://prod.example",
            AMD_API_TOKEN="production",
        )
        with self.assertRaisesRegex(ValueError, "origins"):
            self.config(
                **(env | {"SANDBOX_AMD_API_URL": "https://PROD.example:443/path"})
            )
        with self.assertRaisesRegex(ValueError, "tokens"):
            self.config(**(env | {"SANDBOX_AMD_API_TOKEN": "production"}))

    def test_unsafe_urls_are_rejected_without_echoing_secrets(self):
        for url in (
            "http://sandbox.example",
            "https://secret@sandbox.example",
            "https://sandbox.example?secret=value",
            "https://sandbox.example#secret",
        ):
            with self.assertRaises(ValueError) as error:
                self.config(SANDBOX_AMD_API_URL=url, SANDBOX_AMD_API_TOKEN="token")
            self.assertNotIn("secret", str(error.exception))

    def test_production_is_explicit_and_blocked_for_named_deployment_or_console(self):
        env = dict(
            ABITA_MIDDLEWARE_ENV="production",
            AMD_API_URL="https://prod.example",
            AMD_API_TOKEN="production",
        )
        self.assertIsNone(self.config(**env).office_override)
        with self.assertRaises(ValueError):
            self.config(**env, LIVEKIT_AGENT_DEPLOYMENT="preview")
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(ValueError):
                load_middleware_config(console=True)
