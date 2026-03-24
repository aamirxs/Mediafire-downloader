import requests
import re
import os.path as osp
import tempfile
import shutil
import os
from concurrent.futures import ThreadPoolExecutor, Future, as_completed
from threading import Lock, Event, Thread
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from collections import deque
import time
import logging
from enum import Enum
import struct
import io

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('downloader.log'),
        logging.StreamHandler()
    ]
)


class DownloadStatus(Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class DownloadInfo:
    filename: str
    total_size: int
    downloaded: int
    status: DownloadStatus
    speed: float
    progress: float
    eta: float = 0.0
    error: Optional[str] = None
    pause_event: Optional[Event] = None
    cancel_event: Optional[Event] = None
    future: Optional[Future] = None
    temp_file: Optional[str] = None
    _speed_samples: deque = field(default_factory=lambda: deque(maxlen=20))
    _last_sample_time: float = 0.0
    _last_sample_bytes: int = 0

    def to_dict(self):
        return {
            'filename': self.filename,
            'total_size': self.total_size,
            'downloaded': self.downloaded,
            'status': self.status.value,
            'speed': self.speed,
            'progress': self.progress,
            'eta': self.eta,
            'error': self.error
        }


class DownloadError(Exception):
    def __init__(self, message: str, error_type: str, is_recoverable: bool = True):
        super().__init__(message)
        self.error_type = error_type
        self.is_recoverable = is_recoverable


class MediaFireDownloader:
    # Chunk size for reading from network (8 MB)
    STREAM_CHUNK_SIZE = 8 * 1024 * 1024
    # Number of parallel chunks per file for large files
    CHUNKS_PER_FILE = 8
    # Minimum file size (in bytes) to use chunked parallel download (10 MB)
    MIN_SIZE_FOR_CHUNKED = 10 * 1024 * 1024
    # Speed sampling interval (seconds)
    SPEED_SAMPLE_INTERVAL = 0.5

    def __init__(self, max_workers=5, max_retries=3):
        self.max_workers = max_workers
        self.max_retries = max_retries
        # Main executor for file-level parallelism
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        # Dedicated pool for chunk-level parallelism within each file
        self.chunk_executor = ThreadPoolExecutor(max_workers=max_workers * self.CHUNKS_PER_FILE)
        self.downloads: Dict[str, DownloadInfo] = {}
        self.lock = Lock()
        self.logger = logging.getLogger(__name__)
        # Shared session with connection pooling & keep-alive
        self.session = self._create_session()

    def _create_session(self) -> requests.Session:
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
        # Enable connection pooling with higher limits
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=40,
            max_retries=0  # We handle retries ourselves
        )
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        return sess

    def extract_download_link(self, contents: str) -> str:
        try:
            for line in contents.splitlines():
                m = re.search(r'href="((http|https)://download[^"]+)', line)
                if m:
                    return m.groups()[0]
            raise DownloadError("Download link not found", "LINK_EXTRACTION_ERROR")
        except DownloadError:
            raise
        except Exception as e:
            self.logger.error(f"Error extracting download link: {str(e)}")
            raise DownloadError(f"Failed to extract download link: {str(e)}", "LINK_EXTRACTION_ERROR")

    def _resolve_download_url(self, url: str) -> Tuple[str, str, int]:
        """Resolves a MediaFire page URL to the actual direct download URL.
        Returns (direct_url, filename, total_size)."""
        sess = self._create_session()
        while True:
            res = sess.get(url, stream=True, timeout=30)
            if 'Content-Disposition' in res.headers:
                break
            url = self.extract_download_link(res.text)
            if url is None:
                raise DownloadError("Permission denied or invalid link", "PERMISSION_ERROR", False)

        m = re.search('filename="(.*)"', res.headers['Content-Disposition'])
        if not m:
            raise DownloadError("Cannot extract filename", "FILENAME_ERROR")

        filename = m.groups()[0].encode('iso8859').decode('utf-8')
        total_size = int(res.headers.get('Content-Length', 0))
        res.close()

        return url, filename, total_size

    def _supports_range(self, url: str) -> bool:
        """Check if the server supports Range requests."""
        try:
            resp = self.session.head(url, timeout=10)
            return resp.headers.get('Accept-Ranges', 'none').lower() != 'none'
        except Exception:
            return False

    def _update_speed(self, download_id: str, downloaded: int):
        """Update download speed using a rolling window average."""
        info = self.downloads[download_id]
        now = time.monotonic()

        if info._last_sample_time == 0:
            info._last_sample_time = now
            info._last_sample_bytes = downloaded
            return

        elapsed = now - info._last_sample_time
        if elapsed >= self.SPEED_SAMPLE_INTERVAL:
            bytes_delta = downloaded - info._last_sample_bytes
            speed = bytes_delta / elapsed if elapsed > 0 else 0
            info._speed_samples.append(speed)
            info._last_sample_time = now
            info._last_sample_bytes = downloaded

            # Rolling average speed
            if info._speed_samples:
                info.speed = sum(info._speed_samples) / len(info._speed_samples)

            # ETA calculation
            remaining = info.total_size - downloaded
            if info.speed > 0:
                info.eta = remaining / info.speed
            else:
                info.eta = 0

    def _download_chunk(self, url: str, start: int, end: int, download_id: str,
                        chunk_index: int, temp_dir: str) -> str:
        """Download a specific byte range of a file. Returns path to chunk file."""
        chunk_path = osp.join(temp_dir, f"chunk_{chunk_index}")

        headers = {"Range": f"bytes={start}-{end}"}
        # Use a fresh session for each chunk to maximize parallelism
        sess = self._create_session()

        resp = sess.get(url, headers=headers, stream=True, timeout=60)
        resp.raise_for_status()

        bytes_written = 0
        with open(chunk_path, 'wb') as f:
            for data in resp.iter_content(chunk_size=self.STREAM_CHUNK_SIZE):
                # Check cancel
                if self.downloads[download_id].cancel_event.is_set():
                    resp.close()
                    return chunk_path

                # Check pause — block until unpaused
                pause_evt = self.downloads[download_id].pause_event
                while pause_evt.is_set():
                    if self.downloads[download_id].cancel_event.is_set():
                        resp.close()
                        return chunk_path
                    time.sleep(0.1)

                if data:
                    f.write(data)
                    bytes_written += len(data)

                    with self.lock:
                        self.downloads[download_id].downloaded += len(data)
                        downloaded = self.downloads[download_id].downloaded
                        total = self.downloads[download_id].total_size
                        self.downloads[download_id].progress = (downloaded / total * 100) if total > 0 else 0
                        self._update_speed(download_id, downloaded)

        resp.close()
        return chunk_path

    def _download_chunked(self, url: str, output_dir: str, download_id: str,
                          filename: str, total_size: int) -> str:
        """Download a file using parallel chunks for maximum speed."""
        output_path = osp.join(output_dir, filename)
        temp_dir = tempfile.mkdtemp(prefix=f"mfdl_{download_id}_", dir=output_dir)

        try:
            with self.lock:
                self.downloads[download_id].temp_file = temp_dir

            num_chunks = min(self.CHUNKS_PER_FILE, max(1, total_size // (2 * 1024 * 1024)))
            chunk_size = total_size // num_chunks
            ranges = []
            for i in range(num_chunks):
                start = i * chunk_size
                end = (start + chunk_size - 1) if i < num_chunks - 1 else total_size - 1
                ranges.append((start, end, i))

            # Submit all chunk downloads in parallel
            futures = {}
            for start, end, idx in ranges:
                fut = self.chunk_executor.submit(
                    self._download_chunk, url, start, end, download_id, idx, temp_dir
                )
                futures[fut] = idx

            # Wait for all chunks
            for fut in as_completed(futures):
                if self.downloads[download_id].cancel_event.is_set():
                    # Cancel remaining futures
                    for f in futures:
                        f.cancel()
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    with self.lock:
                        self.downloads[download_id].status = DownloadStatus.CANCELLED
                    return download_id

                try:
                    fut.result(timeout=600)
                except Exception as e:
                    self.logger.error(f"Chunk {futures[fut]} failed: {e}")
                    raise DownloadError(f"Chunk download failed: {e}", "CHUNK_ERROR")

            # Merge chunks into final file
            with open(output_path, 'wb') as outfile:
                for i in range(num_chunks):
                    chunk_path = osp.join(temp_dir, f"chunk_{i}")
                    with open(chunk_path, 'rb') as cf:
                        shutil.copyfileobj(cf, outfile, length=16 * 1024 * 1024)

            # Cleanup temp dir
            shutil.rmtree(temp_dir, ignore_errors=True)

            with self.lock:
                self.downloads[download_id].status = DownloadStatus.COMPLETED
                self.downloads[download_id].progress = 100.0
                self.downloads[download_id].temp_file = None

            return download_id

        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def _download_single_stream(self, url: str, output_dir: str, download_id: str,
                                filename: str, total_size: int) -> str:
        """Download a file using a single stream (for small files or servers not supporting Range)."""
        output_path = osp.join(output_dir, filename)
        tmp_file = tempfile.mktemp(suffix=".tmp", prefix=filename + "_", dir=output_dir)

        with self.lock:
            self.downloads[download_id].temp_file = tmp_file

        sess = self._create_session()
        res = sess.get(url, stream=True, timeout=60)
        res.raise_for_status()

        with open(tmp_file, 'wb') as f:
            for chunk in res.iter_content(chunk_size=self.STREAM_CHUNK_SIZE):
                # Check cancel
                if self.downloads[download_id].cancel_event.is_set():
                    res.close()
                    try:
                        os.remove(tmp_file)
                    except OSError:
                        pass
                    with self.lock:
                        self.downloads[download_id].status = DownloadStatus.CANCELLED
                    return download_id

                # Check pause
                while self.downloads[download_id].pause_event.is_set():
                    if self.downloads[download_id].cancel_event.is_set():
                        res.close()
                        try:
                            os.remove(tmp_file)
                        except OSError:
                            pass
                        with self.lock:
                            self.downloads[download_id].status = DownloadStatus.CANCELLED
                        return download_id
                    time.sleep(0.1)

                if chunk:
                    f.write(chunk)
                    with self.lock:
                        self.downloads[download_id].downloaded += len(chunk)
                        downloaded = self.downloads[download_id].downloaded
                        self.downloads[download_id].progress = (downloaded / total_size * 100) if total_size > 0 else 0
                        self._update_speed(download_id, downloaded)

        res.close()
        shutil.move(tmp_file, output_path)

        with self.lock:
            self.downloads[download_id].status = DownloadStatus.COMPLETED
            self.downloads[download_id].progress = 100.0
            self.downloads[download_id].temp_file = None

        return download_id

    def download_file(self, url: str, output_dir: str, download_id: str) -> str:
        """Main download entry point with retry logic."""
        retry_count = 0
        while retry_count < self.max_retries:
            try:
                return self._attempt_download(url, output_dir, download_id)
            except DownloadError as e:
                if not e.is_recoverable or retry_count >= self.max_retries - 1:
                    with self.lock:
                        self.downloads[download_id].status = DownloadStatus.FAILED
                        self.downloads[download_id].error = str(e)
                    raise
                retry_count += 1
                self.logger.warning(f"Retry {retry_count}/{self.max_retries} for {download_id}: {e}")
                # Reset downloaded bytes for retry
                with self.lock:
                    self.downloads[download_id].downloaded = 0
                    self.downloads[download_id].progress = 0
                    self.downloads[download_id]._speed_samples.clear()
                    self.downloads[download_id]._last_sample_time = 0
                    self.downloads[download_id]._last_sample_bytes = 0
                time.sleep(min(2 ** retry_count, 10))  # Exponential backoff, max 10s

    def _attempt_download(self, url: str, output_dir: str, download_id: str) -> str:
        """Single download attempt — resolves URL, picks strategy, and downloads."""
        try:
            # Resolve MediaFire URL to direct download link
            direct_url, filename, total_size = self._resolve_download_url(url)

            with self.lock:
                self.downloads[download_id].filename = filename
                self.downloads[download_id].total_size = total_size
                self.downloads[download_id].status = DownloadStatus.DOWNLOADING

            # Choose download strategy
            use_chunked = (
                total_size >= self.MIN_SIZE_FOR_CHUNKED
                and self._supports_range(direct_url)
            )

            self.logger.info(
                f"Downloading {filename} ({total_size / 1024 / 1024:.1f} MB) "
                f"{'chunked' if use_chunked else 'single-stream'}"
            )

            if use_chunked:
                return self._download_chunked(direct_url, output_dir, download_id, filename, total_size)
            else:
                return self._download_single_stream(direct_url, output_dir, download_id, filename, total_size)

        except DownloadError:
            raise
        except requests.exceptions.RequestException as e:
            raise DownloadError(f"Network error: {str(e)}", "NETWORK_ERROR")
        except OSError as e:
            raise DownloadError(f"File system error: {str(e)}", "FILESYSTEM_ERROR")
        except Exception as e:
            raise DownloadError(f"Unexpected error: {str(e)}", "UNKNOWN_ERROR", False)

    def start_download(self, url: str, output_dir: str) -> str:
        download_id = f"dl_{int(time.time() * 1000)}_{id(url) % 10000}"

        with self.lock:
            self.downloads[download_id] = DownloadInfo(
                filename="Resolving link...",
                total_size=0,
                downloaded=0,
                status=DownloadStatus.QUEUED,
                speed=0,
                progress=0,
                eta=0,
                pause_event=Event(),
                cancel_event=Event(),
                future=None,
                temp_file=None,
            )

        future = self.executor.submit(self.download_file, url, output_dir, download_id)
        with self.lock:
            self.downloads[download_id].future = future
            self.downloads[download_id].status = DownloadStatus.DOWNLOADING
        return download_id

    def pause_download(self, download_id: str) -> bool:
        with self.lock:
            if download_id not in self.downloads:
                return False
            download = self.downloads[download_id]
            if download.status == DownloadStatus.DOWNLOADING:
                download.pause_event.set()
                download.status = DownloadStatus.PAUSED
                return True
        return False

    def resume_download(self, download_id: str) -> bool:
        with self.lock:
            if download_id not in self.downloads:
                return False
            download = self.downloads[download_id]
            if download.status == DownloadStatus.PAUSED:
                download.pause_event.clear()
                download.status = DownloadStatus.DOWNLOADING
                return True
        return False

    def cancel_download(self, download_id: str) -> bool:
        with self.lock:
            if download_id not in self.downloads:
                return False
            download = self.downloads[download_id]
            if download.status in [DownloadStatus.DOWNLOADING, DownloadStatus.PAUSED]:
                download.cancel_event.set()
                download.pause_event.clear()  # Unblock if paused
                download.status = DownloadStatus.CANCELLED
                if download.future:
                    download.future.cancel()
                # Clean up temp files
                if download.temp_file:
                    try:
                        if osp.isdir(download.temp_file):
                            shutil.rmtree(download.temp_file, ignore_errors=True)
                        elif osp.exists(download.temp_file):
                            os.remove(download.temp_file)
                    except OSError:
                        pass
                return True
        return False

    def clear_completed(self) -> int:
        """Remove completed, failed, and cancelled downloads from tracking. Returns count removed."""
        with self.lock:
            to_remove = [
                did for did, info in self.downloads.items()
                if info.status in (DownloadStatus.COMPLETED, DownloadStatus.FAILED, DownloadStatus.CANCELLED)
            ]
            for did in to_remove:
                del self.downloads[did]
            return len(to_remove)

    def get_status(self, download_id: str) -> Optional[DownloadInfo]:
        with self.lock:
            return self.downloads.get(download_id)

    def get_all_status(self) -> Dict[str, DownloadInfo]:
        with self.lock:
            return self.downloads.copy()
