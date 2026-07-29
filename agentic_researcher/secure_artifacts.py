"""Safe extraction for encrypted Discord Math workflow artifacts."""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import unicodedata
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import BinaryIO, Sequence


DEFAULT_MAX_MEMBERS = 10_000
DEFAULT_MAX_MEMBER_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_AGE_TIMEOUT_SECONDS = 30 * 60
_COPY_CHUNK_BYTES = 1024 * 1024
_ALLOWED_TYPES = {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}
_WINDOWS_INVALID_CHARACTERS = frozenset('<>"|?*')


class UnsafeArchiveError(ValueError):
    """Raised when an archive cannot be extracted without filesystem risk."""


def _safe_member_parts(name: str) -> tuple[str, ...]:
    if not name or "\x00" in name:
        raise UnsafeArchiveError("archive member has an empty or NUL-containing path")

    normalized = name.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
    ):
        raise UnsafeArchiveError(f"archive member uses an absolute path: {name!r}")

    parts = tuple(part for part in posix_path.parts if part not in ("", "."))
    if not parts or any(part == ".." for part in parts):
        raise UnsafeArchiveError(f"archive member escapes the destination: {name!r}")

    for part in parts:
        windows_part = PureWindowsPath(part)
        if (
            ":" in part
            or part.endswith((" ", "."))
            or windows_part.is_reserved()
            or any(character in _WINDOWS_INVALID_CHARACTERS for character in part)
            or any(ord(character) < 32 for character in part)
        ):
            raise UnsafeArchiveError(
                f"archive member is not a safe Windows path: {name!r}"
            )
    return parts


def _normalized_key(parts: tuple[str, ...]) -> str:
    return unicodedata.normalize("NFC", "/".join(parts)).casefold()


def _validate_limits(
    max_members: int,
    max_member_bytes: int,
    max_total_bytes: int,
) -> None:
    if max_members <= 0 or max_member_bytes <= 0 or max_total_bytes <= 0:
        raise ValueError("archive limits must be positive")


def _validate_required_root(required_root: str | None) -> None:
    if required_root is None:
        return
    try:
        parts = _safe_member_parts(required_root)
    except UnsafeArchiveError as error:
        raise ValueError("required root must be one safe path component") from error
    if len(parts) != 1 or parts[0] != required_root:
        raise ValueError("required root must be one exact path component")


def _validate_member(
    member: tarfile.TarInfo,
    *,
    seen: dict[str, bool],
    member_index: int,
    total_bytes: int,
    max_members: int,
    max_member_bytes: int,
    max_total_bytes: int,
    required_root: str | None,
) -> tuple[tuple[str, ...], int]:
    if member_index > max_members:
        raise UnsafeArchiveError(f"archive has more than {max_members} members")
    if member.type not in _ALLOWED_TYPES:
        raise UnsafeArchiveError(
            f"archive member is not a regular file or directory: {member.name!r}"
        )

    parts = _safe_member_parts(member.name)
    if required_root is not None and parts[0] != required_root:
        raise UnsafeArchiveError(
            f"archive member is outside required root {required_root!r}: "
            f"{member.name!r}"
        )

    normalized_key = _normalized_key(parts)
    if normalized_key in seen:
        raise UnsafeArchiveError(
            f"archive contains a duplicate path: {member.name!r}"
        )
    prefixes = [
        _normalized_key(parts[:index]) for index in range(1, len(parts))
    ]
    if any(seen.get(prefix) is False for prefix in prefixes):
        raise UnsafeArchiveError(
            f"archive member is nested below a file: {member.name!r}"
        )
    if member.isfile() and any(
        existing.startswith(normalized_key + "/") for existing in seen
    ):
        raise UnsafeArchiveError(
            f"archive file shadows an existing directory: {member.name!r}"
        )

    if member.size < 0:
        raise UnsafeArchiveError(
            f"archive member has a negative size: {member.name!r}"
        )
    if member.isdir() and member.size:
        raise UnsafeArchiveError(
            f"archive directory has unexpected content: {member.name!r}"
        )
    if member.isfile() and member.size > max_member_bytes:
        raise UnsafeArchiveError(
            f"archive member exceeds {max_member_bytes} bytes: {member.name!r}"
        )
    if member.isfile():
        total_bytes += member.size
        if total_bytes > max_total_bytes:
            raise UnsafeArchiveError(
                f"archive expands beyond {max_total_bytes} bytes"
            )
    seen[normalized_key] = member.isdir()
    return parts, total_bytes


