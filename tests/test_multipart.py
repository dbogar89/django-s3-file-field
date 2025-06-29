from __future__ import annotations

from io import BytesIO
from typing import TYPE_CHECKING, Callable, cast

from botocore.exceptions import ClientError
from django.conf import settings
from django.core.files.storage import FileSystemStorage, Storage
from minio import Minio
from minio_storage.storage import MinioStorage
import pytest
from pytest_mock import MockerFixture
import requests
from storages.backends.s3 import S3Storage

from s3_file_field._multipart import (
    MultipartManager,
    ObjectNotFoundError,
    TransferredPart,
    TransferredParts,
)
from s3_file_field._multipart_minio import MinioMultipartManager
from s3_file_field._multipart_s3 import S3MultipartManager
from s3_file_field._sizes import gb, mb

if TYPE_CHECKING:
    # mypy_boto3_s3 only provides types
    import mypy_boto3_s3 as s3


def s3_storage_factory() -> S3Storage:
    storage = S3Storage(
        access_key=settings.MINIO_STORAGE_ACCESS_KEY,
        secret_key=settings.MINIO_STORAGE_SECRET_KEY,
        region_name="test-region",
        bucket_name=settings.MINIO_STORAGE_MEDIA_BUCKET_NAME,
        # For testing, connect to a local Minio instance
        endpoint_url=(
            f'{"https" if settings.MINIO_STORAGE_USE_HTTPS else "http"}:'
            f"//{settings.MINIO_STORAGE_ENDPOINT}"
        ),
    )

    resource: s3.ServiceResource = storage.connection
    client: s3.Client = resource.meta.client
    try:
        client.head_bucket(Bucket=settings.MINIO_STORAGE_MEDIA_BUCKET_NAME)
    except ClientError:
        client.create_bucket(Bucket=settings.MINIO_STORAGE_MEDIA_BUCKET_NAME)

    return storage


def minio_storage_factory() -> MinioStorage:
    return MinioStorage(
        minio_client=Minio(
            endpoint=settings.MINIO_STORAGE_ENDPOINT,
            secure=settings.MINIO_STORAGE_USE_HTTPS,
            access_key=settings.MINIO_STORAGE_ACCESS_KEY,
            secret_key=settings.MINIO_STORAGE_SECRET_KEY,
            # Don't use s3_connection_params.region, let Minio set its own value internally
        ),
        bucket_name=settings.MINIO_STORAGE_MEDIA_BUCKET_NAME,
        auto_create_bucket=True,
        presign_urls=True,
        # TODO: Test the case of an alternate base_url
        # base_url='http://minio:9000/bucket-name'
    )


@pytest.fixture()
def s3_storage() -> S3Storage:
    return s3_storage_factory()


@pytest.fixture()
def minio_storage() -> MinioStorage:
    return minio_storage_factory()


@pytest.fixture(params=[s3_storage_factory, minio_storage_factory], ids=["s3", "minio"])
def storage(request: pytest.FixtureRequest) -> Storage:
    storage_factory = cast(Callable[[], Storage], request.param)
    return storage_factory()


@pytest.fixture()
def s3_multipart_manager(s3_storage: S3Storage) -> S3MultipartManager:
    return S3MultipartManager(s3_storage)


@pytest.fixture()
def minio_multipart_manager(minio_storage: MinioStorage) -> MinioMultipartManager:
    return MinioMultipartManager(minio_storage)


@pytest.fixture()
def multipart_manager(storage: Storage) -> MultipartManager:
    return MultipartManager.from_storage(storage)


def test_multipart_manager_supported_storage(storage: Storage) -> None:
    assert MultipartManager.supported_storage(storage)


def test_multipart_manager_supported_storage_unsupported() -> None:
    storage = FileSystemStorage()
    assert not MultipartManager.supported_storage(storage)


def test_multipart_manager_initialize_upload(multipart_manager: MultipartManager) -> None:
    initialization = multipart_manager.initialize_upload(
        "new-object",
        100,
        "text/plain",
    )

    assert initialization


@pytest.mark.parametrize("file_size", [10, mb(10), mb(12)], ids=["10B", "10MB", "12MB"])
def test_multipart_manager_complete_upload(
    multipart_manager: MultipartManager, file_size: int
) -> None:
    initialization = multipart_manager.initialize_upload("new-object", file_size, "text/plain")

    transferred_parts = TransferredParts(
        object_key=initialization.object_key, upload_id=initialization.upload_id, parts=[]
    )

    for part in initialization.parts:
        resp = requests.put(part.upload_url, data=b"a" * part.size, timeout=5)
        resp.raise_for_status()
        transferred_parts.parts.append(
            TransferredPart(part_number=part.part_number, size=part.size, etag=resp.headers["ETag"])
        )

    completed_upload = multipart_manager.complete_upload(transferred_parts)
    assert completed_upload
    assert completed_upload.complete_url
    assert completed_upload.body


