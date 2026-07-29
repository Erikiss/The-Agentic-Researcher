from __future__ import annotations

import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest

import agentic_researcher.secure_artifacts as secure_artifacts
from agentic_researcher.secure_artifacts import (
    UnsafeArchiveError,
    safe_extract_age_archive,
    safe_extract_tar,
)


def write_archive(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> None:
    with tarfile.open(path, mode="w:gz") as handle:
        for member, content in members:
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))


def archive_bytes(members: list[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as handle:
        for member, content in members:
            member.size = len(content)
            handle.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def regular(name: str, content: bytes = b"data") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), content


class FakeProcess:
    def __init__(
        self,
        output: bytes,
        return_code: int,
        *,
        already_exited: bool = True,
    ) -> None:
        self.stdout = io.BytesIO(output)
        self.return_code = return_code
        self.already_exited = already_exited
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.return_code if self.already_exited else None

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        self.already_exited = True
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = -15
        self.already_exited = True

    def kill(self) -> None:
        self.killed = True
        self.return_code = -9
        self.already_exited = True


class BlockingOutput:
    def __init__(self) -> None:
        self.released = threading.Event()

    def read(self, size: int = -1) -> bytes:
        del size
        self.released.wait(timeout=5)
        return b""

    def close(self) -> None:
        self.released.set()

    def __enter__(self) -> BlockingOutput:
        return self

    def __exit__(self, *args) -> None:
        del args
        self.close()


def find_age_tool(name: str) -> str | None:
    found = shutil.which(name)
    if found is not None:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        executable = (
            Path(local_app_data)
            / "Microsoft"
            / "WinGet"
            / "Links"
            / f"{name}.exe"
        )
        if executable.is_file():
            return str(executable)
    return None


def test_safe_extract_writes_regular_discord_export(tmp_path: Path) -> None:
    archive = tmp_path / "export.tar.gz"
    write_archive(
        archive,
        [
            regular("discord_exports/ingest_bundle.json", b'{"ok": true}'),
            regular("discord_exports/curation_media/image.png", b"png"),
        ],
    )

    destination = tmp_path / "result"
    extracted = safe_extract_tar(archive, destination)

    assert len(extracted) == 2
    assert (destination / "discord_exports" / "ingest_bundle.json").read_bytes() == (
        b'{"ok": true}'
    )
    assert (
        destination / "discord_exports" / "curation_media" / "image.png"
    ).read_bytes() == b"png"


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "../escape.txt",
        "discord_exports/../../escape.txt",
        r"discord_exports\..\escape.txt",
        "/absolute.txt",
        r"C:\absolute.txt",
        "discord_exports/CON",
        "discord_exports/trailing.",
    ],
)
def test_safe_extract_rejects_unsafe_paths(
    tmp_path: Path, unsafe_name: str
) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    write_archive(archive, [regular(unsafe_name)])
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError):
        safe_extract_tar(archive, destination)

    assert not destination.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_links(tmp_path: Path) -> None:
    archive = tmp_path / "link.tar.gz"
    link = tarfile.TarInfo("discord_exports/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../escape"
    write_archive(archive, [(link, b"")])

    with pytest.raises(UnsafeArchiveError, match="not a regular file"):
        safe_extract_tar(archive, tmp_path / "result")


@pytest.mark.parametrize(
    "member_type",
    [
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
        tarfile.CHRTYPE,
        tarfile.BLKTYPE,
        tarfile.FIFOTYPE,
    ],
)
def test_safe_extract_rejects_all_link_device_and_fifo_types(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    archive = tmp_path / "special.tar.gz"
    member = tarfile.TarInfo("discord_exports/special")
    member.type = member_type
    member.linkname = "discord_exports/target"
    write_archive(archive, [(member, b"")])
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError, match="not a regular file"):
        safe_extract_tar(archive, destination)

    assert not destination.exists()


def test_safe_extract_rejects_case_insensitive_duplicates(tmp_path: Path) -> None:
    archive = tmp_path / "duplicate.tar.gz"
    write_archive(
        archive,
        [
            regular("discord_exports/File.json", b"one"),
            regular("discord_exports/file.json", b"two"),
        ],
    )

    with pytest.raises(UnsafeArchiveError, match="duplicate"):
        safe_extract_tar(archive, tmp_path / "result")


def test_safe_extract_rejects_unicode_normalization_duplicates(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "unicode-duplicate.tar.gz"
    write_archive(
        archive,
        [
            regular("discord_exports/Caf\u00e9.json", b"one"),
            regular("discord_exports/Cafe\u0301.json", b"two"),
        ],
    )
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError, match="duplicate"):
        safe_extract_tar(archive, destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "members, message",
    [
        (
            [
                regular("discord_exports/topic", b"file"),
                regular("discord_exports/topic/item.json", b"child"),
            ],
            "nested below a file",
        ),
        (
            [
                regular("discord_exports/topic/item.json", b"child"),
                regular("discord_exports/topic", b"file"),
            ],
            "shadows an existing directory",
        ),
    ],
)
def test_safe_extract_rejects_file_directory_collisions(
    tmp_path: Path,
    members: list[tuple[tarfile.TarInfo, bytes]],
    message: str,
) -> None:
    archive = tmp_path / "collision.tar.gz"
    write_archive(archive, members)
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError, match=message):
        safe_extract_tar(archive, destination)

    assert not destination.exists()