def _copy_exact(source, target, expected_size: int) -> None:
    remaining = expected_size
    while remaining:
        chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
        if not chunk:
            raise UnsafeArchiveError("archive member ended before its declared size")
        target.write(chunk)
        remaining -= len(chunk)


def _remove_destination(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)


def _stop_process(process: subprocess.Popen[bytes]) -> int | None:
    """Stop a child after a stream failure without leaving it behind."""
    natural_return_code = process.poll()
    if natural_return_code is not None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return natural_return_code
        return natural_return_code

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
    return None


def _stderr_tail(stream: BinaryIO, limit: int = 2_000) -> str:
    stream.flush()
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(max(0, length - limit))
    return stream.read().decode("utf-8", errors="replace")


def _start_timeout_guard(
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> tuple[threading.Timer, threading.Event]:
    timed_out = threading.Event()

    def kill_after_timeout() -> None:
        if process.poll() is None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass
            finally:
                if process.stdout is not None:
                    try:
                        process.stdout.close()
                    except OSError:
                        pass

    timer = threading.Timer(timeout_seconds, kill_after_timeout)
    timer.daemon = True
    timer.start()
    return timer, timed_out


def _age_failure(return_code: int, error_text: str) -> RuntimeError:
    detail = error_text.strip()
    suffix = f": {detail}" if detail else ""
    return RuntimeError(
        f"age decryption failed with exit code {return_code}{suffix}"
    )


def safe_extract_tar(
    archive: Path,
    destination: Path,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    required_root: str | None = None,
) -> tuple[Path, ...]:
    """Extract regular files and directories into a new destination.

    The destination must not exist. Every member is validated before any
    content is written, and a failed extraction removes only the newly created
    destination.
    """
    archive = Path(archive).resolve(strict=True)
    destination = Path(destination).resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"extraction destination already exists: {destination}")
    _validate_limits(max_members, max_member_bytes, max_total_bytes)
    _validate_required_root(required_root)

    destination.parent.mkdir(parents=True, exist_ok=True)
    validated: list[tuple[tarfile.TarInfo, tuple[str, ...]]] = []
    seen: dict[str, bool] = {}
    total_bytes = 0

    try:
        with tarfile.open(archive, mode="r:gz") as handle:
            members = handle.getmembers()
            if not members:
                raise UnsafeArchiveError("archive contains no members")
            for index, member in enumerate(members, start=1):
                parts, total_bytes = _validate_member(
                    member,
                    seen=seen,
                    member_index=index,
                    total_bytes=total_bytes,
                    max_members=max_members,
                    max_member_bytes=max_member_bytes,
                    max_total_bytes=max_total_bytes,
                    required_root=required_root,
                )
                validated.append((member, parts))

            destination.mkdir()
            extracted: list[Path] = []
            for member, parts in validated:
                target = destination.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    if not target.is_dir():
                        raise UnsafeArchiveError(
                            f"archive directory collides with a file: {member.name!r}"
                        )
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = handle.extractfile(member)
                    if source is None:
                        raise UnsafeArchiveError(
                            f"archive member has no readable content: {member.name!r}"
                        )
                    with source, target.open("xb") as output:
                        _copy_exact(source, output, member.size)
                extracted.append(target)
    except Exception:
        _remove_destination(destination)
        raise

    return tuple(extracted)


