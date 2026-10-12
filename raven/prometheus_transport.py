"""Private stdlib HTTP subprocess used by prometheus_source.

Only stdout is the raw response. Stderr contains fixed categorical errors, never
URLs, query text, credentials or exception messages. The parent enforces the
wall-clock deadline, including DNS resolution and TLS setup.
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def main():
    raw = sys.stdin.buffer.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        return fail("INPUT")
    try:
        value = json.loads(raw)
        params = urllib.parse.urlencode({"query": value["query"], "start": value["start"], "end": value["end"],
                                        "step": value["step"], "timeout": f"{value['timeout']:g}s"})
        endpoint = value["endpoint"].rstrip("/") + "/api/v1/query_range?" + params
        headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "Raven-evidence/1"}
        if value["credential"] is not None:
            headers["Authorization"] = "Bearer " + value["credential"]
        context = ssl.create_default_context(cafile=value.get("ca_file"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(endpoint, headers=headers)
        with opener.open(request, timeout=min(2., value["timeout"])) as response:
            if response.status != 200:
                return fail("HTTP-" + str(response.status))
            if response.headers.get("Content-Encoding", "identity").lower() not in ("", "identity"):
                return fail("ENCODING")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdigit() or int(length) > value["max_bytes"]):
                return fail("LENGTH")
            data = bytearray()
            while True:
                chunk = response.read(min(65536, value["max_bytes"] - len(data) + 1))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > value["max_bytes"]:
                    return fail("LIMIT")
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            return 0
    except urllib.error.HTTPError as exc:
        return fail("HTTP-" + str(exc.code))
    except (urllib.error.URLError, OSError):
        return fail("CONNECT")
    except Exception:
        return fail("RESPONSE")


def fail(code):
    sys.stderr.write(code + "\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
