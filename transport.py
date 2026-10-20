"""Client side of the Paxel wire protocol, rebuilt from the captured traffic.

Implements the seven endpoints the real 0.3.83.2 client uses, in the same order:

    GET  {proxy}/health                                  -> signatures_loaded
    POST {proxy}/validate                                -> 200 / reason
    POST {proxy}/api/v1/proxy/preflight                  -> contract_version
    POST {results_origin}/api/v1/scoring_sessions        -> {id, manifest_committed}
    POST {proxy}/v1/messages                             -> Anthropic message (+ nonce)
    POST {results_origin}/api/v1/scoring_sessions/{id}/manifest
    POST {results}/api/v1/results                        -> {slug, results_url}

Nothing here is Paxel code; it is an independent implementation of the observed
contract. See contracts/paxel-live-contracts.md for the field-level evidence.
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

NONCE_RE = re.compile(r"<!--\s*yc_verify:([^:>\s]+):([^\s>]+)\s*-->")


class TransportError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


def _origin(url: str) -> str:
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.netloc.split(':')[0]}{port}".rstrip("/")


@dataclass
class Transport:
    proxy_url: str
    results_url: str
    token: str
    client_version: str = "local-0.1"
    timeout: int = 60
    log: list[dict] = field(default_factory=list)
    nonces: list[str] = field(default_factory=list)
    scoring_session_id: str | None = None

    # ---- low level -----------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                payload = res.read()
                status = res.status
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
        except urllib.error.URLError as exc:
            raise TransportError(f"{method} {url} failed: {exc}") from exc
        self.log.append(
            {"method": method, "url": url, "status": status, "bytes": len(body or b"")}
        )
        return status, payload

    @staticmethod
    def _json(payload: bytes) -> Any:
        if not payload:
            return {}
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def _post_json(self, url: str, obj: dict, token_header: str) -> tuple[int, Any]:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        status, payload = self._request(
            "POST",
            url,
            body,
            {
                token_header: self.token,
                "content-type": "application/json",
                "x-paxel-client-release": self.client_version,
            },
        )
        return status, self._json(payload)

    # ---- the seven endpoints -------------------------------------------
    def health(self) -> dict:
        status, payload = self._request("GET", f"{self.proxy_url.rstrip('/')}/health")
        data = self._json(payload)
        if status != 200 or not data.get("signatures_loaded"):
            raise TransportError(
                f"proxy health failed (HTTP {status}, signatures_loaded={data.get('signatures_loaded')})",
                status,
                payload[:200].decode("utf-8", "replace"),
            )
        return data

    def validate(self) -> dict:
        url = f"{self.proxy_url.rstrip('/')}/validate"
        status, payload = self._request(
            "POST",
            url,
            b"",
            {"x-api-key": self.token, "x-paxel-client-release": self.client_version},
        )
        data = self._json(payload)
        if status != 200:
            raise TransportError(
                f"token rejected: {data.get('reason') or status}", status, payload[:200].decode("utf-8", "replace")
            )
        return data

    def preflight(self, prompt_ids: list[str]) -> dict:
        url = f"{self.proxy_url.rstrip('/')}/api/v1/proxy/preflight"
        status, data = self._post_json(
            url, {"client_prompt_ids": prompt_ids, "client_version": self.client_version}, "X-YC-Token"
        )
        if status == 403 and data.get("code") == "client_out_of_date":
            raise TransportError(
                "prompt contract mismatch: server expects "
                f"{data.get('expected_contract_version')}, missing {data.get('missing_signatures')}",
                status,
            )
        if status == 404:
            return {"contract_version": None, "skipped": True}
        if status != 200:
            raise TransportError(f"preflight failed (HTTP {status})", status)
        return data

    def open_scoring_session(self, git_remote: str | None, since_epoch: str | None = None) -> str:
        origin = _origin(self.results_url)
        status, data = self._post_json(
            f"{origin}/api/v1/scoring_sessions",
            {"git_remote": git_remote, "since_epoch": since_epoch, "client_nonce": uuid.uuid4().hex},
            "X-YC-Token",
        )
        if status == 451 or (isinstance(data, dict) and data.get("code") == "region_blocked"):
            raise TransportError("region blocked", status)
        if status == 429:
            raise TransportError(f"re-score budget exceeded: {data.get('error')}", status)
        if status not in (200, 201):
            raise TransportError(f"scoring session failed (HTTP {status}): {data.get('error')}", status)
        session_id = (data or {}).get("id")
        if not session_id:
            raise TransportError("server returned no scoring session id", status)
        self.scoring_session_id = session_id
        return session_id

    def commit_manifest(self, episode_manifest: dict[str, list[str]]) -> None:
        if not self.scoring_session_id or not episode_manifest:
            return
        origin = _origin(self.results_url)
        status, data = self._post_json(
            f"{origin}/api/v1/scoring_sessions/{self.scoring_session_id}/manifest",
            {"episode_manifest": episode_manifest},
            "X-YC-Token",
        )
        if status != 200:
            raise TransportError(f"manifest commit failed (HTTP {status}): {data.get('error')}", status)

    def messages(
        self,
        prompt_id: str,
        user_content: str,
        model: str,
        max_tokens: int = 1200,
        session_id: str | None = None,
        system: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system:
            payload["system"] = system
        headers = {
            "x-api-key": self.token,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "x-paxel-client-release": self.client_version,
            "x-paxel-prompt-id": prompt_id,
        }
        if session_id:
            headers["x-paxel-session-id"] = session_id
        if self.scoring_session_id:
            headers["x-paxel-scoring-session"] = self.scoring_session_id
        req = urllib.request.Request(
            f"{self.proxy_url.rstrip('/')}/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                status, raw = res.status, res.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        self.log.append({"method": "POST", "url": "v1/messages", "status": status, "bytes": len(raw)})
        if status != 200:
            raise TransportError(f"messages failed (HTTP {status})", status, raw[:300].decode("utf-8", "replace"))
        data = self._json(raw)
        text = "".join(
            block.get("text", "")
            for block in (data.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        for request_id, hmac in NONCE_RE.findall(text):
            nonce = f"{request_id}:{hmac}"
            if nonce not in self.nonces:
                self.nonces.append(nonce)
        return {"text": text, "raw": data, "nonces": self.nonces}

    def upload_results(self, payload: dict) -> dict:
        body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        status, raw = self._request(
            "POST",
            self.results_url,
            body,
            {
                "X-YC-Token": self.token,
                "X-Idempotency-Key": str(uuid.uuid4()),
                "content-type": "application/octet-stream",
                "content-encoding": "gzip",
                "x-paxel-client-release": self.client_version,
            },
        )
        data = self._json(raw)
        if status not in (200, 201):
            raise TransportError(f"upload failed (HTTP {status})", status, raw[:300].decode("utf-8", "replace"))
        return {
            "status": status,
            "slug": data.get("slug"),
            "results_url": data.get("results_url"),
            "idempotent_replay": data.get("idempotent_replay"),
            "compressed_bytes": len(body),
            "payload_bytes": len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        }

    @property
    def calls(self) -> list[dict]:
        return self.log
