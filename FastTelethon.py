# "FastTelethon_OPTIMIZED.py"
# Hyper-Optimized Version for Maximum Speed
# Parallel Connections + Async + Memory Optimization

import asyncio
import hashlib
import inspect
import logging
import math
import os
import time
from collections import defaultdict
from typing import (
    Optional,
    List,
    AsyncGenerator,
    Union,
    Awaitable,
    DefaultDict,
    Tuple,
    BinaryIO,
)

from telethon import utils, helpers, TelegramClient
from telethon.crypto import AuthKey
from telethon.network import MTProtoSender
from telethon.tl.alltlobjects import LAYER
from telethon.tl.functions import InvokeWithLayerRequest
from telethon.tl.functions.auth import (
    ExportAuthorizationRequest,
    ImportAuthorizationRequest,
)
from telethon.tl.functions.upload import (
    GetFileRequest,
    SaveFilePartRequest,
    SaveBigFilePartRequest,
)
from telethon.tl.types import (
    Document,
    InputFileLocation,
    InputDocumentFileLocation,
    InputPhotoFileLocation,
    InputPeerPhotoFileLocation,
    TypeInputFile,
    InputFileBig,
    InputFile,
)

log: logging.Logger = logging.getLogger("telethon")

TypeLocation = Union[
    Document,
    InputDocumentFileLocation,
    InputPeerPhotoFileLocation,
    InputFileLocation,
    InputPhotoFileLocation,
]

# ============================================
# ⚡ بهینه‌سازی‌های بنیادی
# ============================================

# سایز بافر بهینه برای Telegram (1 MB بجای 512 KB)
OPTIMAL_CHUNK_SIZE = 1024 * 1024  # 1 MB

# حداکثر اتصالات متوازی (تا 32 اتصال برای فایل‌های بزرگ)
MAX_PARALLEL_CONNECTIONS = 32

# حداکثر تسک‌های همزمان
MAX_CONCURRENT_TASKS = 32

# بافر برای پیش‌خوانی
READ_AHEAD_BUFFER = 5  # تعداد chunks برای pre-buffer


class SpeedOptimizedDownloadSender:
    """دانلود‌کننده با بهینه‌سازی سرعت"""
    
    __slots__ = ['client', 'sender', 'request', 'remaining', 'stride']
    
    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file: TypeLocation,
        offset: int,
        limit: int,
        stride: int,
        count: int,
    ) -> None:
        self.sender = sender
        self.client = client
        self.request = GetFileRequest(file, offset=offset, limit=limit)
        self.stride = stride
        self.remaining = count

    async def next(self) -> Optional[bytes]:
        if not self.remaining:
            return None
        try:
            result = await self.client._call(self.sender, self.request)
            self.remaining -= 1
            self.request.offset += self.stride
            return result.bytes
        except Exception as e:
            log.error(f"Download error: {e}")
            return None

    def disconnect(self) -> Awaitable[None]:
        return self.sender.disconnect()


