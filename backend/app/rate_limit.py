import math
import threading
import time
from collections import defaultdict, deque
from collections.abc import Hashable

from fastapi import HTTPException, status


class UserRateLimiter:
    def __init__(
        self,
        limit: int = 60,
        window_seconds: int = 60,
        detail: str = "You've reached the writing assistant limit. Please try again shortly.",
    ):
        self.limit = limit
        self.window_seconds = window_seconds
        self.detail = detail
        self._requests: dict[Hashable, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, user_id: Hashable) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            for existing_user_id in list(self._requests):
                requests = self._requests[existing_user_id]
                while requests and requests[0] <= cutoff:
                    requests.popleft()
                if not requests:
                    del self._requests[existing_user_id]

            requests = self._requests[user_id]
            if len(requests) >= self.limit:
                retry_after = max(1, math.ceil(requests[0] + self.window_seconds - now))
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=self.detail,
                    headers={"Retry-After": str(retry_after)},
                )
            requests.append(now)


user_rate_limiter = UserRateLimiter()
auth_rate_limiter = UserRateLimiter(
    limit=10,
    window_seconds=60,
    detail="Too many sign-in attempts. Please wait a minute and try again.",
)
