from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import typing
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import Dict, cast
from urllib.parse import unquote

import msgpack
from dataclasses_json import config
from google.protobuf import json_format as _json_format
from google.protobuf.struct_pb2 import Struct
from marshmallow import fields
from mashumaro.mixins.json import DataClassJSONMixin
from mashumaro.types import SerializableType

from flytekit.core.constants import MESSAGEPACK
from flytekit.core.context_manager import FlyteContext, FlyteContextManager
from flytekit.core.type_engine import (
    AsyncTypeTransformer,
    TypeEngine,
    TypeTransformerFailedError,
    get_underlying_type,
)
from flytekit.exceptions.user import FlyteAssertion
from flytekit.extras.pydantic_transformer.decorator import model_serializer, model_validator
from flytekit.loggers import logger
from flytekit.models.core import types as _core_types
from flytekit.models.core.types import BlobType
from flytekit.models.literals import Binary, Blob, BlobMetadata, Literal, Scalar
from flytekit.models.types import LiteralType
from flytekit.types.pickle.pickle import FlytePickleTransformer


def noop(): ...


T = typing.TypeVar("T")


@dataclass
class FlyteFile(SerializableType, os.PathLike, typing.Generic[T], DataClassJSONMixin):
    path: typing.Union[str, os.PathLike] = field(default=None, metadata=config(mm_field=fields.String()))  # type: ignore
    metadata: typing.Optional[dict[str, str]] = None
    """
    Since there is no native Python implementation of files and directories for the Flyte Blob type, (like how int
    exists for Flyte's Integer type) we need to create one so that users can express that their tasks take
    in or return a file. There is ``pathlib.Path`` of course, (which is usable in Flytekit as a return value, though
    not a return type), but it made more sense to create a new type esp. since we can add on additional properties.

    Files (and directories) differ from the primitive types like floats and string in that Flytekit typically uploads
    the contents of the files to the blob store connected with your Flyte installation. That is, the Python native
    literal that represents a file is typically just the path to the file on the local filesystem. However in Flyte,
    an instance of a file is represented by a {{< py_class_ref Blob <flytekit.models.literals.Blob> >}} literal,
    with the ``uri`` field set to the location in the Flyte blob store (AWS/GCS etc.). Take a look at the
    :std:ref:`data handling doc <flyte:divedeep-data-management>` for a deeper discussion.

    We decided to not support ``pathlib.Path`` as an input/output type because if you wanted the automatic
    upload/download behavior, you should just use the ``FlyteFile`` type. If you do not, then a ``str`` works just as
    well.

    The prefix for where uploads go is set by the raw output data prefix setting, which should be set at registration
    time in the launch plan. See the option listed under ``flytectl register examples --help`` for more information.
    If not set in the launch plan, then your Flyte backend will specify a default. This default is itself configurable
    as well. Contact your Flyte platform administrators to change or ascertain the value.

    In short, if a task returns ``"/path/to/file"`` and the task's signature is set to return ``FlyteFile``, then the
    contents of ``/path/to/file`` are uploaded.

    You can also make it so that the upload does not happen. There are different types of
    task/workflow signatures. Keep in mind that in the backend, in Admin and in the blob store, there is only one type
    that represents files, the {{< py_class_ref Blob <flytekit.models.core.types.BlobType> >}} type.

    Whether the uploading happens or not, the behavior of the translation between Python native values and Flyte
    literal values depends on a few attributes:

    * The declared Python type in the signature. These can be
      * :class:`python:flytekit.FlyteFile`
      * :class:`python:os.PathLike`
      Note that ``os.PathLike`` is only a type in Python, you can't instantiate it.
    * The type of the Python native value we're returning. These can be
      * {{< py_class_ref flytekit.FlyteFile >}}
      * {{< py_class_ref pathlib.Path >}}
      * {{< py_class_ref str >}}
    * Whether the value being converted is a "remote" path or not. For instance, if a task returns a value of
      "http://www.google.com" as a ``FlyteFile``, obviously it doesn't make sense for us to try to upload that to the
      Flyte blob store. So no remote paths are uploaded. Flytekit considers a path remote if it starts with ``s3://``,
      ``gs://``, ``http(s)://``, or even ``file://``.

    **Converting from a Flyte literal value to a Python instance of FlyteFile**

    +-------------+---------------+---------------------------------------------+--------------------------------------+
    |             |               |              Expected Python type                                                  |
    +-------------+---------------+---------------------------------------------+--------------------------------------+
    | Type of Flyte IDL Literal   | FlyteFile                                   |  os.PathLike                         |
    +=============+===============+=============================================+======================================+
    | Blob        | uri matches   | FlyteFile object stores the original string |                                      |
    |             | http(s)/s3/gs | path, but points to a local file instead.   |                                      |
    |             |               |                                             |                                      |
    |             |               | * [fn] downloader: function that writes to  |                                      |
    |             |               |   path when open'ed.                        |                                      |
    |             |               | * [fn] download: will trigger               | Basically this signals Flyte should  |
    |             |               |   download                                  | stay out of the way. You still get   |
    |             |               | * path: randomly generated local path that  | a FlyteFile object (which implements |
    |             |               |   will not exist until downloaded           | the os.PathLike interface)           |
    |             |               | * remote_path: None                         |                                      |
    |             |               | * remote_source: original http/s3/gs path   | * [fn] downloader: noop function,    |
    |             |               |                                             |   even if it's http/s3/gs            |
    |             +---------------+---------------------------------------------+ * [fn] download: raises              |
    |             | uri matches   | FlyteFile object just wraps the string      |   exception                          |
    |             | /local/path   |                                             | * path: just the given path          |
    |             |               | * [fn] downloader: noop function            | * remote_path: None                  |
    |             |               | * [fn] download: raises exception           | * remote_source: None                |
    |             |               | * path: just the given path                 |                                      |
    |             |               | * remote_path: None                         |                                      |
    |             |               | * remote_source: None                       |                                      |
    +-------------+---------------+---------------------------------------------+--------------------------------------+

    **Converting from a Python value (FlyteFile, str, or pathlib.Path) to a Flyte literal**

    +-------------+---------------+---------------------------------------------+--------------------------------------+
    |             |               |                               Expected Python type                                 |
    +-------------+---------------+---------------------------------------------+--------------------------------------+
    | Type of Python value        | FlyteFile                                   |  os.PathLike                         |
    +=============+===============+=============================================+======================================+
    | str or      | path matches  | Blob object is returned with uri set to the given path. No uploading happens.      |
    | pathlib.Path| http(s)/s3/gs |                                                                                    |
    |             +---------------+---------------------------------------------+--------------------------------------+
    |             | path matches  | Contents of file are uploaded to the Flyte  | No warning is logged since only a    |
    |             | /local/path   | blob store (S3, GCS, etc.), in a bucket     | string is given (as opposed to a     |
    |             |               | determined by the raw_output_data_prefix    | FlyteFile). Blob object is returned  |
    |             |               | setting.                                    | with uri set to just the given path. |
    |             |               | Blob object is returned with uri pointing   | No uploading happens.                |
    |             |               | to the blob store location.                 |                                      |
    |             |               |                                             |                                      |
    +-------------+---------------+---------------------------------------------+--------------------------------------+
    | FlyteFile   | path matches  | Blob object is returned with uri set to the given path.                            |
    |             | http(s)/s3/gs | Nothing is uploaded.                                                               |
    |             +---------------+---------------------------------------------+--------------------------------------+
    |             | path matches  | Contents of file are uploaded to the Flyte  | Warning is logged since you're       |
    |             | /local/path   | blob store (S3, GCS, etc.), in a bucket     | passing a more complex object (a     |
    |             |               | determined by the raw_output_data_prefix    | FlyteFile) and expecting a simpler   |
    |             |               | setting. If remote_path is given, then that | interface (os.PathLike). Blob object |
    |             |               | is used instead of the random path. Blob    | is returned with uri set to just the |
    |             |               | object is returned with uri pointing to     | given path. No uploading happens.    |
    |             |               | the blob store location.                    |                                      |
    |             |               |                                             |                                      |
    +-------------+---------------+---------------------------------------------+--------------------------------------+

    Since Flyte file types have a string embedded in it as part of the type, you can add a
    format by specifying a string after the class like so. ::

        def t2() -> flytekit_typing.FlyteFile["csv"]:
            return "/tmp/local_file.csv"
    """

    def _serialize(self) -> typing.Dict[str, typing.Any]:
        lv = FlyteFilePathTransformer().to_literal(FlyteContextManager.current_context(), self, type(self), None)
        out = {"path": lv.scalar.blob.uri}
        if lv.metadata:
            out["metadata"] = lv.metadata
        return out

    @classmethod
    def _deserialize(cls, value) -> "FlyteFile":
        return FlyteFilePathTransformer().dict_to_flyte_file(dict_obj=value, expected_python_type=cls)

    @model_serializer
    def serialize_flyte_file(self) -> Dict[str, typing.Any]:
        lv = FlyteFilePathTransformer().to_literal(FlyteContextManager.current_context(), self, type(self), None)
        out = {"path": lv.scalar.blob.uri}
        if lv.metadata:
            out["metadata"] = lv.metadata
        return out

    @model_validator(mode="after")
    def deserialize_flyte_file(self, info) -> "FlyteFile":
        if info.context is None or info.context.get("deserialize") is not True:
            return self

        pv = FlyteFilePathTransformer().to_python_value(
            FlyteContextManager.current_context(),
            Literal(
                scalar=Scalar(
                    blob=Blob(
                        metadata=BlobMetadata(
                            type=_core_types.BlobType(
                                format="", dimensionality=_core_types.BlobType.BlobDimensionality.SINGLE
                            )
                        ),
                        uri=self.path,
                    )
                ),
                metadata=self.metadata,
            ),
            type(self),
        )
        return pv

    @classmethod
    def extension(cls) -> str:
        return ""

    @classmethod
    def new_remote_file(cls, name: typing.Optional[str] = None, alt: typing.Optional[str] = None) -> FlyteFile:
        """
        Create a new FlyteFile object with a remote path.

        :param name: If you want to specify a different name for the file, you can specify it here.
        :param alt: If you want to specify a different prefix head than the default one, you can specify it here.
        """
        ctx = FlyteContextManager.current_context()
        remote_path = ctx.file_access.generate_new_custom_path(alt=alt, stem=name)
        return cls(path=remote_path)

    @classmethod
    def from_source(cls, source: str | os.PathLike) -> FlyteFile:
        """
        Create a new FlyteFile object with the remote source set to the input
        """
        ctx = FlyteContextManager.current_context()
        lit = Literal(
            scalar=Scalar(
                blob=Blob(
                    metadata=BlobMetadata(type=BlobType(format="", dimensionality=BlobType.BlobDimensionality.SINGLE)),
                    uri=source,
                )
            )
        )
        t = FlyteFilePathTransformer()
        return t.to_python_value(ctx, lit, cls)

    @classmethod
    def new(cls, filename: str | os.PathLike) -> FlyteFile:
        """
        Create a new FlyteFile object in the current Flyte working directory
        """

        if os.path.isabs(filename):
            raise ValueError("Path should be relative.")

        ctx = FlyteContextManager.current_context()

        path = os.path.join(ctx.user_space_params.working_directory, filename)

        return cls(path=path)

    def __class_getitem__(cls, item: typing.Union[str, typing.Type]) -> typing.Type[FlyteFile]:
        from flytekit.types.file import FileExt

        if item is None:
            return cls

        item_string = FileExt.check_and_convert_to_str(item)

        item_string = item_string.strip().lstrip("~").lstrip(".")
        if item == "":
            return cls

        class _SpecificFormatClass(FlyteFile):
            # Get the type engine to see this as kind of a generic
            __origin__ = FlyteFile

            class AttributeHider:
                def __get__(self, instance, owner):
                    raise AttributeError(
                        """We have to return false in hasattr(cls, "__class_getitem__") to make mashumaro deserialize FlyteFile correctly."""
                    )

            # Set __class_getitem__ to AttributeHider to make mashumaro deserialize FlyteFile correctly
            # https://stackoverflow.com/questions/6057130/python-deleting-a-class-attribute-in-a-subclass/6057409
            # Since mashumaro will use the method __class_getitem__ and __origin__ to construct the dataclass back
            # https://github.com/Fatal1ty/mashumaro/blob/e945ee4319db49da9f7b8ede614e988cc8c8956b/mashumaro/core/meta/helpers.py#L300-L303
            __class_getitem__ = AttributeHider()  # type: ignore

            @classmethod
            def extension(cls) -> str:
                return item_string

        return _SpecificFormatClass

    def __init__(
        self,
        path: typing.Union[str, os.PathLike],
        downloader: typing.Callable = noop,
        remote_path: typing.Optional[typing.Union[os.PathLike, str, bool]] = None,
        metadata: typing.Optional[dict[str, str]] = None,
    ):
        """
        FlyteFile's init method.

        :param path: The source path that users are expected to call open() on.
        :param downloader: Optional function that can be passed that used to delay downloading of the actual fil
            until a user actually calls open().
        :param remote_path: If the user wants to return something and also specify where it should be uploaded to.
            Alternatively, if the user wants to specify a remote path for a file that's already in the blob store,
            the path should point to the location and remote_path should be set to False.
        """
        # Make this field public, so that the dataclass transformer can set a value for it
        # https://github.com/flyteorg/flytekit/blob/bcc8541bd6227b532f8462563fe8aac902242b21/flytekit/core/type_engine.py#L298
        self.path = path
        self.metadata = metadata
        self._downloader = downloader
        self._downloaded = False
        self._remote_path = remote_path
        self._remote_source: typing.Optional[typing.Union[str, os.PathLike]] = None

        # Setup local path and downloader for delayed downloading
        # We introduce another attribute self._local_path to avoid overriding user-defined self.path
        self._local_path = self.path

        ctx = FlyteContextManager.current_context()
        if ctx.file_access.is_remote(self.path):
            self._remote_source = self.path
            self._local_path = ctx.file_access.get_random_local_path(self._remote_source)
            self._downloader = partial(
                ctx.file_access.get_data,
                ctx=ctx,
                remote_path=self._remote_source,  # type: ignore
                local_path=self._local_path,
            )

    def __fspath__(self):
        """
        Define the file path protocol for opening FlyteFile with the context manager,
        following show two common use cases:

        1. Directly open a FlyteFile with a local path:

        ff = FlyteFile(path=local_path)
        with open(ff, "r") as f:
            # Read your local file here
            # ...

        There's no need to handle downloading of the file because it's on the local file system.
        In this case, a dummy downloading will be done.

        2. Directly open a FlyteFile with a remote path:

        ff = FlyteFile(path=remote_path)
        with open(ff, "r") as f:
            # Read your remote file here
            # ...

        We now support directly opening a FlyteFile with a file from the remote data storage.
        In this case, a delayed downloading of the remote file will be done.
        For details, please refer to this issue: https://github.com/flyteorg/flyte/issues/6090.
        """
        if not self._downloaded:
            # Download data from remote to local or run dummy downloading for input local path
            self._downloader()
            self._downloaded = True
        return self._local_path

    def __eq__(self, other):
        if isinstance(other, FlyteFile):
            return (
                self.path == other.path
                and self._remote_path == other._remote_path
                and self.extension() == other.extension()
            )
        else:
            return self.path == other

    @property
    def downloaded(self) -> bool:
        return self._downloaded

    @property
    def remote_path(self) -> typing.Optional[os.PathLike]:
        # Find better ux for no-uploads in the future.
        return self._remote_path  # type: ignore

    @property
    def remote_source(self) -> str:
        """
        If this is an input to a task, and the original path is an ``s3`` bucket, Flytekit downloads the
        file for the user. In case the user wants access to the original path, it will be here.
        """
        return typing.cast(str, self._remote_source)

    def download(self) -> str:
        return self.__fspath__()

    async def _download(self) -> str:
        return self.__fspath__()

    @contextmanager
    def open(
        self,
        mode: str,
        cache_type: typing.Optional[str] = None,
        cache_options: typing.Optional[typing.Dict[str, typing.Any]] = None,
    ):
        """Returns a streaming File handle

        ```python
        @task
        def copy_file(ff: FlyteFile) -> FlyteFile:
            new_file = FlyteFile.new_remote_file()
            with ff.open("rb", cache_type="readahead") as r:
                with new_file.open("wb") as w:
                    w.write(r.read())
            return new_file
        ```

        :param mode: Open mode. For example: 'r', 'w', 'rb', 'rt', 'wb', etc.
        :type mode: str
        :param cache_type: Specifies the cache type. Possible values are "blockcache", "bytes", "mmap", "readahead", "first", or "background".
            This is especially useful for large file reads. See https://filesystem-spec.readthedocs.io/en/latest/api.html#readbuffering.
        :type cache_type: str, optional
        :param cache_options: A Dict corresponding to the parameters for the chosen cache_type.
             Refer to fsspec caching options above.
        :type cache_options: Dict[str, Any], optional
        """
        ctx = FlyteContextManager.current_context()
        final_path = self.path
        if self.remote_source:
            final_path = self.remote_source
        elif self.remote_path:
            final_path = self.remote_path
        fs = ctx.file_access.get_filesystem_for_path(final_path)
        f = fs.open(final_path, mode, cache_type=cache_type, cache_options=cache_options)
        yield f
        f.close()

    def __repr__(self):
        return self.path

    def __str__(self):
        return self.path

    def __hash__(self):
        return hash(str(self.path))


