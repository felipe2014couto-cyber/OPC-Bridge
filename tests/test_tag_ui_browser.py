"""Optional local Firefox smoke test, using existing tools and simulated OPC only."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from wsgiref.simple_server import make_server

import pytest

from tests import test_tag_ui
from tests.test_admin_api import ADMIN_TOKEN

runtime = test_tag_ui.runtime
PROG_ID = test_tag_ui.PROG_ID


@pytest.mark.skipif(os.environ.get("OPC_BRIDGE_TEST_BROWSER") != "1",
                    reason="Opt-in smoke test uses existing Firefox/geckodriver")
def test_browser_validate_confirm_apply(runtime, tmp_path, monkeypatch):
    driver = shutil.which("geckodriver")
    if driver is None or shutil.which("firefox") is None:
        pytest.skip("Firefox/geckodriver are not installed; no dependencies are installed by this test")
    app, bridge, database, _, _ = runtime
    pushed = []
    monkeypatch.setattr(bridge, "dispatch_admin_config_operation_threadsafe",
                        lambda agent, operation, payload, **kwargs: pushed.append(payload))

    def proxy_app(environ, start_response):
        environ["HTTP_AUTHORIZATION"] = "Bearer " + ADMIN_TOKEN
        return app(environ, start_response)

    httpd = make_server("127.0.0.1", 0, proxy_app)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        driver_port = sock.getsockname()[1]
    log = (tmp_path / "driver.log").open("wb")
    process = subprocess.Popen([driver, "--host", "127.0.0.1", "--port", str(driver_port),
                                "--log", "fatal"], stdout=log, stderr=log)
    address = "http://127.0.0.1:" + str(driver_port)
    session = None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(address + path, data=body, method=method,
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=30) as response:
            return json.load(response)["value"]

    def command(method, path, data=None):
        return call(method, "/session/" + session + path, data)

    def execute(script):
        return command("POST", "/execute/sync", {"script": script, "args": []})

    def click(selector):
        element = command("POST", "/element", {"using": "css selector", "value": selector})
        element_id = element["element-6066-11e4-a52e-4f735466cecf"]
        command("POST", "/element/" + element_id + "/click", {})

    def wait(script):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if execute("return " + script):
                return
            time.sleep(.1)
        pytest.fail("Browser state did not reach expected condition")

    try:
        deadline = time.monotonic() + 15
        while True:
            try:
                call("GET", "/status")
                break
            except urllib.error.URLError:
                if time.monotonic() >= deadline:
                    pytest.fail("Local geckodriver did not start")
                time.sleep(.1)
        session = call("POST", "/session", {"capabilities": {"alwaysMatch": {
            "browserName": "firefox", "unhandledPromptBehavior": "ignore",
            "moz:firefoxOptions": {"args": ["-headless"]}}}})["sessionId"]
        command("POST", "/url", {"url": "http://127.0.0.1:" + str(httpd.server_port) + "/ui"})
        assert execute("return document.getElementById('login-form') === null") is True
        assert execute("return document.getElementById('token') === null") is True
        assert execute("return document.getElementById('logout') === null") is True
        wait("!document.getElementById('workspace').hidden")
        wait("!document.getElementById('find-servers').disabled")
        click("#find-servers")
        wait("document.getElementById('servers').options.length === 3")
        command("POST", "/execute/sync", {"script": (
            "const servers=document.getElementById('servers'); servers.value=arguments[0];"
            "servers.dispatchEvent(new Event('change'));"
            "const tag=document.querySelector('#tags input'); tag.value='Good.Tag';"
            "tag.dispatchEvent(new Event('input'));"), "args": [PROG_ID]})
        click("#validate-all")
        wait("!document.getElementById('apply').disabled")
        assert pushed == []
        assert "Escrita OPC indisponível" in execute("return document.body.textContent")
        click("#apply")
        assert "Aplicar este plano" in command("GET", "/alert/text")
        command("POST", "/alert/dismiss", {})
        assert pushed == []
        click("#apply")
        command("POST", "/alert/accept", {})
        wait("document.getElementById('operations').textContent.includes('pending')")
        assert len(pushed) == 1 and pushed[0].opc_prog_id == PROG_ID
        assert execute("return document.getElementById('token') === null") is True
        html = execute("return document.documentElement.outerHTML")
        assert ADMIN_TOKEN not in html and "PRIVATE-TOKEN" not in html and "SECRET-HASH" not in html
        (tmp_path / "tag-ui.png").write_bytes(base64.b64decode(command("GET", "/screenshot")))
        with database.session() as repo:
            assert repo.latest_applied_snapshot("agent-a").version == 1
    finally:
        if session is not None:
            call("DELETE", "/session/" + session)
        process.terminate()
        process.wait(timeout=10)
        log.close()
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=5)