@pytest.mark.parametrize(
    "member_name",
    [
        "Discord_exports/file.json",
        "discord_Exports/file.json",
        "discord_exports_extra/file.json",
    ],
)
def test_required_root_is_exact_and_case_sensitive(
    tmp_path: Path,
    member_name: str,
) -> None:
    archive = tmp_path / "wrong-root.tar.gz"
    write_archive(archive, [regular(member_name)])
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError, match="outside required root"):
        safe_extract_tar(
            archive,
            destination,
            required_root="discord_exports",
        )

    assert not destination.exists()


def test_required_root_accepts_an_exact_descendant(tmp_path: Path) -> None:
    archive = tmp_path / "exact-root.tar.gz"
    write_archive(
        archive,
        [regular("discord_exports/ingest_bundle.json", b"{}")],
    )
    destination = tmp_path / "result"

    safe_extract_tar(
        archive,
        destination,
        required_root="discord_exports",
    )

    assert (destination / "discord_exports" / "ingest_bundle.json").is_file()


def test_required_root_must_be_one_safe_component(tmp_path: Path) -> None:
    archive = tmp_path / "export.tar.gz"
    write_archive(archive, [regular("discord_exports/file.json")])

    with pytest.raises(ValueError, match="one exact path component"):
        safe_extract_tar(
            archive,
            tmp_path / "result",
            required_root="discord_exports/nested",
        )


def test_safe_extract_enforces_member_count_limit(tmp_path: Path) -> None:
    archive = tmp_path / "many.tar.gz"
    write_archive(
        archive,
        [
            regular("discord_exports/one.json"),
            regular("discord_exports/two.json"),
        ],
    )
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError, match="more than 1 members"):
        safe_extract_tar(archive, destination, max_members=1)

    assert not destination.exists()


def test_safe_extract_enforces_member_size_limit(tmp_path: Path) -> None:
    archive = tmp_path / "large-member.tar.gz"
    write_archive(
        archive,
        [regular("discord_exports/data.bin", b"12345")],
    )
    destination = tmp_path / "result"

    with pytest.raises(UnsafeArchiveError, match="exceeds 4 bytes"):
        safe_extract_tar(
            archive,
            destination,
            max_member_bytes=4,
        )

    assert not destination.exists()


def test_safe_extract_enforces_total_size_limit(tmp_path: Path) -> None:
    archive = tmp_path / "large.tar.gz"
    write_archive(archive, [regular("discord_exports/data.bin", b"12345")])

    with pytest.raises(UnsafeArchiveError, match="expands beyond"):
        safe_extract_tar(
            archive,
            tmp_path / "result",
            max_total_bytes=4,
        )


def test_safe_extract_requires_new_destination(tmp_path: Path) -> None:
    archive = tmp_path / "export.tar.gz"
    write_archive(archive, [regular("discord_exports/file.txt")])
    destination = tmp_path / "result"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        safe_extract_tar(archive, destination)


def test_age_nonzero_exit_removes_destination_and_bounds_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    encrypted.write_bytes(b"ciphertext")
    identity.write_text("identity", encoding="utf-8")
    destination = tmp_path / "result"
    payload = archive_bytes(
        [regular("discord_exports/ingest_bundle.json", b"{}")]
    )

    def fake_popen(*args, **kwargs) -> FakeProcess:
        del args
        error_stream = kwargs["stderr"]
        error_stream.write(b"x" * 100_000 + b"\ndecryption failed")
        return FakeProcess(payload, return_code=23)

    monkeypatch.setattr(secure_artifacts.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="exit code 23") as raised:
        safe_extract_age_archive(encrypted, identity, destination)

    assert "decryption failed" in str(raised.value)
    assert len(str(raised.value)) < 2_100
    assert not destination.exists()


