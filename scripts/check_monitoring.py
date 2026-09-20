"""Smoke-check the opt-in local stack without printing credentials or metric data."""

import base64
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def read_json(url, authorization=None):
    headers = {"Authorization": authorization} if authorization else {}
    with urlopen(Request(url, headers=headers), timeout=5) as response:
        return json.load(response)


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

    groups = read_json("http://127.0.0.1:9090/api/v1/rules")["data"]["groups"]
    rules = [rule for group in groups for rule in group["rules"]]
    assert len(rules) == 7 and all(rule["health"] == "ok" for rule in rules)
    assert sum(rule["type"] == "alerting" for rule in rules) == 3

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
