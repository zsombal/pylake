import math
import numpy as np

from .detail.timeindex import to_timestamp


class Slice:
    """A lazily evaluated slice of a timeline/HDF5 channel

    Users will only ever get these as a result of slicing a timeline/HDF5
    channel or slicing another slice (via this class' `__getitem__`), i.e.
    the `__init__` method will never be invoked by users.

    Parameters
    ----------
    data_source : Any
        A slice data source. Can be `Continuous`, `TimeSeries`, 'TimeTags',
        or any other source which conforms to the same interface.
    labels : Dict[str, str]
        Plot labels: "x", "y", "title".
    """
    def __init__(self, data_source, labels=None):
        self._src = data_source
        self.labels = labels or {}

    def __len__(self):
        return len(self._src)

    def __getitem__(self, item):
        """All indexing is in timestamp units (ns)"""
        if not isinstance(item, slice):
            raise IndexError("Scalar indexing is not supported, only slicing")
        if item.step is not None:
            raise IndexError("Slice steps are not supported")

        if len(self) == 0:
            return self

        src_start = self._src.start
        src_stop = self._src.stop

        start, stop = item.start, item.stop
        if start is None:
            start = src_start
        if stop is None:
            stop = src_stop
        start, stop = (to_timestamp(v, src_start, src_stop) for v in (start, stop))

        return self._with_data_source(self._src.slice(start, stop))

    def _with_data_source(self, data_source):
        """Return a copy of this slice with a different data source, but keep other properties"""
        return self.__class__(data_source, self.labels)

    @property
    def data(self):
        """The primary values of this channel slice"""
        return self._src.data

    @property
    def timestamps(self):
        """Absolute timestamps (since epoch) which correspond to the channel data"""
        return self._src.timestamps

    @property
    def sample_rate(self) -> int:
        """The data frequency for continuous data sources or `None` if it's variable"""
        try:
            return self._src.sample_rate
        except AttributeError:
            return None

    def downsampled_by(self, factor, reduce=np.mean):
        """Return a copy of this slice which is downsampled by `factor`

        Parameters
        ----------
        factor : int
            The size and sample rate of the data will be divided by this factor.
        reduce : callable
            The `numpy` function which is going to reduce multiple samples into one.
            The default is `np.mean`, but `np.sum` could also be appropriate for some
            cases, e.g. photon counts.
        """
        return self._with_data_source(self._src.downsampled_by(factor, reduce))

    def plot(self, **kwargs):
        """A simple line plot to visualize the data over time

        Parameters
        ----------
        **kwargs
            Forwarded to :func:`matplotlib.pyplot.plot`.
        """
        import matplotlib.pyplot as plt

        seconds = (self.timestamps - self.timestamps[0]) / 1e9
        plt.plot(seconds, self.data, **kwargs)
        plt.xlabel(self.labels.get("x", "Time") + " (s)")
        plt.ylabel(self.labels.get("y", "y"))
        plt.title(self.labels.get("title", "title"))


def _downsample(data, factor, reduce):
    def round_down(size, n):
        """Round down `size` to the nearest multiple of `n`"""
        return int(math.floor(size / n)) * n

    data = data[:round_down(data.size, factor)]
    return reduce(data.reshape(-1, factor), axis=1)