def test_corrupt_age_stream_removes_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    encrypted.write_bytes(b"ciphertext")
    identity.write_text("identity", encoding="utf-8")
    destination = tmp_path / "result"
    process = FakeProcess(b"not a gzip stream", return_code=0)

    monkeypatch.setattr(
        secure_artifacts.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises((tarfile.ReadError, gzip.BadGzipFile, EOFError)):
        safe_extract_age_archive(encrypted, identity, destination)

    assert not destination.exists()


def test_truncated_gzip_footer_is_detected_and_cleaned_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    encrypted.write_bytes(b"ciphertext")
    identity.write_text("identity", encoding="utf-8")
    destination = tmp_path / "result"
    payload = archive_bytes(
        [regular("discord_exports/ingest_bundle.json", b"{}")]
    )
    process = FakeProcess(payload[:-4], return_code=0)

    monkeypatch.setattr(
        secure_artifacts.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises((tarfile.ReadError, gzip.BadGzipFile, EOFError)):
        safe_extract_age_archive(encrypted, identity, destination)

    assert not destination.exists()


def test_stream_failure_terminates_running_age_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    encrypted.write_bytes(b"ciphertext")
    identity.write_text("identity", encoding="utf-8")
    destination = tmp_path / "result"
    process = FakeProcess(
        b"not a gzip stream",
        return_code=0,
        already_exited=False,
    )

    monkeypatch.setattr(
        secure_artifacts.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises((tarfile.ReadError, gzip.BadGzipFile, EOFError)):
        safe_extract_age_archive(encrypted, identity, destination)

    assert process.terminated
    assert not destination.exists()


def test_age_timeout_releases_blocked_output_and_removes_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    encrypted.write_bytes(b"ciphertext")
    identity.write_text("identity", encoding="utf-8")
    destination = tmp_path / "result"
    process = FakeProcess(b"", return_code=0, already_exited=False)
    blocked_output = BlockingOutput()
    process.stdout = blocked_output

    monkeypatch.setattr(
        secure_artifacts.subprocess,
        "Popen",
        lambda *args, **kwargs: process,
    )

    with pytest.raises(TimeoutError, match="exceeded"):
        safe_extract_age_archive(
            encrypted,
            identity,
            destination,
            age_timeout_seconds=0.05,
        )

    assert process.killed
    assert blocked_output.released.is_set()
    assert not destination.exists()


def test_real_age_round_trip_when_tools_are_available(tmp_path: Path) -> None:
    age = find_age_tool("age")
    age_keygen = find_age_tool("age-keygen")
    if age is None or age_keygen is None:
        pytest.skip("age and age-keygen are not available")

    archive = tmp_path / "export.tar.gz"
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    destination = tmp_path / "result"
    write_archive(
        archive,
        [regular("discord_exports/ingest_bundle.json", b'{"ok": true}')],
    )
    try:
        subprocess.run(
            [age_keygen, "--output", str(identity)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("age-keygen did not complete within the bounded test timeout")
    match = re.search(
        r"age1[0-9a-z]+",
        identity.read_text(encoding="utf-8"),
    )
    assert match is not None
    try:
        subprocess.run(
            [
                age,
                "--encrypt",
                "--recipient",
                match.group(0),
                "--output",
                str(encrypted),
                str(archive),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("age did not complete within the bounded test timeout")

    extracted = safe_extract_age_archive(
        encrypted,
        identity,
        destination,
        age_executable=age,
    )

    assert len(extracted) == 1
    assert (
        destination / "discord_exports" / "ingest_bundle.json"
    ).read_bytes() == b'{"ok": true}'


def test_age_process_start_failure_removes_destination(tmp_path: Path) -> None:
    encrypted = tmp_path / "export.tar.gz.age"
    identity = tmp_path / "identity.txt"
    encrypted.write_bytes(b"not needed")
    identity.write_text("not needed", encoding="utf-8")
    destination = tmp_path / "result"

    with pytest.raises(FileNotFoundError):
        safe_extract_age_archive(
            encrypted,
            identity,
            destination,
            age_executable=str(tmp_path / "missing-age-executable"),
        )

    assert not destination.exists()
