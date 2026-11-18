import io
import sys

from pypdf import PdfReader


def main() -> int:
    if sys.platform == "linux":
        import resource

        # Compressed page streams are untrusted and can exceed the upload size by orders of magnitude.
        limit = 256 * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    reader = PdfReader(io.BytesIO(sys.stdin.buffer.read(8 * 1024 * 1024 + 1)))
    if reader.is_encrypted:
        return 1
    remaining = 10_000
    pieces = []
    for page in reader.pages[:12]:
        text = (page.extract_text() or "").strip()[:remaining]
        if text:
            pieces.append(text)
            remaining -= len(text)
        if remaining <= 0:
            break
    sys.stdout.buffer.write("\n\n".join(pieces).encode("utf-8"))
    return 0 if pieces else 1


if __name__ == "__main__":
    raise SystemExit(main())
