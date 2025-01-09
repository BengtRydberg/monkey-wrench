"""The module providing the class for querying the EUMETSAT API."""

import fnmatch
import shutil
import time
from datetime import datetime, timedelta
from os import environ
from pathlib import Path
from typing import ClassVar, Generator

from eumdac import AccessToken, DataStore, DataTailor
from eumdac.collection import SearchResults
from eumdac.product import Product
from eumdac.tailor_models import Chain, RegionOfInterest
from fsspec import open_files
from loguru import logger
from pydantic import ConfigDict, PositiveInt, validate_call
from satpy.readers.utils import FSFile

from monkey_wrench.date_time import (
    assert_start_time_is_before_end_time,
    floor_datetime_minutes_to_specific_snapshots,
)
from monkey_wrench.generic import Order
from monkey_wrench.query._base import Query
from monkey_wrench.query._common import seviri_collection_url
from monkey_wrench.query._types import BoundingBox, EumetsatCollection, Polygon


class EumetsatAPI(Query):
    """A class with utilities to simplify querying all the product IDs from the EUMETSAT API.

    Note:
        This is basically a wrapper around `eumdac`_. However, it does not expose all the functionalities of the
        `eumdac`, only the few ones that we need!

    .. _eumdac: https://user.eumetsat.int/resources/user-guides/eumetsat-data-access-client-eumdac-guide
    """

    # The following does not include any login credentials, therefore we suppress Ruff linter rule S106.
    credentials_env_vars: ClassVar[dict[str, str]] = dict(
        login="EUMETSAT_API_LOGIN",  # noqa: S106
        password="EUMETSAT_API_PASSWORD"  # noqa: S106
    )

    """The keys of environment variables used to authenticate the EUMETSAT API calls.

    Example:
        On Linux, you can use the ``export`` command to set the credentials in a terminal,

        .. code-block:: bash

            export EUMETSAT_API_LOGIN=<login>;
            export EUMETSAT_API_PASSWORD=<password>;
    """

    @validate_call
    def __init__(
            self, collection: EumetsatCollection = EumetsatCollection.seviri, log_context: str = "EUMETSAT API"
    ) -> None:
        """Initialize an instance of the class with API credentials read from the environment variables.

        This constructor method sets up a private `eumdac` datastore by obtaining an authentication token using the
        provided API ``login`` and ``password`` which are read from the environment variables.

        Args:
            collection:
                The collection, defaults to :obj:`~monkey_wrench.query._types.EumetsatCollection.seviri` for SEVIRI.
            log_context:
                A string that will be used in log messages to determine the context. Defaults to an empty string.

        Note:
            See `API key management`_ on the `eumdac` website for more information.

        .. _API key management: https://api.eumetsat.int/api-key
        """
        super().__init__(log_context=log_context)
        token = EumetsatAPI.get_token()
        self.__collection = collection
        self.__data_store = DataStore(token)
        self.__data_tailor = DataTailor(token)
        self.__selected_collection = self.__data_store.get_collection(collection.value.query_string)

    @classmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def get_token(cls) -> AccessToken:
        """Get a token using the :obj:`credentials_env_vars`.

        This method returns the same token if it is still valid and issues a new one otherwise.

        Returns:
            A token using which the datastore can be accessed.
        """
        try:
            credentials = tuple(environ[cls.credentials_env_vars[key]] for key in ["login", "password"])
        except KeyError as error:
            raise KeyError(f"Please set the environment variable {error}.") from None

        token = AccessToken(credentials)

        token_str = str(token)
        token_str = token_str[:3] + " ... " + token_str[-3:]

        logger.info(f"Accessing token '{token_str}' issued at {datetime.now()} and expires {token.expiration}.")
        return token

    def len(self, product_ids: SearchResults) -> int:
        """Return the number of product IDs."""
        return product_ids.total_results

    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def query(
            self,
            start_datetime: datetime,
            end_datetime: datetime,
            polygon: Polygon | None = None,
    ) -> SearchResults:
        """Query product IDs in a single batch.

        This method wraps around the ``eumdac.Collection().search()`` method to perform a search for product IDs
        within a specified time range and the polygon.

        Note:
            For a given SEVIRI collection, an example product ID is
            ``"MSG3-SEVI-MSG15-0100-NA-20150731221240.036000000Z-NA"``.

        Note:
            The keyword arguments of ``start_time`` and ``end_time`` are treated respectively as inclusive and exclusive
            when querying the IDs. For example, to obtain all the data up to and including ``2022/12/31``, we must set
            ``end_time=datetime(2023, 1, 1)``.

        Args:
            start_datetime:
                The start datetime (inclusive).
            end_datetime:
                The end datetime (exclusive).
            polygon:
                An object of type :class:`~monkey_wrench.query._types.Polygon`.

        Returns:
            The results of the search, containing the product IDs found within the specified time range and the polygon.

        Raises:
            ValueError:
                Refer to :func:`~monkey_wrench.date_time.assert_start_time_is_before_end_time`.
        """
        assert_start_time_is_before_end_time(start_datetime, end_datetime)
        end_datetime = floor_datetime_minutes_to_specific_snapshots(
            end_datetime, self.__collection.value.snapshot_minutes
        )
        return self.__selected_collection.search(
            dtstart=start_datetime, dtend=end_datetime, geo=str(polygon) if polygon else None
        )

    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def query_in_batches(
            self,
            start_datetime: datetime = datetime(2022, 1, 1),
            end_datetime: datetime = datetime(2023, 1, 1),
            batch_interval: timedelta = timedelta(days=30),
    ) -> Generator[tuple[SearchResults, int], None, None]:
        """Retrieve all the product IDs, given a time range and a batch interval, fetching one batch at a time.

        Args:
            start_datetime:
                The start of the datetime range for querying (inclusive). Defaults to January 1, 2022.
            end_datetime:
                The end of the datetime range for querying (exclusive). Defaults to January 1, 2023.
            batch_interval:
                The duration of each batch interval. Defaults to ``30`` days. A smaller value for ``batch_interval``
                means a larger number of batches which increases the overall time needed to fetch all the product IDs.
                A larger value for ``batch_interval`` shortens the total time to fetch all the IDs, however, you might
                get an error regarding sending `too many requests` to the server.

        Note:
            An example, for SEVIRI, we expect to have one file (product ID) per ``15`` minutes, i.e. ``4`` files per
            hour or ``96`` files per day. If our re-analysis period is ``2022/01/01`` (inclusive) to ``2023/01/01``
            (exclusive), i.e. ``365`` days. This results in a maximum of ``35040`` files.

            If we split our datetime range into intervals of ``30`` days and fetch product IDs in batches,
            there is a maximum of ``2880 = 96 x 30`` IDs in each batch retrieved by a single request. One might need to
            adapt this value to avoid running into the issue of sending `too many requests` to the server.

        Yields:
            A generator of 2-tuples. The first element of each tuple is the collection of products retrieved in that
            batch. The second element is the number of the retrieved products for that batch. The search results can be
            in turn iterated over to retrieve individual products.

        Example:
            >>> from datetime import datetime, timedelta
            >>> from monkey_wrench.query import EumetsatAPI
            >>>
            >>> start_datetime = datetime(2022, 1, 1)
            >>> end_datetime = datetime(2022, 1, 3)
            >>> batch_interval = timedelta(days=1)
            >>> api = EumetsatAPI()
            >>> for batch, retrieved_count in api.query_in_batches(start_datetime, end_datetime, batch_interval):
            ...     assert retrieved_count == batch.total_results
            ...     print(batch)
            ...     for product in batch:
            ...         print(product)
        """
        expected_total_count = self.len(self.query(start_datetime, end_datetime))
        yield from super().query_in_batches(
            start_datetime,
            end_datetime,
            batch_interval,
            order=Order.descending,
            expected_total_count=expected_total_count
        )

    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def fetch_products(
            self,
            search_results,  # TODO: When adding `SearchResults` as the type, making the documentation fails!
            output_directory: Path,
            bounding_box: BoundingBox = (90., -90, -180., 180.),
            output_file_format: str = "netcdf4",
            sleep_time: PositiveInt = 10
    ) -> list[Path | None]:
        """Fetch all products of a search results and write product files to disk.

        Args:
            search_results:
                Search results for which the files will be fetched.
            output_directory:
                The directory to save the files in.
            bounding_box:
                Bounding box, i.e. (north, south, west, east) limits.
            output_file_format:
                Desired format of the output file(s). Defaults to ``netcdf4``.
            sleep_time:
                Sleep time, in seconds, between requests. Defaults to ``10`` seconds.

        Returns:
            A list paths for the fetched files.
        """
        if not output_directory.exists():
            output_directory.mkdir(parents=True, exist_ok=True)

        chain = Chain(
            product=search_results.collection.product_type,
            format=output_file_format,
            roi=RegionOfInterest(NSWE=bounding_box)
        )
        return [self.fetch_product(product, chain, output_directory, sleep_time) for product in search_results]

    def fetch_product(
            self,
            product: Product,
            chain: Chain,
            output_directory: Path,
            sleep_time: PositiveInt
    ) -> Path | None:
        """Fetch the file for a single product and write the product file to disk.

        Args:
            product:
                The Product whose corresponding file will be fetched.
            chain:
                Chain to apply for customization of the output file.
            output_directory:
                 The directory to save the file in.ort EumetsatAPI
            sleep_time:
                Sleep time, in seconds, between requests.

        Returns:
            The path of the saved file on the disk, Otherwise ``None`` in case of a failure.
        """
        customisation = self.__data_tailor.new_customisation(product, chain)
        logger.info(f"Start downloading product {str(product)}")
        while True:
            if "DONE" in customisation.status:
                customized_file = fnmatch.filter(customisation.outputs, "*.nc")[0]
                with (
                    customisation.stream_output(customized_file) as stream,
                    open(output_directory / stream.name, mode="wb") as fdst
                ):
                    shutil.copyfileobj(stream, fdst)
                    logger.info(f"Wrote file: {fdst.name}' to disk.")
                    return Path(output_directory / stream.name)
            elif customisation.status in ["ERROR", "FAILED", "DELETED", "KILLED", "INACTIVE"]:
                logger.warning(f"Job failed, error code is: '{customisation.status.lower()}'.")
                return None
            elif customisation.status in ["QUEUED", "RUNNING"]:
                logger.info(f"Job is {customisation.status.lower()}.")
                time.sleep(sleep_time)

    @staticmethod
    @validate_call(config=ConfigDict(arbitrary_types_allowed=True))
    def open_seviri_native_file_remotely(product_id: str, cache: str | None = None) -> FSFile:
        """Open SEVIRI native files (``.nat``) remotely, inside a zip archive using the given product ID.

        Note:
            See `fsspec cache <fs>`_, to learn more about buffering and random access in `fsspec`.

        Args:
            product_id:
                The product ID to open.
            cache:
                How to buffer, e.g. ``"filecache"``, ``"blockcache"``, or ``None``. Defaults to ``None``.

        Returns:
            A file object of type ``FSFile``, which can be further used by ``satpy``.

        .. _fs: https://filesystem-spec.readthedocs.io/en/latest/features.html#file-buffering-and-random-access
        """
        https_header = {
            "encoded": True,
            "client_kwargs": {
                "headers": {
                    "Authorization": f"Bearer {EumetsatAPI.get_token()}",
                }
            }
        }
        cache_str = f"::{cache}" if cache else ""
        fstr = f"zip://*.nat{cache_str}::{seviri_collection_url()}/{product_id}"
        logger.info(f"Opening {fstr}")
        return [FSFile(f) for f in open_files(fstr, https=https_header)][0]
