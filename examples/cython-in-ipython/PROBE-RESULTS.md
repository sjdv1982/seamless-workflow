# Throwaway probe results

Measured on 2026-10-10 in the `seamless1` conda environment, with Python 3.14,
IPython 9.13.0 and Cython 3.3.0. No formal test suite was run.

A temporary script built a live Context in a temporary directory, mounted both
`cell-ipython.ipy` (read/write) and `cell-ipython.html` (write), edited the source
file, waited for graph and mount synchronization, and inspected each HTML file.
Wall times were measured independently with `time.perf_counter()` around the
file edit, input update, computation, and synchronization. Cython's on-disk
compilation cache was already warm for the endpoint sources; these are not cold
compilation benchmarks. Initial Context construction is excluded.

| Source | i | Function time (s) | Wall time (s) | HTML bytes |
| --- | ---: | ---: | ---: | ---: |
| ORIGINAL | 100 | 0.000666 | 1.348 | 35,326 |
| ORIGINAL | 6000 | 2.660 | 2.998 | 35,326 |
| Intermediate | 6000 | 2.135 | 3.448 | 33,057 |
| OPTIMIZED | 6000 | 0.178 | 1.337 | 28,713 |
| Return to ORIGINAL (Seamless cache hit) | 6000 | 2.660 (restored) | 0.659 | 35,326 |

The intermediate source used all the optimized type declarations but retained
`from math import log`, rather than `from libc.math cimport log`.
Every variant at i=6000 returned exactly `55423.15899095317`; i=100 returned
`7.113435093928883`, matching the legacy recorded values.

Both mounts reported in sync, with matching node/disk checksums and no errors.
The source cell matched the edited file after canonical newline normalization;
the HTML cell matched its mounted file after the same normalization. HTML
changed after each source variant and reverted to the original HTML on the
cache hit. Changing only i preserved the annotation HTML.

The ORIGINAL and OPTIMIZED HTML were rendered in headless Chrome and inspected
visually. ORIGINAL's nested loops and log calculation are yellow (Python
interaction); OPTIMIZED's typed loops and calculation are white, with Cython
annotation score 0. Import/function entry/return retain some yellow. The
optimized function was about 15 times faster in this measurement.

Temporary script, JSON measurements, HTML snapshots, and screenshots are under
`/tmp/cython-port-probes` (successful run: `mounted-frsre23r`). These are probe
artifacts, not a committed test suite.
