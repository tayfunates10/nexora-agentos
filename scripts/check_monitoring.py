"""Smoke-check the opt-in local stack without printing credentials or metric data."""

import base64
import json
import os
import re
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
RULES_FILE = REPO_ROOT / "infra/monitoring/prometheus/rules.yml"


def declared_rules():
    """Rule names the repository ships, read without adding a YAML dependency.

    Comparing against the file rather than a count means a rule added later is
    checked for having loaded, instead of silently failing this smoke test on a
    number nobody remembered to raise.
    """
    text = RULES_FILE.read_text()
    recording = set(re.findall(r"^  - record: (\S+)$", text, re.MULTILINE))
    alerting = set(re.findall(r"^  - alert: (\S+)$", text, re.MULTILINE))
    assert recording and alerting, "No rules parsed; check the rule file's layout"
    return recording, alerting


def read_json(url, authorization=None):
    headers = {"Authorization": authorization} if authorization else {}
    with urlopen(Request(url, headers=headers), timeout=5) as response:
        return json.load(response)


def evaluated_rules(deadline):
    """Rules Prometheus has reached a verdict on.

    A rule reports health "unknown" until its group is first evaluated, which a
    stack that has just started can easily be asked about first. Waiting for the
    verdict rather than reading it immediately keeps a genuine "err" failing the
    check instead of a race deciding it.
    """
    while True:
        groups = read_json("http://127.0.0.1:9090/api/v1/rules")["data"]["groups"]
        rules = [rule for group in groups for rule in group["rules"]]
        if rules and all(rule["health"] != "unknown" for rule in rules):
            return rules
        if time.monotonic() >= deadline:
            raise RuntimeError("Prometheus did not evaluate its rules in time")
        time.sleep(1)


def main():
    deadline = time.monotonic() + 90
    while True:
        try:
            targets = read_json("http://127.0.0.1:9090/api/v1/targets")["data"][
                "activeTargets"
            ]
            jobs = {target["labels"]["job"]: target["health"] for target in targets}
            if jobs == {"nexora-api": "up", "nexora-worker": "up"}:
                break
        except (URLError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "Both authenticated Nexora scrape targets must be healthy"
            )
        time.sleep(1)

    rules = evaluated_rules(time.monotonic() + 90)
    recording, alerting = declared_rules()
    unhealthy = {
        rule["name"]: rule["health"] for rule in rules if rule["health"] != "ok"
    }
    assert not unhealthy, f"Rules are loaded but not evaluating cleanly: {unhealthy}"
    assert {rule["name"] for rule in rules} == recording | alerting
    assert {rule["name"] for rule in rules if rule["type"] == "alerting"} == alerting

    dashboard_url = "http://127.0.0.1:3001/api/dashboards/uid/nexora-operations"
    try:
        read_json(dashboard_url)
    except HTTPError as error:
        assert error.code == 401
    else:
        raise AssertionError("The operations dashboard must not allow anonymous access")
    credential = "admin:" + os.environ["NEXORA_GRAFANA_ADMIN_PASSWORD"]
    authorization = "Basic " + base64.b64encode(credential.encode()).decode()
    dashboard = read_json(dashboard_url, authorization)["dashboard"]
    assert dashboard["uid"] == "nexora-operations"
    assert len(dashboard["panels"]) == 10
    datasource = read_json(
        "http://127.0.0.1:3001/api/datasources/uid/nexora-prometheus", authorization
    )
    assert datasource["url"] == "http://prometheus:9090"
    print(
        "Monitoring smoke passed: authenticated scrapes, rules and protected dashboard."
    )


if __name__ == "__main__":
    main()