class SpeedOptimizedUploadSender:
    """آپلود‌کننده با بهینه‌سازی سرعت"""
    
    __slots__ = ['client', 'sender', 'request', 'part_count', 'stride', 'previous', 'loop']
    
    def __init__(
        self,
        client: TelegramClient,
        sender: MTProtoSender,
        file_id: int,
        part_count: int,
        big: bool,
        index: int,
        stride: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.client = client
        self.sender = sender
        self.part_count = part_count
        if big:
            self.request = SaveBigFilePartRequest(file_id, index, part_count, b"")
        else:
            self.request = SaveFilePartRequest(file_id, index, b"")
        self.stride = stride
        self.previous = None
        self.loop = loop

    async def next(self, data: bytes) -> None:
        """آپلود بدون انتظار (fire-and-forget)"""
        if self.previous:
            await self.previous
        self.previous = self.loop.create_task(self._next(data))

    async def _next(self, data: bytes) -> None:
        self.request.bytes = data
        try:
            await self.client._call(self.sender, self.request)
            self.request.file_part += self.stride
        except Exception as e:
            log.error(f"Upload error on part {self.request.file_part}: {e}")
            raise

    async def disconnect(self) -> None:
        if self.previous:
            await self.previous
        return await self.sender.disconnect()


class SpeedOptimizedParallelTransferrer:
    """ترنسفر موازی با سرعت حداکثری"""
    
    __slots__ = [
        'client', 'loop', 'dc_id', 'senders', 'auth_key', 'upload_ticker',
        'semaphore', 'connection_pool', 'stats'
    ]
    
    def __init__(self, client: TelegramClient, dc_id: Optional[int] = None) -> None:
        self.client = client
        self.loop = self.client.loop
        self.dc_id = dc_id or self.client.session.dc_id
        self.auth_key = (
            None
            if dc_id and self.client.session.dc_id != dc_id
            else self.client.session.auth_key
        )
        self.senders = None
        self.upload_ticker = 0
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self.connection_pool = []
        self.stats = {'uploaded': 0, 'downloaded': 0}

    async def _cleanup(self) -> None:
        """تمیز کردن اتصالات"""
        if self.senders:
            tasks = [sender.disconnect() for sender in self.senders]
            await asyncio.gather(*tasks, return_exceptions=True)
        self.senders = None

    @staticmethod
    def _get_connection_count(
        file_size: int, max_count: int = MAX_PARALLEL_CONNECTIONS
    ) -> int:
        """محاسبه بهینه تعداد اتصالات"""
        if file_size > 500 * 1024 * 1024:  # > 500 MB
            return max_count
        elif file_size > 100 * 1024 * 1024:  # > 100 MB
            return 24
        elif file_size > 10 * 1024 * 1024:  # > 10 MB
            return 16
        elif file_size > 1 * 1024 * 1024:  # > 1 MB
            return 12
        else:
            return 8  # فایل‌های کوچک

    async def _init_upload(
        self, connections: int, file_id: int, part_count: int, big: bool
    ) -> None:
        """شروع آپلود با اتصالات متوازی"""
        self.senders = [
            await self._create_upload_sender(file_id, part_count, big, 0, connections),
        ]
        
        # بقیه اتصالات به صورت متوازی
        remaining = await asyncio.gather(
            *[
                self._create_upload_sender(file_id, part_count, big, i, connections)
                for i in range(1, connections)
            ],
            return_exceptions=False
        )
        self.senders.extend(remaining)

    async def _create_upload_sender(
        self, file_id: int, part_count: int, big: bool, index: int, stride: int
    ) -> SpeedOptimizedUploadSender:
        return SpeedOptimizedUploadSender(
            self.client,
            await self._create_sender(),
            file_id,
            part_count,
            big,
            index,
            stride,
            loop=self.loop,
        )

    async def _create_sender(self) -> MTProtoSender:
        """ایجاد sender بدون تاخیر"""
        dc = await self.client._get_dc(self.dc_id)
        sender = MTProtoSender(self.auth_key, loggers=self.client._log)
        
        try:
            await asyncio.wait_for(
                sender.connect(
                    self.client._connection(
                        dc.ip_address,
                        dc.port,
                        dc.id,
                        loggers=self.client._log,
                        proxy=self.client._proxy,
                    )
                ),
                timeout=10.0  # timeout برای اتصال
            )
        except asyncio.TimeoutError:
            log.error("Sender connection timeout")
            raise

        if not self.auth_key:
            log.debug(f"Exporting auth to DC {self.dc_id}")
            auth = await self.client(ExportAuthorizationRequest(self.dc_id))
            self.client._init_request.query = ImportAuthorizationRequest(
                id=auth.id, bytes=auth.bytes
            )
            req = InvokeWithLayerRequest(LAYER, self.client._init_request)
            await sender.send(req)
            self.auth_key = sender.auth_key
        
        return sender

    async def init_upload(
        self,
        file_id: int,
        file_size: int,
        part_size_kb: Optional[float] = None,
        connection_count: Optional[int] = None,
    ) -> Tuple[int, int, bool]:
        """شروع آپلود با تنظیمات بهینه"""
        connection_count = connection_count or self._get_connection_count(file_size)
        
        # استفاده از 1 MB بجای 512 KB برای سرعت بیشتر
        part_size = int((part_size_kb or 1024) * 1024)
        part_count = (file_size + part_size - 1) // part_size
        
        # برای فایل‌های بزرگ‌تر از 5 MB از BigFile استفاده کن
        is_large = file_size > 5 * 1024 * 1024
        
        await self._init_upload(connection_count, file_id, part_count, is_large)
        return part_size, part_count, is_large

    async def init_download(
        self,
        file: TypeLocation,
        file_size: int,
        part_size_kb: Optional[float] = None,
        connection_count: Optional[int] = None,
    ) -> Tuple[int, int]:
        """شروع دانلود متوازی"""
        connection_count = connection_count or self._get_connection_count(file_size)
        part_size = int((part_size_kb or 1024) * 1024)
        part_count = (file_size + part_size - 1) // part_size
        
        self.senders = [
            await self._create_download_sender(file, 0, part_size, connection_count),
        ]
        
        remaining = await asyncio.gather(
            *[
                self._create_download_sender(
                    file, i * part_size, part_size, connection_count
                )
                for i in range(1, connection_count)
            ],
            return_exceptions=False
        )
        self.senders.extend(remaining)
        
        return part_size, part_count

    async def _create_download_sender(
        self,
        file: TypeLocation,
        offset: int,
        limit: int,
        stride: int,
    ) -> SpeedOptimizedDownloadSender:
        return SpeedOptimizedDownloadSender(
            self.client,
            await self._create_sender(),
            file,
            offset,
            limit,
            stride * limit,
            1,
        )

    async def upload(self, part: bytes) -> None:
        """آپلود قطعه"""
        async with self.semaphore:
            await self.senders[self.upload_ticker].next(part)
            self.upload_ticker = (self.upload_ticker + 1) % len(self.senders)
            self.stats['uploaded'] += len(part)

    async def download(self) -> Optional[bytes]:
        """دانلود قطعه"""
        if not self.senders:
            return None
        async with self.semaphore:
            return await self.senders[self.upload_ticker].next()

    async def finish_upload(self) -> None:
        """اتمام آپلود"""
        await self._cleanup()

    async def finish_download(self) -> None:
        """اتمام دانلود"""
        await self._cleanup()


def stream_file(file_to_stream: BinaryIO, chunk_size=OPTIMAL_CHUNK_SIZE):
    """خواندن فایل به صورت کارآمد"""
    while True:
        data_read = file_to_stream.read(chunk_size)
        if not data_read:
            break
        yield data_read


class BufferPool:
    """مدیریت بافر برای کاهش allocation"""
    
    def __init__(self, size: int = 10):
        self.pool = [bytearray() for _ in range(size)]
        self.available = list(range(size))
        self.lock = asyncio.Lock()
    
    async def get(self) -> bytearray:
        async with self.lock:
            if self.available:
                return self.pool[self.available.pop()]
        return bytearray()
    
    async def put(self, buf: bytearray):
        buf.clear()
        async with self.lock:
            if len(self.available) < len(self.pool):
                self.available.append(self.pool.index(buf))


async def _internal_transfer_to_telegram_optimized(
    client: TelegramClient,
    response: BinaryIO,
    progress_callback: callable = None,
    connection_count: Optional[int] = None,
) -> Tuple[TypeInputFile, int]:
    """آپلود سریع به Telegram"""
    
    file_id = helpers.generate_random_long()
    file_size = os.path.getsize(response.name)
    
    hash_md5 = hashlib.md5()
    uploader = SpeedOptimizedParallelTransferrer(client)
    part_size, part_count, is_large = await uploader.init_upload(
        file_id, file_size, connection_count=connection_count
    )
    
    buffer = bytearray()
    upload_tasks = []
    
    start_time = time.time()
    
    for data in stream_file(response, chunk_size=part_size):
        # progress callback
        if progress_callback:
            r = progress_callback(response.tell(), file_size)
            if inspect.isawaitable(r):
                await r
        
        if not is_large:
            hash_md5.update(data)
        
        # بهینه‌سازی: اگر دقیق یک part بود، مستقیم فرستاد
        if len(buffer) == 0 and len(data) == part_size:
            task = asyncio.create_task(uploader.upload(data))
            upload_tasks.append(task)
            
            # انتظر برای کاهش استفاده از حافظه
            if len(upload_tasks) >= MAX_CONCURRENT_TASKS:
                await asyncio.gather(*upload_tasks)
                upload_tasks = []
            continue
        
        # ترکیب با بافر
        new_len = len(buffer) + len(data)
        if new_len >= part_size:
            cutoff = part_size - len(buffer)
            buffer.extend(data[:cutoff])
            
            task = asyncio.create_task(uploader.upload(bytes(buffer)))
            upload_tasks.append(task)
            
            buffer.clear()
            buffer.extend(data[cutoff:])
            
            if len(upload_tasks) >= MAX_CONCURRENT_TASKS:
                await asyncio.gather(*upload_tasks)
                upload_tasks = []
        else:
            buffer.extend(data)
    
    # بقیه داده‌ها
    if len(buffer) > 0:
        task = asyncio.create_task(uploader.upload(bytes(buffer)))
        upload_tasks.append(task)
    
    # انتظار برای تمام تسک‌ها
    if upload_tasks:
        await asyncio.gather(*upload_tasks)
    
    await uploader.finish_upload()
    
    elapsed = time.time() - start_time
    speed = file_size / elapsed / (1024 * 1024)  # MB/s
    log.info(f"Upload completed: {speed:.2f} MB/s")
    
    if is_large:
        return InputFileBig(file_id, part_count, "upload"), file_size
    else:
        return InputFile(file_id, part_count, "upload", hash_md5.hexdigest()), file_size


async def upload_file(
    client: TelegramClient,
    file: BinaryIO,
    progress_callback: callable = None,
    connection_count: Optional[int] = None,
) -> TypeInputFile:
    """آپلود فایل با سرعت حداکثری"""
    res = (
        await _internal_transfer_to_telegram_optimized(
            client, file, progress_callback, connection_count
        )
    )[0]
    return res


async def download_file(
    client: TelegramClient,
    document: Document,
    file: BinaryIO,
    progress_callback: callable = None,
    connection_count: Optional[int] = None,
) -> None:
    """دانلود فایل با سرعت حداکثری"""
    
    file_size = document.size
    downloader = SpeedOptimizedParallelTransferrer(client)
    part_size, part_count = await downloader.init_download(
        document, file_size, connection_count=connection_count
    )
    
    download_tasks = []
    start_time = time.time()
    downloaded = 0
    
    for i in range(part_count):
        task = asyncio.create_task(downloader.download())
        download_tasks.append(task)
        
        if len(download_tasks) >= MAX_CONCURRENT_TASKS:
            results = await asyncio.gather(*download_tasks, return_exceptions=True)
            for data in results:
                if data:
                    file.write(data)
                    downloaded += len(data)
                    
                    if progress_callback:
                        r = progress_callback(downloaded, file_size)
                        if inspect.isawaitable(r):
                            await r
            
            download_tasks = []
    
    if download_tasks:
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        for data in results:
            if data:
                file.write(data)
                downloaded += len(data)
    
    await downloader.finish_download()
    
    elapsed = time.time() - start_time
    speed = file_size / elapsed / (1024 * 1024)  # MB/s
    log.info(f"Download completed: {speed:.2f} MB/s")
