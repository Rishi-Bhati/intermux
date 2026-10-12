from __future__ import annotations

import unittest

from intermux import config
from intermux.errors import ValidationError
from intermux.helper import protocol
from intermux.helper.protocol import (
    ACTIONS,
    PROTOCOL_VERSION,
    error_response,
    ok_response,
    validate_env,
    validate_request,
)


def req(action, params=None, **extra):
    obj = {"v": PROTOCOL_VERSION, "action": action}
    if params is not None:
        obj["params"] = params
    obj.update(extra)
    return obj


class ValidRequestTests(unittest.TestCase):
    def test_status(self):
        self.assertEqual(validate_request(req("status")), ("status", {}))

    def test_status_with_explicit_empty_params(self):
        self.assertEqual(validate_request(req("status", {})), ("status", {}))

    def test_bind_defaults_filled(self):
        action, params = validate_request(req("bind", {"iface": "eth0", "app": "/usr/bin/firefox"}))
        self.assertEqual(action, "bind")
        self.assertEqual(
            params,
            {
                "iface": "eth0",
                "app": "/usr/bin/firefox",
                "running_policy": "ask",
                "allow_unsupported": False,
                "env": {},
            },
        )

    def test_bind_all_params(self):
        env = {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0", "LANG": "en_US.UTF-8"}
        _, params = validate_request(
            req(
                "bind",
                {
                    "iface": "wlp2s0",
                    "app": "/opt/app/bin/app",
                    "running_policy": "separate",
                    "allow_unsupported": True,
                    "env": env,
                },
            )
        )
        self.assertEqual(params["iface"], "wlp2s0")
        self.assertEqual(params["app"], "/opt/app/bin/app")
        self.assertEqual(params["running_policy"], "separate")
        self.assertIs(params["allow_unsupported"], True)
        self.assertEqual(params["env"], env)

    def test_bind_running_policies(self):
        for policy in protocol.RUNNING_POLICIES:
            _, params = validate_request(
                req("bind", {"iface": "eth0", "app": "/usr/bin/x", "running_policy": policy})
            )
            self.assertEqual(params["running_policy"], policy)

    def test_stop_app(self):
        self.assertEqual(
            validate_request(req("stop_app", {"iface": "eth0", "app": "/usr/bin/firefox"})),
            ("stop_app", {"iface": "eth0", "app": "/usr/bin/firefox"}),
        )

    def test_kill_app_default_force(self):
        self.assertEqual(
            validate_request(req("kill_app", {"app": "/usr/bin/firefox"})),
            ("kill_app", {"app": "/usr/bin/firefox", "force": False}),
        )

    def test_kill_app_force(self):
        self.assertEqual(
            validate_request(req("kill_app", {"app": "/usr/bin/firefox", "force": True})),
            ("kill_app", {"app": "/usr/bin/firefox", "force": True}),
        )

    def test_teardown_idle(self):
        self.assertEqual(validate_request(req("teardown_idle")), ("teardown_idle", {}))

    def test_reset_default_and_explicit(self):
        self.assertEqual(validate_request(req("reset")), ("reset", {"kill_apps": False}))
        self.assertEqual(
            validate_request(req("reset", {"kill_apps": True})), ("reset", {"kill_apps": True})
        )

    def test_verify(self):
        self.assertEqual(validate_request(req("verify", {"iface": "eth0"})), ("verify", {"iface": "eth0"}))

    def test_every_action_has_a_valid_example(self):
        examples = {
            "status": {},
            "bind": {"iface": "eth0", "app": "/usr/bin/x"},
            "stop_app": {"iface": "eth0", "app": "/usr/bin/x"},
            "kill_app": {"app": "/usr/bin/x"},
            "teardown_idle": {},
            "reset": {},
            "verify": {"iface": "eth0"},
            "adopt": {"iface": "eth0", "app": "/usr/bin/x"},
            "release_adoption": {"id": "0123456789abcdef"},
            "batch": {"ops": [{"op": "bind", "iface": "eth0", "app": "/usr/bin/x"}]},
        }
        self.assertEqual(set(examples), set(ACTIONS))
        for action, params in examples.items():
            with self.subTest(action=action):
                self.assertEqual(validate_request(req(action, params))[0], action)

    def test_valid_interface_names(self):
        for iface in ("eth0", "wlp2s0", "br-1a2b", "eth0:1", "wg_vpn.1", "a", "x" * 15):
            with self.subTest(iface=iface):
                _, params = validate_request(req("verify", {"iface": iface}))
                self.assertEqual(params["iface"], iface)


class RejectedRequestTests(unittest.TestCase):
    def assertRejected(self, obj):
        with self.assertRaises(ValidationError) as ctx:
            validate_request(obj)
        self.assertEqual(ctx.exception.code, "invalid_request")

    def test_rejects_non_object_request(self):
        for obj in (None, [], "status", 1, True):
            with self.subTest(obj=obj):
                self.assertRejected(obj)

    def test_rejects_bad_action(self):
        for action in ("format", "", "Status", None, 5, ["status"]):
            with self.subTest(action=action):
                self.assertRejected(req(action))

    def test_rejects_missing_action(self):
        self.assertRejected({"v": PROTOCOL_VERSION, "params": {}})

    def test_rejects_wrong_protocol_version(self):
        for version in (0, 2, "1", None, True, 1.0):
            with self.subTest(version=version):
                self.assertRejected({"v": version, "action": "status"})

    def test_rejects_missing_version(self):
        self.assertRejected({"action": "status"})

    def test_rejects_unknown_top_level_field(self):
        self.assertRejected(req("status", {}, extra=1))

    def test_rejects_non_object_params(self):
        for params in ([], "x", 1, None):
            with self.subTest(params=params):
                self.assertRejected({"v": PROTOCOL_VERSION, "action": "status", "params": params})

    def test_rejects_unknown_param_per_action(self):
        cases = {
            "status": {"x": 1},
            "bind": {"iface": "eth0", "app": "/usr/bin/x", "force": True},
            "stop_app": {"iface": "eth0", "app": "/usr/bin/x", "env": {}},
            "kill_app": {"app": "/usr/bin/x", "kill_apps": True},
            "teardown_idle": {"iface": "eth0"},
            "reset": {"force": True},
            "verify": {"iface": "eth0", "app": "/usr/bin/x"},
        }
        for action, params in cases.items():
            with self.subTest(action=action):
                self.assertRejected(req(action, params))

    def test_rejects_missing_required_params(self):
        self.assertRejected(req("bind", {"app": "/usr/bin/x"}))
        self.assertRejected(req("bind", {"iface": "eth0"}))
        self.assertRejected(req("stop_app", {"iface": "eth0"}))
        self.assertRejected(req("kill_app", {}))
        self.assertRejected(req("verify", {}))

    def test_rejects_bad_running_policy(self):
        for policy in ("", "Ask", "kill", None, 1):
            with self.subTest(policy=policy):
                self.assertRejected(
                    req("bind", {"iface": "eth0", "app": "/usr/bin/x", "running_policy": policy})
                )

    def test_rejects_non_bool_flags(self):
        for value in (1, 0, "yes", None, []):
            with self.subTest(value=value):
                self.assertRejected(
                    req("bind", {"iface": "eth0", "app": "/usr/bin/x", "allow_unsupported": value})
                )
                self.assertRejected(req("kill_app", {"app": "/usr/bin/x", "force": value}))
                self.assertRejected(req("reset", {"kill_apps": value}))


class EnvValidationTests(unittest.TestCase):
    def bind_with_env(self, env):
        return req("bind", {"iface": "eth0", "app": "/usr/bin/x", "env": env})

    def test_allowlisted_keys_accepted(self):
        env = {key: "value" for key in config.ENV_ALLOWLIST}
        self.assertEqual(validate_env(env), env)
        _, params = validate_request(self.bind_with_env(env))
        self.assertEqual(params["env"], env)

    def test_validate_env_returns_plain_dict(self):
        result = validate_env({"DISPLAY": ":0"})
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"DISPLAY": ":0"})

    def test_rejects_key_outside_allowlist(self):
        for key in ("PATH", "LD_PRELOAD", "HOME", "PYTHONPATH", "display", "", "XDG_DATA_DIRS"):
            with self.subTest(key=key):
                with self.assertRaises(ValidationError):
                    validate_env({key: "x"})
                with self.assertRaises(ValidationError):
                    validate_request(self.bind_with_env({key: "x"}))

    def test_rejects_non_string_key_or_value(self):
        with self.assertRaises(ValidationError):
            validate_env({1: "x"})
        with self.assertRaises(ValidationError):
            validate_env({"DISPLAY": 0})
        with self.assertRaises(ValidationError):
            validate_env({"DISPLAY": None})

    def test_rejects_newline_and_nul_in_value(self):
        for value in ("a\nb", "a\rb", "a\x00b", "trailing\n"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    validate_env({"LANG": value})

    def test_rejects_overlong_value(self):
        with self.assertRaises(ValidationError):
            validate_env({"LANG": "x" * 4097})
        self.assertEqual(validate_env({"LANG": "x" * 4096}), {"LANG": "x" * 4096})

    def test_rejects_non_object_env(self):
        for env in ([], "DISPLAY=:0", None, 1):
            with self.subTest(env=env):
                with self.assertRaises(ValidationError):
                    validate_env(env)
                with self.assertRaises(ValidationError):
                    validate_request(self.bind_with_env(env))

    def test_env_not_allowed_for_other_actions(self):
        with self.assertRaises(ValidationError):
            validate_request(req("stop_app", {"iface": "eth0", "app": "/usr/bin/x", "env": {}}))


class AppPathTests(unittest.TestCase):
    def test_rejects_newline_in_app_path(self):
        for app in ("/usr/bin/firefox\n", "/usr/bin/\nfirefox", "/usr/bin/fire\rfox", "/usr/bin/fire\x00fox"):
            with self.subTest(app=app):
                with self.assertRaises(ValidationError):
                    validate_request(req("bind", {"iface": "eth0", "app": app}))
                with self.assertRaises(ValidationError):
                    validate_request(req("kill_app", {"app": app}))

    def test_rejects_relative_app_path(self):
        for app in ("firefox", "./firefox", "bin/firefox", "", "../usr/bin/x"):
            with self.subTest(app=app):
                with self.assertRaises(ValidationError):
                    validate_request(req("bind", {"iface": "eth0", "app": app}))
                with self.assertRaises(ValidationError):
                    validate_request(req("stop_app", {"iface": "eth0", "app": app}))
                with self.assertRaises(ValidationError):
                    validate_request(req("kill_app", {"app": app}))

    def test_rejects_directory_app_path(self):
        with self.assertRaises(ValidationError):
            validate_request(req("kill_app", {"app": "/usr/bin/"}))

    def test_rejects_non_string_app(self):
        for app in (None, 5, ["/usr/bin/x"]):
            with self.subTest(app=app):
                with self.assertRaises(ValidationError):
                    validate_request(req("kill_app", {"app": app}))

    def test_rejects_overlong_app_path(self):
        with self.assertRaises(ValidationError):
            validate_request(req("kill_app", {"app": "/" + "a" * 4096}))

    def test_accepts_absolute_app_path(self):
        _, params = validate_request(req("kill_app", {"app": "/usr/bin/firefox"}))
        self.assertEqual(params["app"], "/usr/bin/firefox")
        _, params = validate_request(req("kill_app", {"app": "/" + "a" * 4095}))
        self.assertEqual(len(params["app"]), 4096)


class InterfaceNameTests(unittest.TestCase):
    def test_rejects_bad_iface_name(self):
        bad = [
            "",
            "x" * 16,
            "eth0 ",
            " eth0",
            "eth0\n",
            "eth0;rm",
            "a/b",
            "e th",
            "eth0$",
            "é0",
            ".",
            "..",
            None,
            5,
            ["eth0"],
        ]
        for iface in bad:
            with self.subTest(iface=iface):
                with self.assertRaises(ValidationError):
                    validate_request(req("verify", {"iface": iface}))
                with self.assertRaises(ValidationError):
                    validate_request(req("bind", {"iface": iface, "app": "/usr/bin/x"}))
                with self.assertRaises(ValidationError):
                    validate_request(req("stop_app", {"iface": iface, "app": "/usr/bin/x"}))


class ResponseTests(unittest.TestCase):
    def test_ok_response(self):
        self.assertEqual(ok_response({"a": 1}), {"ok": True, "data": {"a": 1}})
        self.assertEqual(ok_response(None), {"ok": True, "data": None})

    def test_error_response(self):
        self.assertEqual(
            error_response("conflict", "busy"),
            {"ok": False, "error": {"code": "conflict", "message": "busy", "data": None}},
        )
        self.assertEqual(
            error_response("already_running", "running", {"pids": [1]}),
            {"ok": False, "error": {"code": "already_running", "message": "running", "data": {"pids": [1]}}},
        )


class AdoptActionTests(unittest.TestCase):
    def test_batch_valid(self):
        ops = [{"op": "bind", "iface": "eth0", "app": "/usr/bin/x", "running_policy": "separate"},
               {"op": "adopt", "iface": "wlan0", "app": "/usr/bin/y"}]
        action, params = validate_request(req("batch", {"ops": ops}))
        self.assertEqual(action, "batch")
        self.assertEqual(params["ops"][0]["running_policy"], "separate")
        self.assertEqual(params["ops"][0]["env"], {})
        self.assertEqual(params["ops"][1], {"op": "adopt", "iface": "wlan0", "app": "/usr/bin/y"})

    def test_batch_rejects_bad_input(self):
        good = {"op": "bind", "iface": "eth0", "app": "/usr/bin/x"}
        bad = [
            [],
            [dict(good) for _ in range(51)],
            [dict(good, op="reset")],
            [dict(good, iface="bad iface")],
            [dict(good, extra=1)],
            [dict(good, app="relative")],
            [dict(good, env={"LD_PRELOAD": "x"})],
            [{"op": "adopt", "iface": "eth0", "app": "/usr/bin/x", "env": {}}],
            ["bind"],
        ]
        for ops in bad:
            with self.subTest(ops=str(ops)[:60]):
                with self.assertRaises(ValidationError):
                    validate_request(req("batch", {"ops": ops}))
        with self.assertRaises(ValidationError):
            validate_request(req("batch", {}))
        self.assertEqual(len(validate_request(req("batch", {"ops": [good] * 50}))[1]["ops"]), 50)

    def test_adopt_accepted(self):
        action, params = validate_request(req("adopt", {"iface": "wlan0", "app": "/usr/bin/firefox"}))
        self.assertEqual(action, "adopt")
        self.assertEqual(params, {"iface": "wlan0", "app": "/usr/bin/firefox"})

    def test_adopt_rejects_bad_params(self):
        for params in ({"iface": "wlan0"}, {"app": "/usr/bin/x"}, {"iface": "a b", "app": "/usr/bin/x"},
                       {"iface": "wlan0", "app": "relative"}, {"iface": "wlan0", "app": "/x", "extra": 1}):
            with self.assertRaises(ValidationError):
                validate_request(req("adopt", params))

    def test_release_adoption_accepted(self):
        action, params = validate_request(req("release_adoption", {"id": "0123456789abcdef"}))
        self.assertEqual(action, "release_adoption")
        self.assertEqual(params, {"id": "0123456789abcdef"})

    def test_release_adoption_rejects_bad_ids(self):
        for bad in ("", "short", "ABCDEF0123", "../../etc", "g" * 10, "a" * 33, 12345678, None):
            with self.assertRaises(ValidationError):
                validate_request(req("release_adoption", {"id": bad}))
        with self.assertRaises(ValidationError):
            validate_request(req("release_adoption", {}))

    def test_actions_listed(self):
        self.assertIn("adopt", ACTIONS)
        self.assertIn("release_adoption", ACTIONS)


if __name__ == "__main__":
    unittest.main()
