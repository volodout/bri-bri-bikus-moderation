"""HTTP client for B2B product snapshots."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class B2BClientError(RuntimeError):
    pass


class B2BClient:
    def __init__(self, base_url: str, service_key: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.service_key = service_key
        self.timeout = timeout

    def fetch_product(self, product_id: str) -> dict[str, Any]:
        if not self.base_url:
            raise B2BClientError("B2B_BASE_URL is not configured")

        product_path = quote(product_id, safe="")
        request = Request(
            f"{self.base_url}/api/v1/products/{product_path}",
            headers={"X-Service-Key": self.service_key, "Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status >= 400:
                    raise B2BClientError(f"B2B returned HTTP {response.status}")
                payload = response.read().decode("utf-8")
        except HTTPError as error:
            raise B2BClientError(f"B2B returned HTTP {error.code}") from error
        except URLError as error:
            raise B2BClientError(f"B2B request failed: {error.reason}") from error

        try:
            product = json.loads(payload)
        except json.JSONDecodeError as error:
            raise B2BClientError("B2B returned invalid JSON") from error
        if not isinstance(product, dict):
            raise B2BClientError("B2B returned non-object product payload")
        return product

