"""Tests for report payload validation and token handling. No network access."""

from __future__ import annotations

import os
import unittest
import urllib.request
from unittest import mock

from intermux import report
from intermux.config import VERSION
from intermux.errors import InterMuxError, ValidationError


class BuildPayloadTests(unittest.TestCase):
    def test_minimal_payload(self) -> None:
        payload = report.build_payload(title="  Crash  ", description=" It fails ")
        self.assertEqual(payload, {"title": "Crash", "description": "It fails", "report_type": "bug"})

    def test_optional_fields_included_when_given(self) -> None:
        payload = report.build_payload(
            title="Add X", description="Please", report_type="feature",
            impact="high", frequency="weekly", steps="1. open")
        self.assertEqual(payload["report_type"], "feature")
        self.assertEqual(payload["impact"], "high")
        self.assertEqual(payload["frequency"], "weekly")
        self.assertEqual(payload["steps"], "1. open")

    def test_blank_optional_fields_are_omitted(self) -> None:
        payload = report.build_payload(title="T", description="D", impact=None, frequency=None, steps="   ")
        self.assertNotIn("impact", payload)
        self.assertNotIn("frequency", payload)
        self.assertNotIn("steps", payload)

    def test_missing_title_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            report.build_payload(title="   ", description="D")

    def test_missing_description_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            report.build_payload(title="T", description="")

    def test_unknown_type_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            report.build_payload(title="T", description="D", report_type="rant")

    def test_unknown_impact_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            report.build_payload(title="T", description="D", impact="extreme")

    def test_unknown_frequency_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            report.build_payload(title="T", description="D", frequency="yearly")

    def test_length_limits(self) -> None:
        with self.assertRaises(ValidationError):
            report.build_payload(title="x" * (report.TITLE_MAX + 1), description="D")
        with self.assertRaises(ValidationError):
            report.build_payload(title="T", description="x" * (report.DESCRIPTION_MAX + 1))
        with self.assertRaises(ValidationError):
            report.build_payload(title="T", description="D", steps="x" * (report.STEPS_MAX + 1))


class EnvironmentTests(unittest.TestCase):
    def test_environment_appended_to_description(self) -> None:
        base = report.build_payload(title="T", description="Original text")
        attached = report.with_environment(base)
        self.assertTrue(attached["description"].startswith("Original text"))
        self.assertIn("Environment:", attached["description"])
        self.assertIn(f"InterMux {VERSION}", attached["description"])
        self.assertEqual(base["description"], "Original text", "the input payload must not change")

    def test_summary_has_no_username_or_home(self) -> None:
        summary = report.system_summary()
        self.assertNotIn(os.path.expanduser("~"), summary)
        self.assertIn("Python", summary)


class TokenTests(unittest.TestCase):
    def test_token_read_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {report.TOKEN_ENV: "  pub:sec  "}):
            self.assertEqual(report.configured_token(), "pub:sec")

    def test_blank_token_is_not_configured(self) -> None:
        with mock.patch.dict(os.environ, {report.TOKEN_ENV: "   "}):
            self.assertIsNone(report.configured_token())

    def test_submit_without_token_raises_before_network(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != report.TOKEN_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("urllib.request.urlopen") as urlopen:
                with self.assertRaises(InterMuxError) as ctx:
                    report.submit({"title": "T", "description": "D", "report_type": "bug"})
        self.assertEqual(ctx.exception.code, "not_configured")
        urlopen.assert_not_called()

    def test_web_form_url_is_https(self) -> None:
        self.assertTrue(report.REPORT_PAGE_URL.startswith("https://"))
        self.assertTrue(report.API_URL.startswith("https://"))


class RedirectTests(unittest.TestCase):
    def test_redirect_handler_is_present_and_refuses_redirects(self) -> None:
        self.assertTrue(issubclass(report.NoRedirect, urllib.request.HTTPRedirectHandler))
        handler = report.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://example.invalid/"))


if __name__ == "__main__":
    unittest.main()