def test_multipart_manager_test_upload(multipart_manager: MultipartManager) -> None:
    multipart_manager.test_upload()


def test_multipart_manager_create_upload_id(multipart_manager: MultipartManager) -> None:
    upload_id = multipart_manager._create_upload_id("new-object", "text/plain")
    assert isinstance(upload_id, str)


def test_multipart_manager_generate_presigned_part_url(multipart_manager: MultipartManager) -> None:
    upload_url = multipart_manager._generate_presigned_part_url(
        "new-object", "fake-upload-id", 1, 100
    )

    assert isinstance(upload_url, str)


@pytest.mark.skip()
def test_multipart_manager_generate_presigned_part_url_content_length(
    multipart_manager: MultipartManager,
) -> None:
    # TODO: make this work for Minio
    upload_url = multipart_manager._generate_presigned_part_url(
        "new-object", "fake-upload-id", 1, 100
    )
    # Ensure Content-Length is a signed header
    assert "content-length" in upload_url


def test_multipart_manager_generate_presigned_complete_url(
    multipart_manager: MultipartManager,
) -> None:
    upload_url = multipart_manager._generate_presigned_complete_url(
        TransferredParts(object_key="new-object", upload_id="fake-upload-id", parts=[])
    )

    assert isinstance(upload_url, str)


def test_multipart_manager_generate_presigned_complete_body(
    multipart_manager: MultipartManager,
) -> None:
    body = multipart_manager._generate_presigned_complete_body(
        TransferredParts(
            object_key="new-object",
            upload_id="fake-upload-id",
            parts=[
                TransferredPart(part_number=1, size=1, etag="fake-etag-1"),
                TransferredPart(part_number=2, size=2, etag="fake-etag-2"),
            ],
        )
    )

    assert body == (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<CompleteMultipartUpload xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
        "<Part><PartNumber>1</PartNumber><ETag>fake-etag-1</ETag></Part>"
        "<Part><PartNumber>2</PartNumber><ETag>fake-etag-2</ETag></Part>"
        "</CompleteMultipartUpload>"
    )


@pytest.mark.parametrize("file_size", [10, mb(10), mb(12)], ids=["10B", "10MB", "12MB"])
def test_multipart_manager_get_object_size(
    storage: Storage, multipart_manager: MultipartManager, file_size: int
) -> None:
    key = storage.get_alternative_name(f"object-with-size-{file_size}", "")
    # In theory, Storage.save can change the key, though this shouldn't happen with a randomized key
    key = storage.save(name=key, content=BytesIO(b"X" * file_size))

    size = multipart_manager.get_object_size(
        object_key=key,
    )

    assert size == file_size

    storage.delete(key)


def test_multipart_manager_get_object_size_not_found(multipart_manager: MultipartManager) -> None:
    with pytest.raises(ObjectNotFoundError):
        multipart_manager.get_object_size(
            object_key="no-such-object",
        )


@pytest.mark.parametrize(
    ("file_size", "requested_part_size", "initial_part_size", "final_part_size", "part_count"),
    [
        # Base
        (mb(50), mb(10), mb(10), mb(10), 5),
        # Different final size
        (mb(55), mb(10), mb(10), mb(5), 6),
        # Single part
        (mb(10), mb(10), 0, mb(10), 1),
        # Too small requested_part_size
        (mb(50), mb(2), mb(5), mb(5), 10),
        # Too large requested_part_size
        (gb(50), gb(10), gb(5), gb(5), 10),
        # Too many parts
        (mb(100_000), mb(5), mb(10), mb(10), 10_000),
        # TODO: file too large
    ],
    ids=[
        "base",
        "different_final",
        "single_part",
        "too_small_part",
        "too_large_part",
        "too_many_part",
    ],
)
def test_multipart_manager_iter_part_sizes(
    mocker: MockerFixture,
    file_size: int,
    requested_part_size: int,
    initial_part_size: int,
    final_part_size: int,
    part_count: int,
) -> None:
    mocker.patch.object(MultipartManager, "part_size", new=requested_part_size)

    part_nums, part_sizes = zip(*MultipartManager._iter_part_sizes(file_size))

    # TODO: zip(*) returns a tuple, but semantically this should be a list
    assert part_nums == tuple(range(1, part_count + 1))

    assert all(part_size == initial_part_size for part_size in part_sizes[:-1])
    assert part_sizes[-1] == final_part_size