class Continuous:
    """A source of continuous data for a timeline slice

    Parameters
    ----------
    data : array_like
        Anything that's convertible to an `np.ndarray`.
    start : int
        Timestamp of the first data point.
    dt : int
        Delta between two timestamps. Constant for the entire data range.
    """
    def __init__(self, data, start, dt):
        self._src_data = data
        self._cached_data = None
        self.start = start
        self.stop = start + len(data) * dt
        self.dt = dt

    def __len__(self):
        return len(self._src_data)

    @staticmethod
    def from_dataset(dset, y_label="y"):
        start = dset.attrs["Start time (ns)"]
        dt = int(1e9 / dset.attrs["Sample rate (Hz)"])
        return Slice(Continuous(dset.value, start, dt),
                     labels={"title": dset.name.strip("/"), "y": y_label})

    @property
    def data(self):
        if self._cached_data is None:
            self._cached_data = np.asarray(self._src_data)
        return self._cached_data

    @property
    def timestamps(self):
        return np.arange(self.start, self.stop, self.dt)

    @property
    def sample_rate(self):
        return int(1e9 / self.dt)

    def slice(self, start, stop):
        # TODO: should be lazily evaluated
        idx = np.logical_and(start <= self.timestamps, self.timestamps < stop)
        return self.__class__(self.data[idx], max(start, self.start), self.dt)

    def downsampled_by(self, factor, reduce):
        return self.__class__(_downsample(self.data, factor, reduce),
                              start=self.start + self.dt * (factor - 1) // 2, dt=self.dt * factor)


class TimeSeries:
    """A source of time series data for a timeline slice

    Parameters
    ----------
    data : array_like
        Anything that's convertible to an `np.ndarray`.
    timestamps : array_like
        An array of integer timestamps.
    """
    def __init__(self, data, timestamps):
        assert len(data) == len(timestamps)
        # TODO: should be lazily evaluated
        self.data = np.asarray(data)
        self.timestamps = np.asarray(timestamps)

    def __len__(self):
        return len(self.data)

    @staticmethod
    def from_dataset(dset, y_label="y"):
        return Slice(TimeSeries(dset["Value"], dset["Timestamp"]),
                     labels={"title": dset.name.strip("/"), "y": y_label})

    @property
    def start(self):
        return self.timestamps[0]

    @property
    def stop(self):
        return self.timestamps[-1] + 1

    def slice(self, start, stop):
        idx = np.logical_and(start <= self.timestamps, self.timestamps < stop)
        return self.__class__(self.data[idx], self.timestamps[idx])

    def downsampled_by(self, factor, reduce):
        raise NotImplementedError("Downsampling is currently not available for time series data")


class TimeTags:
    """A source of time tag data for a timeline slice

    Parameters
    ----------
    data : array_like
        Anything that's convertible to an `np.ndarray`
    start : int
        Timestamp of the start of the channel slice
    stop : int
        Timestamp of the end of the channel slice
    """
    def __init__(self, data, start=None, stop=None):
        self.data = np.asarray(data, dtype=np.int64)
        self.start = start if start is not None else \
            (self.data[0] if self.data.size > 0 else 0)
        self.stop = stop if stop is not None else \
            (self.data[-1]+1 if self.data.size > 0 else 0)

    def __len__(self):
        return self.data.size

    @staticmethod
    def from_dataset(dset, y_label="y"):
        return Slice(TimeTags(dset.value))

    @property
    def timestamps(self):
        # For time tag data, the data is the timestamps!
        return self.data

    def slice(self, start, stop):
        idx = np.logical_and(start <= self.data, self.data < stop)
        return self.__class__(self.data[idx], min(start, stop), max(start, stop))

    def downsampled_by(self, factor, reduce):
        raise NotImplementedError("Downsampling is not available for time tag data")


class Empty:
    """A lightweight source of no data

    Both `Continuous` and `TimeSeries` can be empty, but this is a lighter
    class which can be returned an empty slice from properties.
    """
    def __len__(self):
        return 0

    @property
    def data(self):
        return np.empty(0)

    @property
    def timestamps(self):
        return np.empty(0)


empty_slice = Slice(Empty())


def channel_class(dset):
    """Figure out the right channel source class given an HDF5 dataset"""
    if "Kind" in dset.attrs:
        # Bluelake HDF5 files >=v2 mark channels with a "Kind" attribute:
        kind = dset.attrs["Kind"]
        if kind == b"TimeTags":
            return TimeTags
        elif kind == b"TimeSeries":
            return TimeSeries
        elif kind == b"Continuous":
            return Continuous
        else:
            raise RuntimeError("Unknown channel kind " + str(kind))
    # For compatibility with Bluelake HDF5 files v1:
    elif dset.dtype.fields is None:
        return Continuous
    else:
        return TimeSeries


