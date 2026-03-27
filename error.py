class MCPConnectionError(Exception):
    def __init__(self, url, reason, original_error=None):
        self.url = url
        self.reason = reason
        self.original_error = original_error
        super().__init__(f"MCP connection failed for '{url}': {reason}")

def validate_mcp_url(url):
    if not url:
        raise MCPConnectionError(url or "<empty>", "MCP server URL is empty or missing.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise MCPConnectionError(url, f"Invalid URL scheme '{parsed.scheme or ''}'. Must be http:// or https://.")
    if not parsed.hostname:
        raise MCPConnectionError(url, "MCP server URL is missing a hostname.")

def _classify_mcp_connection_error(exc, url):
    msg = str(exc).lower()
    if isinstance(exc, httpx.ConnectError):
        if "name or service not known" in msg or "getaddrinfo failed" in msg:
            return MCPConnectionError(url, "DNS resolution failed — hostname not found. Check for typos.", exc)
        if "connection refused" in msg:
            return MCPConnectionError(url, "Connection refused — server may not be running or port is wrong.", exc)
    if isinstance(exc, httpx.ConnectTimeout):
        return MCPConnectionError(url, "Connection timed out — server did not respond.", exc)
    return MCPConnectionError(url, f"{type(exc).__name__}: {exc}", exc)

class MCPAwareAsyncClient(httpx.AsyncClient):
    async def send(self, request, **kwargs):
        try:
            return await super().send(request, **kwargs)
        except MCPConnectionError:
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.InvalidURL, httpx.TimeoutException) as exc:
            raise _classify_mcp_connection_error(exc, str(request.url)) from exc