def safe_extract_age_archive(
    encrypted_archive: Path,
    identity: Path,
    destination: Path,
    *,
    age_executable: str = "age",
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    required_root: str = "discord_exports",
    age_timeout_seconds: float = DEFAULT_AGE_TIMEOUT_SECONDS,
) -> tuple[Path, ...]:
    """Decrypt an age-encrypted tar.gz stream directly into a new directory."""
    encrypted_archive = Path(encrypted_archive).resolve(strict=True)
    identity = Path(identity).resolve(strict=True)
    destination = Path(destination).resolve(strict=False)
    if destination.exists():
        raise FileExistsError(f"extraction destination already exists: {destination}")
    _validate_limits(max_members, max_member_bytes, max_total_bytes)
    _validate_required_root(required_root)
    if age_timeout_seconds <= 0:
        raise ValueError("age timeout must be positive")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.mkdir()
    seen: dict[str, bool] = {}
    extracted: list[Path] = []
    total_bytes = 0
    with tempfile.TemporaryFile() as error_stream:
        try:
            process = subprocess.Popen(
                [
                    age_executable,
                    "--decrypt",
                    "--identity",
                    os.fspath(identity),
                    os.fspath(encrypted_archive),
                ],
                stdout=subprocess.PIPE,
                stderr=error_stream,
            )
        except Exception:
            _remove_destination(destination)
            raise
        if process.stdout is None:
            _stop_process(process)
            _remove_destination(destination)
            raise RuntimeError("failed to open age process output stream")

        timeout_guard, timed_out = _start_timeout_guard(
            process,
            age_timeout_seconds,
        )
        stream_error: Exception | None = None
        natural_return_code: int | None = None
        try:
            with process.stdout:
                with gzip.GzipFile(fileobj=process.stdout, mode="rb") as decoded:
                    with tarfile.open(fileobj=decoded, mode="r|") as handle:
                        member_count = 0
                        for member_count, member in enumerate(handle, start=1):
                            parts, total_bytes = _validate_member(
                                member,
                                seen=seen,
                                member_index=member_count,
                                total_bytes=total_bytes,
                                max_members=max_members,
                                max_member_bytes=max_member_bytes,
                                max_total_bytes=max_total_bytes,
                                required_root=required_root,
                            )
                            target = destination.joinpath(*parts)
                            if member.isdir():
                                target.mkdir(parents=True, exist_ok=True)
                                if not target.is_dir():
                                    raise UnsafeArchiveError(
                                        "archive directory collides with a file: "
                                        f"{member.name!r}"
                                    )
                            else:
                                target.parent.mkdir(parents=True, exist_ok=True)
                                source = handle.extractfile(member)
                                if source is None:
                                    raise UnsafeArchiveError(
                                        "archive member has no readable content: "
                                        f"{member.name!r}"
                                    )
                                with source, target.open("xb") as output:
                                    _copy_exact(source, output, member.size)
                            extracted.append(target)
                        if member_count == 0:
                            raise UnsafeArchiveError(
                                "archive contains no members"
                            )

                    # Force gzip to authenticate its footer instead of stopping
                    # at tar's end marker. Discard any decoded padding/tail.
                    while decoded.read(_COPY_CHUNK_BYTES):
                        pass
        except Exception as error:
            stream_error = error
            natural_return_code = _stop_process(process)
        else:
            try:
                return_code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _stop_process(process)
                _remove_destination(destination)
                raise RuntimeError(
                    "age closed its output but did not exit within 5 seconds"
                )
            except Exception:
                _stop_process(process)
                _remove_destination(destination)
                raise
        finally:
            timeout_guard.cancel()
            timeout_guard.join(timeout=1)

        try:
            error_text = _stderr_tail(error_stream)
        except Exception:
            _remove_destination(destination)
            raise
        if timed_out.is_set():
            _remove_destination(destination)
            timeout_error = TimeoutError(
                "age decryption exceeded "
                f"{age_timeout_seconds:g} seconds"
            )
            if stream_error is not None:
                raise timeout_error from stream_error
            raise timeout_error
        if stream_error is not None:
            _remove_destination(destination)
            if (
                natural_return_code is not None
                and natural_return_code != 0
                and not isinstance(stream_error, UnsafeArchiveError)
            ):
                raise _age_failure(
                    natural_return_code,
                    error_text,
                ) from stream_error
            raise stream_error
        if return_code != 0:
            _remove_destination(destination)
            raise _age_failure(return_code, error_text)

        return tuple(extracted)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely extract a Discord Math tar.gz artifact."
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--identity",
        type=Path,
        help="age identity file; when set, decrypt the archive as a stream.",
    )
    parser.add_argument("--age-executable", default="age")
    parser.add_argument(
        "--age-timeout-seconds",
        type=float,
        default=DEFAULT_AGE_TIMEOUT_SECONDS,
    )
    parser.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    parser.add_argument(
        "--max-member-bytes",
        type=int,
        default=DEFAULT_MAX_MEMBER_BYTES,
    )
    parser.add_argument(
        "--max-total-bytes",
        type=int,
        default=DEFAULT_MAX_TOTAL_BYTES,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.identity is not None:
        extracted = safe_extract_age_archive(
            args.archive,
            args.identity,
            args.destination,
            age_executable=args.age_executable,
            max_members=args.max_members,
            max_member_bytes=args.max_member_bytes,
            max_total_bytes=args.max_total_bytes,
            age_timeout_seconds=args.age_timeout_seconds,
        )
    else:
        extracted = safe_extract_tar(
            args.archive,
            args.destination,
            max_members=args.max_members,
            max_member_bytes=args.max_member_bytes,
            max_total_bytes=args.max_total_bytes,
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "archive": str(args.archive.resolve()),
                "destination": str(args.destination.resolve()),
                "members": len(extracted),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
