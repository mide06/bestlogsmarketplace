import logging
import os
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class EmonBestLogsAPIError(Exception):
    """Raised when the EmonBestLogs supplier integration fails."""


class EmonBestLogsService:
    def __init__(self, api_key=None, base_url=None):
        self.api_key = api_key or os.getenv("EMON_API_KEY")
        self.base_url = (base_url or os.getenv("EMON_BASE_URL") or "https://emonbestlog.com/api/v1").rstrip("/")

        if not self.api_key:
            raise EmonBestLogsAPIError("EMON_API_KEY is not configured.")

        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )
        # Configurable idempotency header name (defaults to standard Idempotency-Key)
        self.idempotency_header = os.getenv("EMON_IDEMPOTENCY_HEADER", "Idempotency-Key")

    def _build_url(self, path):
        return urljoin(f"{self.base_url}/", path.lstrip("/"))

    def _request(self, method, path, **kwargs):
        url = self._build_url(path)
        timeout = kwargs.pop("timeout", 10)
        logger.info("EmonBestLogs request: method=%s url=%s params=%s json=%s", method.upper(), url, kwargs.get("params"), kwargs.get("json"))
        try:
            response = self.session.request(method=method.upper(), url=url, timeout=timeout, **kwargs)
            logger.info("EmonBestLogs response status: %s for %s", response.status_code, url)
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                payload = {"raw": response.text}

            logger.info("EmonBestLogs response JSON for %s: %s", url, payload)
            if isinstance(payload, dict) and payload.get("error"):
                logger.error("EmonBestLogs API error for %s: %s", path, payload)
                raise EmonBestLogsAPIError(payload.get("message") or payload.get("error"))
            return payload
        except requests.exceptions.HTTPError as exc:
            status_code = exc.response.status_code if exc.response else 0
            message = self._normalize_error_message(exc.response)
            logger.error("EmonBestLogs HTTP error on %s: %s (%s)", path, message, status_code)
            self._raise_for_supplier_status(status_code, message)
        except requests.exceptions.RequestException as exc:
            logger.exception("Network failure while calling EmonBestLogs %s", path)
            raise EmonBestLogsAPIError(f"Network error while contacting EmonBestLogs: {exc}") from exc

    def _normalize_error_message(self, response):
        if not response:
            return "Unknown supplier error"
        try:
            payload = response.json()
        except ValueError:
            return response.text
        return payload.get("message") or payload.get("error") or response.text

    def _raise_for_supplier_status(self, status_code, message):
        if status_code == 401:
            raise EmonBestLogsAPIError("Invalid API key for EmonBestLogs.")
        if status_code == 402:
            raise EmonBestLogsAPIError("Insufficient wallet balance on EmonBestLogs.")
        if status_code == 404:
            raise EmonBestLogsAPIError("Requested product not found on EmonBestLogs.")
        if status_code == 409:
            raise EmonBestLogsAPIError("Requested product is out of stock on EmonBestLogs.")
        if status_code == 429:
            raise EmonBestLogsAPIError("Too many requests to EmonBestLogs. Please retry later.")
        raise EmonBestLogsAPIError(message or f"Unexpected supplier response status: {status_code}")

    def get_categories(self):
        data = self._request("GET", "/categories")
        logger.info("EmonBestLogs /categories response payload: %s", data)
        return data

    def get_products(self, category=None, in_stock=None, page=None):
        params = {}
        if category:
            params["category"] = category
        if in_stock is not None:
            params["in_stock"] = in_stock
        if page:
            params["page"] = page

        data = self._request("GET", "/products", params=params)
        logger.info("EmonBestLogs /products response payload: %s", data)
        return data

    def buy_product(self, product_id, quantity=1, idempotency_key=None):
        payload = {"product": product_id, "quantity": quantity}
        headers = None
        if idempotency_key:
            headers = {self.idempotency_header: str(idempotency_key)}
        return self._request("POST", "/buy", json=payload, headers=headers)

    def get_balance(self):
        return self._request("GET", "/balance")
