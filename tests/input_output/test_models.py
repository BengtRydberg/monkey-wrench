import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from monkey_wrench import input_output
from monkey_wrench.date_time import DateTimeRange, FilePathParser, SeviriIDParser
from monkey_wrench.generic import Pattern
from monkey_wrench.input_output import (
    DateTimeDirectory,
    DirectoryVisitor,
    FilesIntegrityValidator,
    FsSpecCache,
    Reader,
    TempDirectory,
    Writer,
)
from tests.utils import EnvironmentVariables, make_dummy_datetime_files, make_dummy_file, make_dummy_files

start_datetime = datetime(2022, 1, 1, 0, 12, tzinfo=UTC)
end_datetime = datetime(2022, 1, 4, tzinfo=UTC)
batch_interval = timedelta(hours=1)
number_of_days = (end_datetime - start_datetime).days


# ======================================================
### Tests for DirectoryVisitor()

@pytest.mark.parametrize("reverse", [
    True, False
])
@pytest.mark.parametrize("recursive", [
    True, False
])
@pytest.mark.parametrize("pattern", [
    ".nc", ".", "nc", [".", "nc"], None, "", "2022", "non_existent_pattern"
])
def test_DirectoryVisitor(temp_dir, reverse, pattern, recursive):
    output_filepath = temp_dir / Path("output.txt")
    datetime_objects, _ = _make_dummy_datetime_files(temp_dir, reverse)
    top_level_files, _, _ = make_dummy_files(temp_dir, prefix="top_level_files_2022.nc")
    files = DirectoryVisitor(
        parent_directory=temp_dir,
        reverse=reverse,
        recursive=recursive,
        sub_strings=pattern,
        output_filepath=output_filepath
    ).visit()

    if output_filepath.exists():
        items = Reader(input_filepath=output_filepath).read()
        assert set(items) == {str(f) for f in files}

    if pattern == "non_existent_pattern":
        assert len(files) == 0
    else:
        if recursive:
            assert len(datetime_objects) + len(top_level_files) == len(files)
            files = [f for f in files if "top_level" not in str(f)]
            _assert_filenames_match_filenames_from_datetime(files, datetime_objects)
        else:
            assert top_level_files == set(files)


def test_DirectoryVisitor_callback(temp_dir):
    buff = []
    _, dummy_files = _make_dummy_datetime_files(temp_dir)
    files = DirectoryVisitor(parent_directory=temp_dir, callback=lambda x: buff.append(x)).visit()

    assert set(files) == set(buff)
    assert set(dummy_files) == set(buff)


def _assert_datetime_directories_exist(temp_dir):
    for i in range(1, number_of_days + 1):
        assert os.path.exists(temp_dir / Path(f"2022/01/0{i}"))


def _assert_filenames_match_filenames_from_datetime(files, datetime_objects):
    for file, datetime_ in zip(files, datetime_objects, strict=True):
        assert f"{file.stem}.nc" == str(input_output.seviri.input_filename_from_datetime(datetime_))


def _make_dummy_datetime_files(temp_dir, reverse=False):
    datetime_objs = list(
        DateTimeRange(start_datetime=start_datetime, end_datetime=end_datetime, interval=batch_interval))
    if reverse:
        datetime_objs = datetime_objs[::-1]
    files = make_dummy_datetime_files(datetime_objs, temp_dir)
    _assert_datetime_directories_exist(temp_dir)
    return datetime_objs, files


# ======================================================
### Tests for copy_files_between_directories()

@pytest.mark.parametrize("pattern", [
    ""
    "file_for_test_"
])
def test_copy_files_between_directories(temp_dir, pattern):
    dest_directory = _make_dummy_files_for_copy(temp_dir, pattern)
    input_output.copy_files_between_directories(temp_dir, dest_directory, pattern=Pattern(sub_strings=pattern))

    assert 4 == len(DirectoryVisitor(parent_directory=dest_directory).visit())
    assert 3 == len(DirectoryVisitor(parent_directory=dest_directory, sub_strings=pattern).visit())


def _make_dummy_files_for_copy(temp_dir, pattern):
    make_dummy_files(temp_dir, prefix=pattern)
    dest_directory = Path(temp_dir, "dest_directory")
    os.makedirs(dest_directory, exist_ok=True)
    make_dummy_file(dest_directory / "excluded.ex")
    return dest_directory


# ======================================================
### Tests for FilesIntegrityValidator()

@pytest.mark.parametrize(("expected", "keys"), [
    (
            ("expected_missing", "expected_corrupted"),
            ("reference", "nominal_size", "tolerance", "number_of_processes")
    ),
    (
            (None, None),
            ("number_of_processes",)
    ),
    (
            ("expected_missing", None),
            ("reference", "number_of_processes")
    ),
    (
            (None, "expected_corrupted"),
            ("nominal_size", "tolerance", "number_of_processes")
    ),
])
def test_compare_files_against_reference(dummy_and_reference_files_for_comparison, expected, keys):
    collected_files, files_information, temp_dir = dummy_and_reference_files_for_comparison

    kwargs = {k: files_information[k] for k in keys}

    file_size_validator = FilesIntegrityValidator(
        **{k: kwargs.pop(k) for k in {"number_of_processes", "nominal_size", "tolerance", "reference"} if k in kwargs}
    )
    expected_ = tuple(files_information.get(i) for i in expected)
    assert file_size_validator.verify(collected_files) == expected_

    file_size_validator = FilesIntegrityValidator(
        **(file_size_validator.model_dump() | {"reference": DirectoryVisitor(parent_directory=temp_dir)})
    )
    assert [bool(i) for i in file_size_validator.verify(collected_files)] == [False, False]


