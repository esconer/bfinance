"""
Downloader utility for financial documents (Concall MP3s, transcripts, annual reports, credit ratings).
"""

from pathlib import Path
from typing import Optional
import httpx


class DocumentDownloader:
    """
    Direct downloader for audio files and PDF filings with connection pooling and chunked streaming.
    """

    DEFAULT_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    }

    @classmethod
    def download_file(
        cls,
        url: str,
        dest_path: str,
        timeout: float = 60.0,
        proxy: Optional[str] = None,
    ) -> str:
        """
        Download a remote file (PDF, MP3, PPT) to local disk.

        Args:
            url: Direct HTTP/HTTPS download URL.
            dest_path: Local target filepath or directory.
            timeout: Maximum download timeout in seconds.
            proxy: Optional proxy string.

        Returns:
            str: Resolved absolute path of the downloaded file.
        """
        if not url:
            raise ValueError("Download URL cannot be empty.")

        target = Path(dest_path)
        if target.is_dir() or dest_path.endswith(("\\", "/")):
            filename = url.split("?")[0].rstrip("/").split("/")[-1] or "downloaded_file"
            target = target / filename

        target.parent.mkdir(parents=True, exist_ok=True)

        client_kwargs = {
            "headers": cls.DEFAULT_HEADERS,
            "timeout": timeout,
            "follow_redirects": True,
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        with httpx.Client(**client_kwargs) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                with open(target, "wb") as f:
                    for chunk in response.iter_bytes(chunk_size=65536):
                        f.write(chunk)

        return str(target.resolve())
