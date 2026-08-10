"""Pins for the 2026-08-08 security review fixes. Several of these were
confirmed-by-probe exploits (arbitrary file read, library-wipe, stored XSS)."""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "sim"))
sys.path.insert(0, os.path.join(HERE, "scripts"))


def load_server():
    os.environ.setdefault("FLOTILLA_LIB", tempfile.mkdtemp())
    spec = importlib.util.spec_from_file_location("srv", os.path.join(HERE, "server.py"))
    m = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(m)
    except SystemExit:
        pass
    return m


def main():
    srv = load_server()

    # F1 — _safe_path refuses traversal + absolute components
    base = tempfile.mkdtemp()
    os.makedirs(os.path.join(base, "series", "real"))
    open(os.path.join(base, "series", "real", "g1.json"), "w").write("{}")
    open(os.path.join(base, "secret.json"), "w").write("SECRET")
    ok = srv._safe_path(os.path.join(base, "series"), "real", "g1.json")
    assert ok.endswith("real/g1.json")
    for bad in [("..", "secret.json"), ("/etc/hostname",), (".", "x"),
                ("real/../../secret.json",)]:
        try:
            srv._safe_path(os.path.join(base, "series"), *bad)
            raise AssertionError(f"traversal allowed: {bad}")
        except ValueError:
            pass

    # F4 — _san neutralizes the library-wipe names, path-callers still guarded
    for evil in ("..", ".", "...", "/", "....//"):
        s = srv._san(evil)
        assert ".." not in s and not s.startswith("/"), f"_san({evil!r})={s!r}"
    assert srv._san("My Run!") == "My-Run"
    assert srv._san("") == "" and srv._san("normal") == "normal"

    # F3/F5/hub — _esc and js embed-safety neutralize the XSS carriers
    assert srv._esc("<script>&\"'") == "&lt;script&gt;&amp;&quot;&#39;"
    import make_bundle
    payload = make_bundle.js_embed_safe(json.dumps(
        {"t": "</script><img src=x onerror=alert(1)>"}))
    assert "</script>" not in payload and "\\u003c" in payload
    # value still round-trips (escaped only inside the JSON string)
    assert json.loads(payload.replace("\\u003c", "<").replace("\\u003e", ">")
                      )["t"] == "</script><img src=x onerror=alert(1)>"

    # F6 — constant-time bearer check, correct semantics
    assert srv._bearer_ok("tok", "tok") is True
    assert srv._bearer_ok("tok", "toX") is False
    assert srv._bearer_ok(None, "tok") is False and srv._bearer_ok("", "") is True

    # --- wringer pass 4 (2026-08-09) ---

    # W1 — provider base_url SSRF guard: https-only, public-host-only, and
    # provider flavour chosen by the parsed HOST not a URL substring
    for bad in ["http://api.anthropic.com/v1",       # not https
                "https://127.0.0.1/v1",              # loopback
                "https://169.254.169.254/latest",    # cloud metadata
                "https://10.0.0.5/v1",               # RFC1918
                "https://[::1]/v1"]:                 # loopback v6
        try:
            srv._guard_provider_url(bad)
            raise AssertionError(f"SSRF guard allowed {bad}")
        except ValueError:
            pass
    # a public host resolves fine (DNS may be unavailable in CI — tolerate)
    try:
        assert srv._guard_provider_url("https://api.anthropic.com/v1") \
            == "api.anthropic.com"
    except ValueError as e:
        assert "resolve" in str(e)

    # W2 — the delete/rename/archive path key is _san'd, so ".." can never
    # resolve to the library root (os.path.basical('..')=='..' would have)
    assert srv._san("..") == "" and srv._san("../../etc") == "etc"
    assert srv._san("normal-name") == "normal-name"

    # W3 — a credential file is 0600 from the first byte (no chmod race)
    d = tempfile.mkdtemp()
    fp = os.path.join(d, "cred.json")
    srv._write_secret_file(fp, '{"k":"v"}')
    assert oct(os.stat(fp).st_mode & 0o777) == "0o600"
    assert json.load(open(fp))["k"] == "v"

    # W4 — worker provider keys are scoped to the job's models; the builtin
    # always ships, an unrelated third-party rung's key does NOT
    srv.KEYSTORE = os.path.join(tempfile.mkdtemp(), "server-keys.json")
    srv._save_keystore({"providers": [
        {"id": "digitalocean", "builtin": True, "enabled": True, "order": 0},
        {"id": "bt", "key": "sk-secret-bt", "enabled": True, "order": 1,
         "model_map": {"kimi-k3": "BT/k3"}},
        {"id": "zz", "key": "sk-secret-zz", "enabled": True, "order": 2,
         "models": ["some-other-model"]}],
        "fallback": {}})
    scoped = json.loads(srv._providers_json(
        srv._cfg_models({"bots": [{"model": "kimi-k3"}, "merchant"]})))
    ids = [p["id"] for p in scoped["providers"]]
    assert "digitalocean" in ids and "bt" in ids and "zz" not in ids, ids
    full = json.loads(srv._providers_json())          # None = local = full
    assert {"digitalocean", "bt", "zz"} <= {p["id"] for p in full["providers"]}

    # W5 — aux POST bearer resolves from the path (pre-body-read auth)
    srv.AUX["job-x"] = {"bearer": "sekret"}
    assert srv._aux_post_bearer_ok("/api/aux/job-x/game", "sekret") is True
    assert srv._aux_post_bearer_ok("/api/aux/job-x/game", "wrong") is False
    assert srv._aux_post_bearer_ok("/api/aux/nope/game", "sekret") is False
    assert srv._aux_post_bearer_ok("/api/rename", "sekret") is False
    srv.AUX.pop("job-x", None)

    print("PASS test_security")


if __name__ == "__main__":
    main()
