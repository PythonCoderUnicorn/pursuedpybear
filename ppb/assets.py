"""
The asset loading system.
"""
import abc
import concurrent.futures
import logging
import threading
import weakref

import ppb.vfs as vfs
import ppb.events as events
from ppb.systemslib import System

__all__ = 'Asset', 'AssetLoadingSystem',

logger = logging.getLogger(__name__)


class AbstractAsset(abc.ABC):
    """
    The asset interface.

    This defines the common interface for virtual assets, proxy assets, and
    real/file assets.
    """
    @abc.abstractmethod
    def load(self):
        """
        Get the data of this asset, in the appropriate form.
        """

    def is_loaded(self):
        """
        Returns if the data is ready now or if :py:meth:`load()` will block.
        """
        return True


_asset_cache = weakref.WeakValueDictionary()


class Asset(AbstractAsset):
    """
    A resource to be loaded from the filesystem and used.

    Meant to be subclassed, but in specific ways.
    """
    def __new__(cls, name):
        clsname = f"{cls.__module__}:{cls.__qualname__}"
        try:
            return _asset_cache[(clsname, name)]
        except KeyError:
            inst = super().__new__(cls)
            _asset_cache[(clsname, name)] = inst
            return inst

    def __init__(self, name):
        self.name = str(name)
        self._finished = threading.Event()
        _hint(self.name, self._finished_background)

    def __repr__(self):
        return f"<{type(self).__name__} name={self.name!r}>"

    def _finished_background(self, fut):
        # Internal
        # Called in background thread
        try:
            try:
                raw = fut.result()
            except FileNotFoundError:
                if hasattr(self, 'file_missing'):
                    logger.warning("File not found: %r", self.name)
                    self._data = self.file_missing()
                    if _finished is not None:
                        _finished(self)
                else:
                    raise
            else:
                self._data = self.background_parse(raw)
                if _finished is not None:
                    _finished(self)
        except Exception as exc:
            # Save unhandled exceptions to be raised in the main thread
            self._raise_error = exc
        finally:
            # This always needs to happen so the main thread isn't just blocked
            self._finished.set()

    def background_parse(self, data: bytes):
        """
        Takes the data loaded from the file and returns the parsed data.

        Subclasses probably want to override this.

        Called in the background thread.
        """
        return data

    def is_loaded(self):
        """
        Returns if the data has been loaded and parsed.
        """
        return self._finished.is_set()

    def load(self, timeout: float = None):
        """
        Gets the parsed data.

        Will block until the data is loaded.
        """
        if _hint is _default_hint:
            logger.warn(f"Waited on {self!r} before the engine began")
        self._finished.wait(timeout)
        if hasattr(self, '_raise_error'):
            raise self._raise_error
        else:
            return self._data


class AssetLoadingSystem(System):
    def __init__(self, *, engine, **_):
        super().__init__(**_)
        self.engine = engine
        self._executor = concurrent.futures.ThreadPoolExecutor()
        self._queue = weakref.WeakValueDictionary()  # maps names to futures
        self._began = 0
        self._ended = 0

    def __enter__(self):
        # 1. Register ourselves as the hint provider
        global _hint, _finished, _backlog
        assert _hint is _default_hint
        _hint = self._hint
        _finished = self._finished

        # 2. Grab-n-clear the backlog (atomically?)
        queue, _backlog = _backlog, []

        # 3. Process the backlog
        for filename, callback in queue:
            self._hint(filename, callback)

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Reset the hint provider
        global _hint, _finished
        _hint = _default_hint
        _finished = None

    def _hint(self, filename, callback=None):
        try:
            fut = self._queue[filename]
        except KeyError:
            self._began += 1
            fut = self._queue[filename] = self._executor.submit(self._load, filename)
        if callback is not None:
            fut.add_done_callback(callback)

    @staticmethod
    def _load(filename):
        with vfs.open(filename) as file:
            return file.read()

    def _finished(self, asset):
        self._ended += 1
        self.engine.signal(events.AssetLoaded(
            asset=asset,
            total_loaded=self._ended,
            total_queued=self._began - self._ended,
        ))


_backlog = []


def _default_hint(filename, callback=None):
    _backlog.append((filename, callback))


_hint = _default_hint
_finished = None
