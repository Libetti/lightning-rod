from __future__ import annotations

from threading import RLock


# netCDF4 uses native HDF5 libraries that may not be thread-safe in packaged
# wheels. Serialize Dataset access across CMI and GLM background workers.
NETCDF_LOCK = RLock()
