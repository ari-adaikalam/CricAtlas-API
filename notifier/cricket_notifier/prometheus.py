"""Reading recorded series from Prometheus as raw samples: one HTTP call that returns every sample in a range, with its timestamp.

A range selector (`name[6h]` at a time) returns the stored samples themselves, not values evaluated at steps, so a gap shows as a gap. The read is bounded: a response over its size limit or a series list over
its sample limit is refused, and a failure to reach Prometheus is not a quiet zero but `PrometheusUnavailable`, which the summary reports as no data.
"""
import json
import math
import urllib.error
import urllib.parse
import urllib.request

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class PrometheusUnavailable(RuntimeError):
    """Prometheus could not be reached or did not answer as expected."""


class TooManySamples(RuntimeError):
    """A read would hold more samples than the limit."""


class PrometheusClient:
    def __init__(self, base_url: str, timeout: float = 20.0, max_samples: int = 200_000):
        self.base, self.timeout, self.max_samples = base_url.rstrip("/"), timeout, max_samples

    def matrix(self, selector: str, end: float, range_seconds: float) -> list[dict]:
        """The raw samples of every series matching `selector` in (end - range, end]: [{"labels": {...}, "values": [(time, value), ...]}]. The selector is one of the fixed names in rules.py."""
        query = f"{selector}[{int(range_seconds)}s]"
        url = f"{self.base}/api/v1/query?{urllib.parse.urlencode({'query': query, 'time': f'{end:.3f}'})}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError):
            raise PrometheusUnavailable("Prometheus could not be reached") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TooManySamples("the response is over the size limit")
        try:
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict) or data.get("status") != "success" or data["data"]["resultType"] != "matrix":
                raise ValueError
            result = data["data"]["result"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise PrometheusUnavailable("Prometheus answered in a form that was not expected") from None
        out, count = [], 0
        for series in result:
            values = []
            for t, v in series.get("values", []):
                number = float(v)
                if math.isfinite(number):
                    values.append((float(t), number))
            count += len(values)
            if count > self.max_samples:
                raise TooManySamples(f"more than {self.max_samples} samples")
            if values:
                out.append({"labels": {str(k): str(x) for k, x in series.get("metric", {}).items()}, "values": values})
        return out
