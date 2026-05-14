"""한국투자증권 OpenAPI client — minimal, stdlib only."""

import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Optional

BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE_PATH = Path("data/.kis_token.json")

# Index codes (FID_INPUT_ISCD when FID_COND_MRKT_DIV_CODE=U)
KOSPI = "0001"
KOSDAQ = "1001"
KOSPI200 = "2001"


class KISError(Exception):
    pass


class KISClient:
    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        account: Optional[str] = None,
    ):
        self.app_key = app_key or os.environ["KIS_APP_KEY"]
        self.app_secret = app_secret or os.environ["KIS_APP_SECRET"]
        self.account = account or os.environ.get("KIS_ACCOUNT", "")
        self._token: Optional[str] = None
        self._expires_at: int = 0

    # ----- HTTP -----
    def _request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict] = None,
        params: Optional[Dict] = None,
        body: Optional[Dict] = None,
    ) -> Dict:
        url = f"{BASE_URL}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ----- Token -----
    def get_token(self) -> str:
        if self._token and self._expires_at > time.time() + 300:
            return self._token
        # Try cache file
        if TOKEN_CACHE_PATH.exists():
            try:
                cached = json.loads(TOKEN_CACHE_PATH.read_text())
                if cached.get("expires_at", 0) > time.time() + 300:
                    self._token = cached["access_token"]
                    self._expires_at = cached["expires_at"]
                    return self._token
            except Exception:
                pass
        # Issue new token
        body = {
            "grant_type": "client_credentials",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
        }
        resp = self._request(
            "POST", "/oauth2/tokenP",
            headers={"Content-Type": "application/json"},
            body=body,
        )
        if "access_token" not in resp:
            raise KISError(f"Token issuance failed: {resp}")
        self._token = resp["access_token"]
        self._expires_at = int(time.time()) + int(resp.get("expires_in", 86400))
        try:
            TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_CACHE_PATH.write_text(json.dumps({
                "access_token": self._token,
                "expires_at": self._expires_at,
            }))
        except Exception as e:
            print(f"[kis] token cache write failed (non-fatal): {e}")
        return self._token

    def _data_headers(self, tr_id: str) -> Dict[str, str]:
        return {
            "authorization": f"Bearer {self.get_token()}",
            "appkey": self.app_key,
            "appsecret": self.app_secret,
            "tr_id": tr_id,
            "custtype": "P",  # 개인
        }

    # ----- Market Data -----
    def get_index_price(self, code: str) -> Dict:
        """Index current price + today's open/high/low + prev-day diff.

        code: '0001' (KOSPI), '1001' (KOSDAQ), '2001' (KOSPI200).
        Returns dict 'output' from KIS response.
        """
        resp = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            headers=self._data_headers("FHPUP02100000"),
            params={"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
        )
        if resp.get("rt_cd") != "0":
            raise KISError(f"Index price API error: {resp}")
        return resp["output"]

    def get_stock_price(self, ticker6: str) -> Dict:
        """Stock current price snapshot. ticker6 is 6-digit code (e.g., '005930')."""
        resp = self._request(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            headers=self._data_headers("FHKST01010100"),
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker6},
        )
        if resp.get("rt_cd") != "0":
            raise KISError(f"Stock price API error: {resp}")
        return resp["output"]
