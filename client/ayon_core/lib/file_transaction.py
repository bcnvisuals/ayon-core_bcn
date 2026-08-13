from __future__ import annotations

import concurrent.futures
import os
import logging
import errno
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Any

from ayon_core.lib import create_hard_link

from .serverside_speedcopy import copyfile, both_cifs_or_smb2


class DuplicateDestinationError(ValueError):
    """Error raised when transfer destination already exists in queue.

    The error is only raised if `allow_queue_replacements` is False on the
    FileTransaction instance and the added file to transfer is of a different
    src file than the one already detected in the queue.

    """


class FileTransaction:
    """File transaction with rollback options.

    The file transaction is a three-step process.

    1) Rename any existing files to a "temporary backup" during `process()`
    2) Copy the files to final destination during `process()`
    3) Remove any backed up files (*no rollback possible!) during `finalize()`

    Step 3 is done during `finalize()`. If not called the .bak files will
    remain on disk.

    These steps try to ensure that we don't overwrite half of any existing
    files e.g. if they are currently in use.

    Note:
        A regular filesystem is *not* a transactional file system and even
        though this implementation tries to produce a 'safe copy' with a
        potential rollback do keep in mind that it's inherently unsafe due
        to how filesystem works and a myriad of things could happen during
        the transaction that break the logic. A file storage could go down,
        permissions could be changed, other machines could be moving or writing
        files. A lot can happen.

    Warning:
        Any folders created during the transfer will not be removed.

    """
    MODE_COPY = 0
    MODE_HARDLINK = 1

    def __init__(
        self,
        log: logging.Logger | None = None,
        allow_queue_replacements: bool = False,
    ) -> None:
        if log is None:
            log = logging.getLogger("FileTransaction")

        self.log: logging.Logger = log

        # The transfer queue
        # todo: make this an actual FIFO queue?
        self._transfers: dict[str, tuple[str, dict[str, Any]]] = {}

        # Destination file paths that a file was transferred to
        self._transferred: list[str] = []

        # Backup file location mapping to original locations
        self._backup_to_original: dict[str, str] = {}

        self._allow_queue_replacements: bool = allow_queue_replacements

        # Cached flag whether server-side copy is possible for this transaction
        # Computed once in `process` to avoid repeated detection per file.
        self._serverside_ok: bool | None = None

    def add(self, src: str, dst: str, mode: int = MODE_COPY) -> None:
        """Add a new file to transfer queue.

        Args:
            src (str): Source path.
            dst (str): Destination path.
            mode (MODE_COPY, MODE_HARDLINK): Transfer mode.

        """
        opts = {"mode": mode}

        src = os.path.normpath(os.path.abspath(src))
        dst = os.path.normpath(os.path.abspath(dst))

        if dst in self._transfers:
            queued_src = self._transfers[dst][0]
            if src == queued_src:
                self.log.debug(
                    f"File transfer was already in queue: {src} -> {dst}"
                )
                return

            if not self._allow_queue_replacements:
                raise DuplicateDestinationError(
                    "Transfer to destination is already in queue: "
                    f"{queued_src} -> {dst}. It's not allowed to be"
                    f" replaced by a new transfer from {src}"
                )

            self.log.warning("File transfer in queue replaced..")
            self.log.debug(
                f"Removed from queue: {queued_src} -> {dst}"
                f" replaced by {src} -> {dst}"
            )

        self._transfers[dst] = (src, opts)

    def process(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            # Compute serverside_ok once for the whole transaction to avoid
            # repeated statfs checks or filesystem probing per file.
            if self._transfers and self._serverside_ok is None:
                # Peek at any one item from the transfer queue
                any_dst, (any_src, _) = next(iter(self._transfers.items()))
                try:
                    self._serverside_ok = both_cifs_or_smb2(
                        any_src,
                        os.path.dirname(os.path.abspath(any_dst)) or "."
                    )
                except Exception:
                    # If detection fails for any reason, disable serverside
                    # optimization to ensure robust copying.
                    self._serverside_ok = False
            # Submit backup tasks
            backup_futures = [
                executor.submit(self._backup_file, dst, src)
                for dst, (src, _) in self._transfers.items()
            ]
            wait_for_future_errors(
                executor, backup_futures, logger=self.log
            )

            # Submit transfer tasks
            transfer_futures = [
                executor.submit(self._transfer_file, dst, src, opts)
                for dst, (src, opts) in self._transfers.items()
            ]
            transfer_start_time = time.perf_counter()
            try:
                wait_for_future_errors(
                    executor, transfer_futures, logger=self.log
                )
            finally:
                transfer_elapsed = time.perf_counter() - transfer_start_time
                self.log.debug(
                    f"Transfer of {len(self._transfers)} file(s) finished in "
                    f"{transfer_elapsed:.2f}s"
                )

    def _backup_file(self, dst: str, src: str) -> None:
        self.log.debug(f"Checking file destination ... {src} -> {dst}")
        path_same = self._same_paths(src, dst)
        if path_same or not os.path.exists(dst):
            return

        # Backup original file
        backup = dst + ".bak"
        self._backup_to_original[backup] = dst
        self.log.debug(f"Backup existing file: {dst} -> {backup}")
        os.rename(dst, backup)

    def _transfer_file(
        self, dst: str, src: str, opts: dict[str, Any]
    ) -> None:
        """Transfer file from source to destination with fallback support.

        Attempts to use the configured copyfile implementation first, but
        falls back to shutil.copyfile if it fails due to permission issues or
        other filesystem-related problems.

        Args:
            dst (str): Destination file path.
            src (str): Source file path.
            opts (dict): Transfer options containing mode information.

        """
        path_same = self._same_paths(src, dst)
        if path_same:
            self.log.debug(
                f"Source and destination are same files {src} -> {dst}")
            return

        self._create_folder_for_file(dst)

        if opts["mode"] == self.MODE_COPY:
            self.log.debug(f"Copying file server-side ... {src} -> {dst}")
            try:
                copyfile(
                    src,
                    dst,
                    serverside_ok=self._serverside_ok
                )
            except (PermissionError, OSError) as exc:
                self.log.warning(
                    f"copyfile failed ({exc}), falling back to shutil.copyfile"
                )
                try:
                    shutil.copyfile(src, dst)
                except Exception as fallback_exc:
                    self.log.error(
                        "Both copyfile and shutil.copyfile failed for "
                        f"{src} -> {dst}"
                    )
                    raise fallback_exc
        elif opts["mode"] == self.MODE_HARDLINK:
            self.log.debug(f"Hardlinking file ... {src} -> {dst}")
            create_hard_link(src, dst)

        self._transferred.append(dst)

    def finalize(self) -> None:
        # Delete any backed up files
        for backup in self._backup_to_original.keys():
            try:
                os.remove(backup)
            except OSError:
                self.log.error(
                    f"Failed to remove backup file: {backup}",
                    exc_info=True)

    def rollback(self) -> Exception | None:
        errors = 0
        last_exc = None
        # Rollback any transferred files
        for path in self._transferred:
            try:
                os.remove(path)
            except OSError as exc:
                last_exc = exc
                errors += 1
                self.log.error(
                    f"Failed to rollback created file: {path}",
                    exc_info=True)

        # Rollback the backups
        for backup, original in self._backup_to_original.items():
            try:
                os.rename(backup, original)
            except OSError as exc:
                last_exc = exc
                errors += 1
                self.log.error(
                    f"Failed to restore original file: {backup} -> {original}",
                    exc_info=True)

        if errors:
            self.log.error(
                f"{errors} errors occurred during rollback.",
                exc_info=True)
            raise last_exc

    @property
    def transferred(self) -> list[str]:
        """Return the processed transfers destination paths"""
        return list(self._transferred)

    @property
    def backups(self) -> list[str]:
        """Return the backup file paths"""
        return list(self._backup_to_original.keys())

    def _create_folder_for_file(self, path: str) -> None:
        dirname = os.path.dirname(path)
        try:
            os.makedirs(dirname)
        except OSError as e:
            if e.errno != errno.EEXIST:
                self.log.critical("An unexpected error occurred.")
                raise e

    def _same_paths(self, src: str, dst: str) -> bool:
        # handles same paths but with C:/project vs c:/project
        if os.path.exists(src) and os.path.exists(dst):
            return os.stat(src) == os.stat(dst)

        return src == dst


def wait_for_future_errors(
    executor: ThreadPoolExecutor,
    futures: list[Future],
    logger: logging.Logger | None = None
) -> Exception | None:
    """For the ThreadPoolExecutor shutdown and cancel futures as soon one of
    the workers raises an error as they complete.

    The ThreadPoolExecutor only cancels pending futures on exception but will
    still complete those that are running - each which also themselves could
    fail. We log all exceptions but re-raise the last exception only.
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    for future in concurrent.futures.as_completed(futures):
        exception = future.exception()
        if exception:
            # As soon as an error occurs, stop executing more futures.
            # Running workers, however, will still be complete, so we also want
            # to log those errors if any occurred on them.
            executor.shutdown(wait=True, cancel_futures=True)
            break
    else:
        # Futures are completed, no exceptions occurred
        return None

    # An exception occurred in at least one future. Get exceptions from
    # all futures that are done and ended up failing until that point.
    exceptions = []
    for future in futures:
        if not future.cancelled() and future.done():
            exception = future.exception()
            if exception:
                exceptions.append(exception)

    # Log any exceptions that occurred in all workers
    for exception in exceptions:
        logger.error("Error occurred in worker", exc_info=exception)

    # Raise the last exception
    raise exceptions[-1]
