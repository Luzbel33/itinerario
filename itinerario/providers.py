from urllib.parse import urlsplit

import httpx

class ProviderError(RuntimeError):
    def __init__(self, provider: str, operation: str, status: int | None = None):
        self.status = status
        super().__init__(f"{provider}/{operation} ({status if status is not None else 'conexión'})")


async def request(
    client: httpx.AsyncClient, provider: str, operation: str, method: str, url: str, **kwargs
) -> httpx.Response:
    try:
        response = await client.request(method, url, **kwargs)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise ProviderError(provider, operation, error.response.status_code) from None
    except httpx.RequestError:
        raise ProviderError(provider, operation) from None
    return response


def public_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            len(value) <= 2048 and parsed.scheme in {"https", "http"} and bool(parsed.hostname)
            and "." in parsed.hostname and parsed.username is None and parsed.password is None
            and not any(char.isspace() or ord(char) < 32 for char in value)
        )
    except ValueError:
        return False
