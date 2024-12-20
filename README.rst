**Monkey wrench** gathers the tools to run `CHIMP`_, fetch and process its input files, analyse and visualise its
output, and much more! It is an attempt to avoid *reinventing the wheel* by consolidating all Python scripts and
codes that we use alongside CHIMP.

In particular, you can:

* Query and fetch SEVIRI data from the EUMETSAT API via either single queries or batches
* Resample fetched files from the API
* Use the provided CHIMP extensions to allow for reading files which are not natively supported by CHIMP
* Perform queries on local files, datetime objects, and conversion between filenames and datetime stamps
* Run CHIMP for different groups of input files in a queue
* Perform post-processing of CHIMP output as well as some visualization utilities
* Use it as a task runner

.. _CHIMP: https://github.com/simonpf/chimp
