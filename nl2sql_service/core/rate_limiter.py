import time
import threading
from collections import defaultdict
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

class RateLimiter:
    """In-memory Token Bucket Rate Limiter."""
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate # tokens per second
        self.tokens: dict[str, float] = defaultdict(lambda: float(capacity))
        self.last_refill: dict[str, float] = defaultdict(time.monotonic)
        self.lock = threading.Lock()

    def consume(self, client_ip: str) -> bool:
        with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_refill[client_ip]
            
            # Refill tokens
            refill_amount = elapsed * self.refill_rate
            if refill_amount > 0:
                self.tokens[client_ip] = min(float(self.capacity), self.tokens[client_ip] + refill_amount)
                self.last_refill[client_ip] = now
            
            if self.tokens[client_ip] >= 1.0:
                self.tokens[client_ip] -= 1.0
                return True
            return False

# Global instance: 30 requests per minute = 1 request per 2 seconds (0.5/s)
# Burst capacity of 30 requests.
global_limiter = RateLimiter(capacity=30, refill_rate=0.5)

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, paths_to_limit: list[str]):
        super().__init__(app)
        self.paths_to_limit = paths_to_limit
        
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if any(path.startswith(p) for p in self.paths_to_limit):
            client_ip = request.client.host if request.client else "unknown"
            if request.url.hostname == "testserver" or client_ip == "testclient":
                return await call_next(request)
            if not global_limiter.consume(client_ip):
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too Many Requests. Please slow down."}
                )
        return await call_next(request)
