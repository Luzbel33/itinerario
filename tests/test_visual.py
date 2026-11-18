import base64
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image

from itinerario.visual import _download, _frames, read_media


class VisualTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_untrusted_redirect_and_size(self):
        requests = []

        def handle(req):
            requests.append(req.url.host)
            if req.url.path == "/big":
                return httpx.Response(200, content=b"x" * 32)
            return httpx.Response(302, headers={"Location": "https://localhost/image"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "image"
                for url in (
                    "https://cdninstagram.com.example.org/file", "https://cdninstagram.com:444/file",
                    "https://somebody@cdninstagram.com/file", "http://scontent.cdninstagram.com/file",
                    "https://scontent.cdninstagram.com/redirect", "https://scontent.cdninstagram.com/big",
                ):
                    self.assertFalse(await _download(client, url, target, 16))
                self.assertEqual(requests, ["scontent.cdninstagram.com"] * 2)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe are required")
    async def test_real_video_samples_different_times(self):
        with tempfile.TemporaryDirectory() as directory:
            clip = Path(directory) / "clip.mp4"
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=240x160:r=12:d=3",
                "-threads", "1", "-c:v", "mpeg4", str(clip),
            ], check=True, timeout=10)
            frames = await _frames(clip, video=True)
            self.assertEqual(len(frames), 4)
            for frame in frames:
                with Image.open(io.BytesIO(frame)) as image:
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.size, (240, 160))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
    async def test_reads_only_inline_images_and_identifies_cover_fallback(self):
        source = io.BytesIO()
        Image.new("RGB", (640, 400), color="red").save(source, "PNG")
        calls = []

        def handle(req):
            if req.url.host == "p-w.insnoop.com":
                return httpx.Response(200, content=source.getvalue())
            if req.url.host == "scontent.cdninstagram.com":
                return httpx.Response(403)
            self.assertEqual(req.url.host, "api.openai.com")
            self.assertNotIn(b"signed-url-value", req.content)
            payload = json.loads(req.content)
            self.assertFalse(payload["store"])
            blocks = payload["input"][0]["content"]
            data = blocks[1]["image_url"].removeprefix("data:image/jpeg;base64,")
            with Image.open(io.BytesIO(base64.b64decode(data))) as image:
                self.assertEqual(image.size, (640, 400))
            calls.append(req.url.host)
            return httpx.Response(200, json={
                "status": "completed", "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": "La imagen muestra una convocatoria."},
                ]}],
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await read_media(
                client, "test-key", "test-model", image_url="https://p-w.insnoop.com/image?s=signed-url-value",
                video_url="https://scontent.cdninstagram.com/expired", objective="Busco una vacante.",
            )
        self.assertIn("Solo la portada", result)
        self.assertIn("convocatoria", result)
        self.assertEqual(calls, ["api.openai.com"])