class FlyteFilePathTransformer(AsyncTypeTransformer[FlyteFile]):
    def __init__(self):
        super().__init__(name="FlyteFilePath", t=FlyteFile)

    @staticmethod
    def get_format(t: typing.Union[typing.Type[FlyteFile], os.PathLike]) -> str:
        if t is os.PathLike:
            return ""
        return cast(FlyteFile, t).extension()

    def _blob_type(self, format: str) -> BlobType:
        return BlobType(format=format, dimensionality=BlobType.BlobDimensionality.SINGLE)

    def assert_type(
        self, t: typing.Union[typing.Type[FlyteFile], os.PathLike], v: typing.Union[FlyteFile, os.PathLike, str]
    ):
        if isinstance(v, os.PathLike) or isinstance(v, FlyteFile) or isinstance(v, str):
            return
        raise TypeError(
            f"No automatic conversion found from type {type(v)} to FlyteFile."
            f"Supported (os.PathLike, str, Flytefile)"
        )

    def get_literal_type(self, t: typing.Union[typing.Type[FlyteFile], os.PathLike]) -> LiteralType:
        return LiteralType(blob=self._blob_type(format=FlyteFilePathTransformer.get_format(t)))

    def get_mime_type_from_extension(self, extension: str) -> typing.Union[str, typing.Sequence[str]]:
        extension_to_mime_type = {
            "hdf5": "text/plain",
            "joblib": "application/octet-stream",
            "python_pickle": "application/octet-stream",
            "ipynb": "application/json",
            "onnx": "application/json",
            "tfrecord": "application/octet-stream",
            "jsonl": ["application/json", "application/x-ndjson"],
        }

        for ext, mimetype in mimetypes.types_map.items():
            extension_to_mime_type[ext.split(".")[1]] = mimetype

        return extension_to_mime_type[extension]

    def validate_file_type(
        self, python_type: typing.Type[FlyteFile], source_path: typing.Union[str, os.PathLike]
    ) -> None:
        """
        This method validates the type of the file at source_path against the expected python_type.
        It uses the magic library to determine the real type of the file. If the magic library is not installed,
        it logs a debug message and returns. If the actual file does not exist, it returns without raising an error.

        :param python_type: The expected type of the file
        :param source_path: The path to the file to validate
        :raises ValueError: If the real type of the file is not the same as the expected python_type
        """
        if FlyteFilePathTransformer.get_format(python_type) == "":
            return

        try:
            # isolate the exception to the libmagic import
            import magic

        except ImportError as e:
            logger.debug(f"Libmagic is not installed. Error message: {e}")
            return

        ctx = FlyteContext.current_context()
        if ctx.file_access.is_remote(source_path):
            # Skip validation for remote files. One of the use cases for FlyteFile is to point to remote files,
            # you might have access to a remote file (e.g., in s3) that you want to pass to a Flyte workflow.
            # Therefore, we should only validate FlyteFiles for which their path is considered local.
            return

        if FlyteFilePathTransformer.get_format(python_type):
            real_type = magic.from_file(source_path, mime=True)
            expected_type = self.get_mime_type_from_extension(FlyteFilePathTransformer.get_format(python_type))
            if real_type not in expected_type:
                raise ValueError(f"Incorrect file type, expected {expected_type}, got {real_type}")

    async def async_to_literal(
        self,
        ctx: FlyteContext,
        python_val: typing.Union[FlyteFile, os.PathLike, str],
        python_type: typing.Type[FlyteFile],
        expected: LiteralType,
    ) -> Literal:
        remote_path = None
        should_upload = True

        if python_val is None:
            raise TypeTransformerFailedError("None value cannot be converted to a file.")

        # Correctly handle `Annotated[FlyteFile, ...]` by extracting the origin type
        python_type = get_underlying_type(python_type)

        if not (python_type is os.PathLike or issubclass(python_type, FlyteFile)):
            raise ValueError(f"Incorrect type {python_type}, must be either a FlyteFile or os.PathLike")

        # information used by all cases
        meta = BlobMetadata(type=self._blob_type(format=FlyteFilePathTransformer.get_format(python_type)))

        if isinstance(python_val, FlyteFile):
            # Cast the source path to str type to avoid error raised when the source path is used as the blob uri,
            # please refer to this issue: https://github.com/flyteorg/flyte/issues/5872.
            source_path = str(python_val.path)
            self.validate_file_type(python_type, source_path)

            # If the object has a remote source, then we just convert it back. This means that if someone is just
            # going back and forth between a FlyteFile Python value and a Blob Flyte IDL value, we don't do anything.
            if python_val._remote_source is not None:
                return Literal(
                    scalar=Scalar(blob=Blob(metadata=meta, uri=python_val._remote_source)), metadata=python_val.metadata
                )

            # If the user specified the remote_path to be False, that means no matter what, do not upload. Also if the
            # path given is already a remote path, say https://www.google.com, the concept of uploading to the Flyte
            # blob store doesn't make sense.
            if python_val.remote_path is False or ctx.file_access.is_remote(source_path):
                should_upload = False
            # If the type that's given is a simpler type, we also don't upload, and print a warning too.
            if python_type is os.PathLike:
                logger.warning(
                    f"Converting from a FlyteFile Python instance to a Blob Flyte object, but only a {python_type} was"
                    f" specified. Since a simpler type was specified, we'll skip uploading!"
                )
                should_upload = False

            # Set the remote destination if one was given instead of triggering a random one below
            remote_path = python_val.remote_path or None

            if ctx.execution_state.is_local_execution() and python_val.remote_path is None:
                should_upload = False

        elif isinstance(python_val, pathlib.Path) or isinstance(python_val, str):
            source_path = str(python_val)
            if issubclass(python_type, FlyteFile):
                self.validate_file_type(python_type, source_path)
                if ctx.file_access.is_remote(source_path):
                    should_upload = False
                else:
                    if isinstance(python_val, pathlib.Path) and not python_val.is_file():
                        raise ValueError(f"Error converting pathlib.Path {python_val} because it's not a file.")

                    # If it's a string pointing to a local destination, then make sure it's a file.
                    if isinstance(python_val, str):
                        p = pathlib.Path(python_val)
                        if not p.is_file():
                            raise TypeTransformerFailedError(f"Error converting {python_val} because it's not a file.")
                        if ctx.execution_state.is_local_execution():
                            should_upload = False
            # python_type must be os.PathLike - see check at beginning of function
            else:
                should_upload = False

        else:
            raise TypeTransformerFailedError(f"Expected FlyteFile or os.PathLike object, received {type(python_val)}")

        # If we're uploading something, that means that the uri should always point to the upload destination.
        if should_upload:
            headers = self.get_additional_headers(source_path)
            if remote_path is not None:
                remote_path = await ctx.file_access.async_put_data(
                    source_path, remote_path, is_multipart=False, **headers
                )
            else:
                remote_path = await ctx.file_access.async_put_raw_data(source_path, **headers)
            # If the source path is a local file, the remote path will be a remote storage path.
            return Literal(
                scalar=Scalar(blob=Blob(metadata=meta, uri=unquote(str(remote_path)))),
                metadata=getattr(python_val, "metadata", None),
            )
        # If not uploading, then we can only take the original source path as the uri.
        else:
            return Literal(
                scalar=Scalar(blob=Blob(metadata=meta, uri=source_path)), metadata=getattr(python_val, "metadata", None)
            )

    @staticmethod
    def get_additional_headers(source_path: str | os.PathLike) -> typing.Dict[str, str]:
        if str(source_path).endswith(".gz"):
            return {"ContentEncoding": "gzip"}
        return {}

    def dict_to_flyte_file(
        self, dict_obj: typing.Dict[str, str], expected_python_type: typing.Union[typing.Type[FlyteFile], os.PathLike]
    ) -> FlyteFile:
        path = dict_obj.get("path", None)
        metadata = dict_obj.get("metadata", None)

        if path is None:
            raise ValueError("FlyteFile's path should not be None")

        return self.to_python_value(
            FlyteContextManager.current_context(),
            Literal(
                scalar=Scalar(
                    blob=Blob(
                        metadata=BlobMetadata(
                            type=_core_types.BlobType(
                                format="", dimensionality=_core_types.BlobType.BlobDimensionality.SINGLE
                            )
                        ),
                        uri=path,
                    )
                ),
                metadata=metadata,
            ),
            expected_python_type,
        )

    def from_binary_idl(
        self, binary_idl_object: Binary, expected_python_type: typing.Union[typing.Type[FlyteFile], os.PathLike]
    ) -> FlyteFile:
        """
        If the input is from flytekit, the Life Cycle will be as follows:

        Life Cycle:
        binary IDL                 -> resolved binary         -> bytes                   -> expected Python object
        (flytekit customized          (propeller processing)     (flytekit binary IDL)      (flytekit customized
        serialization)                                                                       deserialization)

        Example Code:
            @dataclass
            class DC:
                ff: FlyteFile

            @workflow
            def wf(dc: DC):
                t_ff(dc.ff)

        Note:
        - The deserialization is the same as put a flyte file in a dataclass, which will deserialize by the mashumaro's API.

        Related PR:
        - Title: Override Dataclass Serialization/Deserialization Behavior for FlyteTypes via Mashumaro
        - Link: https://github.com/flyteorg/flytekit/pull/2554
        """
        if binary_idl_object.tag == MESSAGEPACK:
            python_val = msgpack.loads(binary_idl_object.value)
            return self.dict_to_flyte_file(dict_obj=python_val, expected_python_type=expected_python_type)
        else:
            raise TypeTransformerFailedError(f"Unsupported binary format: `{binary_idl_object.tag}`")

    def from_generic_idl(
        self, generic: Struct, expected_python_type: typing.Union[typing.Type[FlyteFile], os.PathLike]
    ) -> FlyteFile:
        """
        If the input is from Flyte Console, the Life Cycle will be as follows:

        Life Cycle:
        json str            -> protobuf struct         -> resolved protobuf struct   -> expected Python object
        (console user input)   (console output)           (propeller)                   (flytekit customized deserialization)

        Example Code:
        @dataclass
        class DC:
            ff: FlyteFile

        @workflow
        def wf(dc: DC):
            t_ff(dc.ff)

        Note:
        - The deserialization is the same as put a flyte file in a dataclass, which will deserialize by the mashumaro's API.

        Related PR:
        - Title: Override Dataclass Serialization/Deserialization Behavior for FlyteTypes via Mashumaro
        - Link: https://github.com/flyteorg/flytekit/pull/2554
        """
        json_str = _json_format.MessageToJson(generic)
        python_val = json.loads(json_str)
        return self.dict_to_flyte_file(dict_obj=python_val, expected_python_type=expected_python_type)

    async def async_to_python_value(
        self, ctx: FlyteContext, lv: Literal, expected_python_type: typing.Union[typing.Type[FlyteFile], os.PathLike]
    ) -> FlyteFile:
        # Handle dataclass attribute access
        if lv.scalar:
            if lv.scalar.binary:
                return self.from_binary_idl(lv.scalar.binary, expected_python_type)
            if lv.scalar.generic:
                return self.from_generic_idl(lv.scalar.generic, expected_python_type)

        try:
            uri = lv.scalar.blob.uri
            metadata = lv.metadata
        except AttributeError:
            raise TypeTransformerFailedError(f"Cannot convert from {lv} to {expected_python_type}")

        if lv.scalar.blob.metadata.type.dimensionality != BlobType.BlobDimensionality.SINGLE:
            raise TypeTransformerFailedError(f"{lv.scalar.blob.uri} is not a file.")

        if not ctx.file_access.is_remote(uri) and not os.path.isfile(uri):
            raise FlyteAssertion(
                f"Cannot convert from {lv} to {expected_python_type}. " f"Expected a file, but {uri} is not a file."
            )

        # In this condition, we still return a FlyteFile instance, but it's a simple one that has no downloading tricks
        # Using is instead of issubclass because FlyteFile does actually subclass it
        if expected_python_type is os.PathLike:
            return FlyteFile(path=uri, metadata=metadata)

        # Correctly handle `Annotated[FlyteFile, ...]` by extracting the origin type
        expected_python_type = get_underlying_type(expected_python_type)

        # The rest of the logic is only for FlyteFile types.
        if not issubclass(expected_python_type, FlyteFile):  # type: ignore
            raise TypeError(f"Neither os.PathLike nor FlyteFile specified {expected_python_type}")

        # This is a local file path, like /usr/local/my_file, don't mess with it. Certainly, downloading it doesn't
        # make any sense.
        if not ctx.file_access.is_remote(uri):
            return expected_python_type(path=uri, metadata=metadata)  # type: ignore

        # For the remote case, return an FlyteFile object that can download
        local_path = ctx.file_access.get_random_local_path(uri)

        _downloader = partial(ctx.file_access.get_data, remote_path=uri, local_path=local_path, is_multipart=False)

        expected_format = FlyteFilePathTransformer.get_format(expected_python_type)
        ff = FlyteFile.__class_getitem__(expected_format)(path=local_path, downloader=_downloader, metadata=metadata)
        ff._remote_source = uri
        return ff

    def guess_python_type(self, literal_type: LiteralType) -> typing.Type[FlyteFile[typing.Any]]:
        if (
            literal_type.blob is not None
            and literal_type.blob.dimensionality == BlobType.BlobDimensionality.SINGLE
            and literal_type.blob.format != FlytePickleTransformer.PYTHON_PICKLE_FORMAT
        ):
            return FlyteFile.__class_getitem__(literal_type.blob.format)

        raise ValueError(f"Transformer {self} cannot reverse {literal_type}")


TypeEngine.register(FlyteFilePathTransformer(), additional_types=[os.PathLike])
