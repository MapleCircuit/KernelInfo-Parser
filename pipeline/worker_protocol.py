"""Frame protocol and split-counter keys shared by the worker pool parent and the file-processing workers."""

from typing import Any
import io
import struct
import pickle
import zlib




# Frames on each worker's private duplex pipe. The parent sends task frames (two ahead, so a worker never
# waits for its next batch) and a stop frame once no batch is left; the worker answers each task with a
# result frame and ends with a done frame. A pipe that closes before the done frame means the worker died
# (signal, OOM kill, libclang abort) while parsing the oldest batch it had not returned.
_FRAME_TASK = b"T"


_FRAME_STOP = b"S"


_FRAME_RESULT = b"R"


_FRAME_INIT_FAILED = b"I"


_FRAME_DONE = b"D"


_RESULT_HEADER = struct.Struct("<cqB")  # frame tag, batch id, zlib-compressed flag


_STAGE_END = object()


# Batches sent to a worker ahead of the one it parses
_TASKS_AHEAD = 2


# Serialized batches the reader thread may hold for the ingest thread; more only adds parent memory
_PREFETCH_DEPTH = 8


def _recv_task(conn: Any) -> tuple[int, list[str]] | None:
    """Worker side: wait for the next `(batch_id, files)` task, or None on the stop frame or a closed pipe."""
    try:
        frame = conn.recv_bytes()
    except (EOFError, OSError):
        return None
    if frame[:1] != _FRAME_TASK:
        return None
    return pickle.loads(frame[1:])


def _encode_batch_result(
    batch_id: int,
    batch_cs_dict: dict[str, Any],
    errors: list[tuple[str, str, str]],
    compress: bool,
    blob_symbols: dict[int, dict[tuple[str, int], int]] | None = None,
) -> bytes | memoryview:
    """Serialize a parsed batch, per-file errors, and streamed blob symbols into one result frame."""
    buf = io.BytesIO()
    buf.write(_RESULT_HEADER.pack(_FRAME_RESULT, batch_id, 0))
    if blob_symbols:
        pickle.dump((batch_cs_dict, errors, blob_symbols), buf, protocol=pickle.HIGHEST_PROTOCOL)
    else:
        pickle.dump((batch_cs_dict, errors), buf, protocol=pickle.HIGHEST_PROTOCOL)
    if not compress:
        return buf.getbuffer()
    body = zlib.compress(buf.getbuffer()[_RESULT_HEADER.size :], level=1)
    return _RESULT_HEADER.pack(_FRAME_RESULT, batch_id, 1) + body


def _decode_batch_result(frame: bytes) -> tuple[int, bytes | memoryview]:
    """Split a result frame into its batch id and pickled `(batch_cs_dict, errors)` body."""
    _tag, batch_id, compressed = _RESULT_HEADER.unpack_from(frame, 0)
    body = memoryview(frame)[_RESULT_HEADER.size :]
    return batch_id, (zlib.decompress(body) if compressed else body)


# Per-worker counters a worker sends with its done frame (file_processing_worker)
_WORKER_SPLIT_KEYS = (
    "busy_s", "task_wait_s", "send_wait_s", "gc_s", "prune_s", "pre_resolve_s", "ref_views_s", "encode_s", "db_reads", "db_read_s",
)