def test_compare_files_against_reference_transform(temp_dir):
    datetime_objs, collected_files = _make_dummy_datetime_files(temp_dir)

    missing, _ = FilesIntegrityValidator(
        reference=datetime_objs, transform_function=FilePathParser.parse
    ).verify(collected_files)

    assert missing == set()


@pytest.fixture
def dummy_and_reference_files_for_comparison(temp_dir):
    reference_items, expected_missing, expected_corrupted = make_dummy_files(temp_dir, number_of_files_to_remove=3)
    collected_files = DirectoryVisitor(parent_directory=temp_dir).visit()
    items = dict(
        nominal_size=1000,
        tolerance=0.05,
        reference=reference_items,
        expected_missing=expected_missing,
        expected_corrupted=expected_corrupted,
        number_of_processes=1,
        none=None
    )
    return collected_files, items, temp_dir


# ======================================================
### Tests for
#               Writer()
#               Reader()

def test_write_to_file_and_read_from_file(temp_dir):
    product_ids_orig = [
        "20150601045740.599", "20150601044240.763", "20150601042740.925", "20150601041241.084",
        "20150601035741.242", "20150601034241.398", "20150601032741.551", "20150601031239.899",
        "20150601025740.047", "20150601024240.194", "20150601022740.339", "20150601021240.481",
        "20150601015740.621", "20150601014240.760", "20150601012740.897", "20150601011241.032",
        "20150601005739.963", "20150601004240.094", "20150601002740.223", "20150601001240.351"
    ]
    product_ids = [f"MSG3-SEVI-MSG15-0100-NA-{p}000000Z-NA" for p in product_ids_orig]

    p0 = seviri_product_ids_file(temp_dir, 0)
    p1 = seviri_product_ids_file(temp_dir, 1)
    p2 = seviri_product_ids_file(temp_dir, 2)

    Writer(
        output_filepath=p0,
        exceptions=[],
    ).write(product_ids_orig)

    Writer(
        output_filepath=p1,
        exceptions=[],
        transform_function=lambda pid: f"MSG3-SEVI-MSG15-0100-NA-{pid}000000Z-NA"
    ).write(product_ids_orig)

    Writer(
        output_filepath=p2,
        exceptions=[],
    ).write_in_batches(([x], 1) for x in product_ids)

    read_ids_orig = Reader(input_filepath=p0).read()
    read_ids = Reader(input_filepath=p1).read()
    read_ids_batches = Reader(input_filepath=p2).read()
    read_ids_transformed = Reader(input_filepath=p1, transform_function=SeviriIDParser.parse).read()

    assert read_ids_orig == product_ids_orig
    assert read_ids == product_ids
    assert read_ids_batches == product_ids
    assert {SeviriIDParser.parse(i) for i in product_ids} == set(read_ids_transformed)


def seviri_product_ids_file(path, idx):
    return path / Path(f"seviri_product_ids_{idx}.txt")


# ======================================================
### Tests for DateTimeDirectory()

@pytest.mark.parametrize("kwargs", [
    dict(format_string="%Y/%m/%d", dry_run=False),
    dict(format_string="%Y/%m/%d", dry_run=True),
    dict(format_string="%Y-%m/%d", dry_run=False),
    dict(format_string="%Y-%m/%d", dry_run=True),
])
def test_DateTimeDirectory(temp_dir, kwargs):
    datetime_obj = datetime(2022, 3, 12)
    dir_path = DateTimeDirectory(
        parent_directory=temp_dir,
        format_string=kwargs["format_string"]
    ).create(datetime_obj, dry_run=kwargs["dry_run"])
    if not kwargs["dry_run"]:
        assert dir_path.exists()
    assert temp_dir / Path(datetime_obj.strftime(kwargs["format_string"])) == dir_path


def test_DateTimeDirectory_remove(temp_dir):
    with mock.patch("monkey_wrench.input_output._models.Path.unlink") as unlink, \
            mock.patch("monkey_wrench.input_output._models.Path.exists", return_value=True) as exists:
        DateTimeDirectory(
            parent_directory=temp_dir,
            remove_directory_if_exists=True
        ).create(datetime(2022, 3, 12))
        exists.assert_called()
        unlink.assert_called()


# ======================================================
### Tests for TempDirectory()

def test_TempDirectory():
    default_temp_path = tempfile.gettempdir()
    here_path = os.path.abspath(".")
    with TempDirectory(temp_directory=".").context() as tmp:
        assert str(tmp).startswith(here_path)
        with tempfile.TemporaryDirectory() as tmpdir:
            assert tmpdir.startswith(here_path)
    assert default_temp_path == tempfile.gettempdir()


@pytest.mark.parametrize(("tmpdir_factory", "expected"), [
    (lambda x: x, True),
    (lambda x: None, False)
])
def test_TempDirectory_default(temp_dir, tmpdir_factory, expected):
    tmp_directory = Path(temp_dir, "another_temp_dir")
    os.makedirs(tmp_directory, exist_ok=True)

    with EnvironmentVariables(**{"TMPDIR": tmpdir_factory(str(tmp_directory))}):
        with TempDirectory().context() as tmp:
            assert str(tmp).startswith("/tmp")
            assert ("/another_temp_dir/" in str(tmp)) is expected


# ======================================================
### Tests for FsSpecCache()

@pytest.mark.parametrize("fc", [
    "filecache",
    "blockcache",
    None
])
def test_FsSpecCache(fc):
    fsc = FsSpecCache(cache=fc)
    assert fsc.cache_str == (f"::{fc}" if fc else "")
