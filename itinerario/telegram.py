import io
import re

import httpx

from .providers import ProviderError, request

MAX_ATTACHMENT = 8 * 1024 * 1024


class Telegram:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self.client = client
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"

    async def call(self, method: str, payload: dict):
        response = await request(self.client, "telegram", method, "POST", f"{self.base}/{method}",
                                 json=payload, timeout=38)
        result = response.json()
        if not result.get("ok"):
            raise ProviderError("telegram", method, result.get("error_code"))
        return result.get("result")

    async def whoami(self):
        return await self.call("getMe", {})

    async def updates(self, offset: int | None):
        payload = {"timeout": 27, "allowed_updates": ["message"], "limit": 50}
        if offset is not None:
            payload["offset"] = offset
        return await self.call("getUpdates", payload)

    async def send(self, owner: int, text: str, *, html: bool = False):
        payload = {"chat_id": owner, "text": text, "link_preview_options": {"is_disabled": True}}
        if html:
            payload["parse_mode"] = "HTML"
        return await self.call("sendMessage", payload)

    async def document(self, owner: int, data: bytes, filename: str, caption: str):
        response = await request(
            self.client, "telegram", "sendDocument", "POST", f"{self.base}/sendDocument",
            data={"chat_id": str(owner), "caption": caption},
            files={"document": (filename, io.BytesIO(data), "application/pdf")}, timeout=70,
        )
        if not response.json().get("ok"):
            raise ProviderError("telegram", "sendDocument")

    async def attachment(self, document: dict) -> bytes:
        if document.get("mime_type") not in {"application/pdf", "application/octet-stream", None}:
            raise ValueError("Podés adjuntar un PDF con texto seleccionable de hasta 8 MB.")
        if document.get("file_size", 0) > MAX_ATTACHMENT or not document.get("file_name", "").lower().endswith(".pdf"):
            raise ValueError("Podés adjuntar un PDF con texto seleccionable de hasta 8 MB.")
        metadata = await self.call("getFile", {"file_id": document["file_id"]})
        path = metadata.get("file_path", "")
        if not re.fullmatch(r"(?:[a-zA-Z0-9_-]+/)*[a-zA-Z0-9_.-]+", path):
            raise ProviderError("telegram", "archivo")
        if any(part in {".", ".."} for part in path.split("/")):
            raise ProviderError("telegram", "archivo")
        try:
            async with self.client.stream("GET", f"https://api.telegram.org/file/bot{self.token}/{path}") as response:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_ATTACHMENT:
                        raise ValueError("El PDF supera el límite de 8 MB.")
                    chunks.extend(chunk)
        except httpx.HTTPError:
            raise ProviderError("telegram", "descarga") from None
        if not chunks.startswith(b"%PDF-"):
            raise ValueError("El archivo no parece un PDF válido.")
        return bytes(chunks)
