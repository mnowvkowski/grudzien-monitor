import copy
from datetime import datetime, timezone
from html import escape
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

import monitor

PRODUCTS = json.loads((Path(__file__).parent / "products.json").read_text())
NOW = datetime(2026, 9, 28, 12, 30, tzinfo=timezone.utc)


def variant(product, available=False, **updates):
    value = {"variation_id": product["variation_id"], "attributes": product["attributes"],
             "is_in_stock": available, "is_purchasable": True,
             "variation_is_active": True, "variation_is_visible": True}
    value.update(updates)
    return value


def document(product, available=False, variants=None):
    values = variants if variants is not None else [variant(product, available)]
    return (f'<form class="variations_form cart" data-product_id="{product["product_id"]}" '
            f'data-product_variations="{escape(json.dumps(values), quote=True)}"></form>')


def empty_state():
    return {"version": 1, "products": {}, "failures": {}}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.out = patch("sys.stdout", new_callable=io.StringIO).start()
        self.err = patch("sys.stderr", new_callable=io.StringIO).start()
        self.addCleanup(patch.stopall)
        self.push = Mock()
        self.state = empty_state()

    def run_check(self, available=False, fetch=None, clock=None, **kwargs):
        return monitor.check_products(PRODUCTS, self.state, "test-topic",
                                      fetch=fetch or (lambda p: document(p, available)),
                                      push=self.push, clock=clock or (lambda: NOW), **kwargs)

    def test_exact_variant_in_exact_product_form(self):
        for product in PRODUCTS:
            selected = variant(product, False)
            other = variant(product, True, variation_id=123,
                            attributes={"attribute_akcyza": "Podmiot zwolniony z akcyzy"})
            wrong_form = document(dict(product, product_id=123), True)
            self.assertFalse(monitor.parse_available(wrong_form + document(product, variants=[other, selected]), product))
            self.assertTrue(monitor.parse_available(document(product, True), product))
            for bad in (dict(selected, variation_id=123),
                        dict(selected, attributes={"attribute_akcyza": "gospodarstwo domowe"}),
                        dict(selected, attributes={"attribute_akcyza": "Gospodarstwo domowe", "extra": "x"})):
                with self.assertRaises(monitor.MonitorError):
                    monitor.parse_available(document(product, variants=[bad]), product)

    def test_unknown_html_and_ambiguous_data_fail_closed(self):
        p = PRODUCTS[0]
        for html in ("<h1>Access denied</h1>", document(p, variants=[]),
                     document(p, variants=[variant(p), variant(p)]),
                     document(p) + document(p),
                     document(p).replace("data-product_variations=", "missing="),
                     '<form class="variations_form" data-product_id="963" data-product_variations="false"></form>',
                     '<form class="variations_form" data-product_id="963" data-product_variations="broken"></form>'):
            with self.subTest(html=html[:100]), self.assertRaises(monitor.MonitorError):
                monitor.parse_available(html, p)

    def test_strict_booleans_and_optional_flags(self):
        p = PRODUCTS[0]
        for flag in ("is_in_stock", "is_purchasable", "variation_is_active", "variation_is_visible"):
            for invalid in ("true", "false", 1, 0, None):
                with self.subTest(flag=flag, invalid=invalid), self.assertRaises(monitor.MonitorError):
                    monitor.parse_available(document(p, variants=[variant(p, True, **{flag: invalid})]), p)
            self.assertFalse(monitor.parse_available(document(p, variants=[variant(p, True, **{flag: False})]), p))
        value = variant(p, True)
        del value["variation_is_active"]
        del value["variation_is_visible"]
        self.assertTrue(monitor.parse_available(document(p, variants=[value]), p))
        del value["is_in_stock"]
        with self.assertRaises(monitor.MonitorError):
            monitor.parse_available(document(p, variants=[value]), p)

    def test_baseline_transitions_and_deduplication(self):
        self.assertEqual(self.run_check(), 0)
        self.push.assert_not_called()
        self.assertEqual(self.run_check(True), 0)
        self.assertEqual(self.push.call_count, 2)
        self.assertEqual(self.run_check(True), 0)
        self.assertEqual(self.push.call_count, 2)
        self.assertEqual(self.run_check(False), 0)
        self.assertEqual(self.push.call_count, 4)
        self.assertEqual(self.run_check(False), 0)
        self.assertEqual(self.push.call_count, 4)

    def test_first_available_alerts(self):
        self.assertEqual(self.run_check(True), 0)
        self.assertEqual(self.push.call_count, 2)
        self.assertTrue(all(s["available"] for s in self.state["products"].values()))

    def test_push_failure_keeps_state_and_retries(self):
        self.run_check(False)
        previous = copy.deepcopy(self.state)
        self.push.side_effect = RuntimeError("secret-topic-in-exception")
        self.assertEqual(self.run_check(True), 1)
        self.assertEqual(self.state, previous)
        self.assertNotIn("secret-topic-in-exception", self.err.getvalue())
        self.push.side_effect = None
        self.assertEqual(self.run_check(True), 0)
        self.assertEqual(self.push.call_count, 4)
        self.run_check(True)
        self.assertEqual(self.push.call_count, 4)

    def test_partial_failure_alert_once_and_recovery(self):
        self.run_check(False)
        previous = copy.deepcopy(self.state["products"][PRODUCTS[0]["key"]])
        def partial(product):
            if product == PRODUCTS[0]:
                raise URLError("secret-never-log")
            return document(product, True)
        self.assertEqual(self.run_check(fetch=partial), 1)
        self.assertEqual(self.state["products"][PRODUCTS[0]["key"]], previous)
        self.assertTrue(self.state["products"][PRODUCTS[1]["key"]]["available"])
        self.assertEqual(self.push.call_count, 2)
        self.assertEqual(self.run_check(fetch=partial), 1)
        self.assertEqual(self.push.call_count, 2)
        self.assertNotIn("secret-never-log", self.err.getvalue())
        self.run_check(True)
        self.assertEqual(self.state["failures"], {})
        self.run_check(fetch=partial)
        self.assertEqual(self.push.call_count, 4)

    def test_failure_notification_retried_until_accepted(self):
        self.push.side_effect = monitor.MonitorError("failed")
        self.assertEqual(self.run_check(fetch=lambda p: "invalid"), 1)
        self.assertEqual(self.state, empty_state())
        self.push.side_effect = None
        self.run_check(fetch=lambda p: "invalid")
        self.assertEqual(len(self.state["failures"]), 2)
        self.assertEqual(self.push.call_count, 4)

    def test_daily_timestamp_is_actual_success_time_without_repeated_changes(self):
        self.run_check()
        previous = copy.deepcopy(self.state)
        self.run_check(clock=lambda: NOW.replace(hour=14))
        self.assertEqual(self.state, previous)
        tomorrow = NOW.replace(day=29, hour=0, minute=2)
        self.run_check(clock=lambda: tomorrow)
        self.assertTrue(all(s["observed_at"] == tomorrow.isoformat(timespec="seconds")
                            for s in self.state["products"].values()))

    def test_dry_run_no_notification_or_state_changes(self):
        self.assertEqual(self.run_check(True, dry_run=True), 0)
        self.assertEqual(self.state, empty_state())
        self.push.assert_not_called()
        with patch.object(monitor, "load_state") as load, patch.object(monitor, "save_state") as save, \
                patch.object(monitor, "request_bytes", return_value=document(PRODUCTS[0], True).encode()), \
                patch.dict("os.environ", {}, clear=True):
            # One matching page, one mismatched: parsing still occurs in dry-run.
            self.assertEqual(monitor.main(["--dry-run"]), 1)
            load.assert_not_called()
            save.assert_not_called()

    def test_test_notification_only_leaves_state_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text("original bytes")
            with patch.object(monitor, "notify") as push, patch.object(monitor, "check_products") as check, \
                    patch.dict("os.environ", {"NTFY_TOPIC": "test-topic"}):
                self.assertEqual(monitor.main(["--test-notification", "--state", str(path)]), 0)
                push.assert_called_once()
                check.assert_not_called()
                self.assertEqual(path.read_text(), "original bytes")

    def test_main_saves_partial_success_even_on_error(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            with patch.object(monitor, "request_bytes", side_effect=[b"invalid", b'{"event":"message","topic":"test-topic","id":"x"}', document(PRODUCTS[1]).encode()]), \
                    patch.dict("os.environ", {"NTFY_TOPIC": "test-topic"}):
                self.assertEqual(monitor.main(["--state", str(path)]), 1)
            state = monitor.load_state(path)
            self.assertEqual(state["failures"], {PRODUCTS[0]["key"]: True})
            self.assertFalse(state["products"][PRODUCTS[1]["key"]]["available"])

    def test_http_retry_is_bounded_and_secret_errors_sanitized(self):
        with patch.object(monitor, "urlopen", side_effect=URLError("private-topic")) as opener, \
                patch.object(monitor.time, "sleep"):
            with self.assertRaises(monitor.MonitorError) as error:
                monitor.request_bytes(monitor.Request("https://example.com"))
            self.assertEqual(opener.call_count, 2)
            self.assertNotIn("private-topic", str(error.exception))
            self.assertEqual(opener.call_args.kwargs["timeout"], 12)

    def test_ntfy_requires_accepted_message_and_does_not_expose_topic(self):
        for response in (b"{}", b"not-json", b'{"event":"message","id":"x","topic":"wrong"}'):
            with patch.object(monitor, "request_bytes", return_value=response), self.assertRaises(monitor.MonitorError):
                monitor.notify("test-topic", "Title", "Body")
        with patch.object(monitor, "request_bytes", return_value=b'{"event":"message","id":"x","topic":"test-topic"}') as send:
            monitor.notify("test-topic", "Title", "Body", url=PRODUCTS[0]["url"])
            request = send.call_args.args[0]
            self.assertEqual(request.full_url, "https://ntfy.sh/")
            self.assertEqual(json.loads(request.data)["topic"], "test-topic")
            self.assertEqual(send.call_args.kwargs["attempts"], 1)

    def test_corrupt_state_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            path.write_text('{"version":1,"products":{"x":{"available":"false"}},"failures":{}}')
            original = path.read_bytes()
            with patch.dict("os.environ", {"NTFY_TOPIC": "test-topic"}):
                self.assertEqual(monitor.main(["--state", str(path)]), 1)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
